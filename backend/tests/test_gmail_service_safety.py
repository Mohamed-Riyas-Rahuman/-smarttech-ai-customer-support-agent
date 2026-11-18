"""Offline tests for OAuth, identity, permission, and Gmail API status separation."""
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from unittest.mock import PropertyMock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from googleapiclient.errors import HttpError
from httplib2 import Response

from app.core.config import settings
from app.integrations.email.gmail_service import GmailService


ALL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def test_server_callback_directs_desktop_client_to_existing_local_flow(tmp_path):
    client_path = tmp_path / "client.json"
    client_path.write_text(json.dumps({"installed": {
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "test-secret",
        "redirect_uris": ["http://localhost"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}), encoding="utf-8")
    service = GmailService()
    with patch.object(GmailService, "credentials_path", new_callable=PropertyMock, return_value=client_path):
        with pytest.raises(ValueError, match="scripts/authorize_gmail.py"):
            service.create_oauth_flow("http://localhost:8000/api/gmail/oauth/callback")


def test_web_oauth_client_supports_exact_configured_callback(tmp_path):
    client_path = tmp_path / "client.json"
    client_path.write_text(json.dumps({"web": {
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "test-secret",
        "redirect_uris": ["http://localhost:8000/api/gmail/oauth/callback"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}), encoding="utf-8")
    service = GmailService()
    with patch.object(GmailService, "credentials_path", new_callable=PropertyMock, return_value=client_path):
        flow = service.create_oauth_flow("http://localhost:8000/api/gmail/oauth/callback")
        with pytest.raises(ValueError, match="authorized redirect URIs"):
            service.create_oauth_flow("http://localhost:8000/other-callback")
    assert flow.redirect_uri == "http://localhost:8000/api/gmail/oauth/callback"


def _status(scopes=ALL_SCOPES, profile=None, profile_error=None):
    service = GmailService()
    token = Path(__file__).parent / ".pytest_cache_gmail_token"
    token.write_text("test-placeholder", encoding="utf-8")
    try:
        credentials = SimpleNamespace(valid=True, scopes=scopes)
        api = MagicMock()
        if profile_error:
            api.users().getProfile().execute.side_effect = profile_error
        else:
            api.users().getProfile().execute.return_value = profile or {"emailAddress": "support@example.test"}
        with patch.object(service, "has_credentials_file", return_value=True), \
             patch.object(service, "has_token_file", return_value=True), \
             patch.object(service, "get_credentials", return_value=credentials), \
             patch.object(service, "get_client", return_value=api), \
             patch.object(GmailService, "token_path", new_callable=lambda: property(lambda _self: token)):
            return service.get_authorization_status()
    finally:
        token.unlink(missing_ok=True)


def test_verified_oauth_profile_and_send_permission_are_distinct_checks():
    result = _status()
    assert result["is_authorized"] is True
    assert result["has_send_permission"] is True
    assert result["connected_email"] == "support@example.test"
    assert result["connection_state"] == "connected"
    assert result["gmail_api_available"] is True


def test_missing_send_grant_is_not_reported_as_connected():
    result = _status(scopes=["https://www.googleapis.com/auth/gmail.modify"])
    assert result["is_authorized"] is True
    assert result["has_send_permission"] is False
    assert result["connection_state"] == "permission_missing"


def test_missing_profile_address_fails_strict_identity_verification():
    result = _status(profile={"historyId": "123"})
    assert result["is_authorized"] is True
    assert result["connected_email"] is None
    assert result["connection_state"] == "profile_verification_failed"
    assert result["gmail_api_available"] is True


def test_gmail_api_outage_is_distinguished_from_oauth_expiration():
    error = HttpError(Response({"status": "503"}), b"temporary service outage")
    result = _status(profile_error=error)
    assert result["connection_state"] == "gmail_api_unavailable"
    assert result["gmail_api_available"] is False
    assert GmailService._classify_oauth_error(RuntimeError("invalid_grant")) == "refresh_token_revoked_or_expired"


def test_expired_access_token_refreshes_and_persists_updated_token(tmp_path):
    service = GmailService()
    token_path = tmp_path / "oauth-token.json"
    token_path.write_text("{}", encoding="utf-8")

    class MockCredentials:
        expired = True
        valid = False
        refresh_token = "test-refresh-token"
        scopes = ALL_SCOPES

        def refresh(self, _request):
            self.expired = False
            self.valid = True

        def to_json(self):
            return json.dumps({"token": "test-refreshed-access-token"})

    credentials = MockCredentials()
    with patch.object(GmailService, "token_path", new_callable=PropertyMock, return_value=token_path), \
         patch("app.integrations.email.gmail_service.Credentials.from_authorized_user_file", return_value=credentials), \
         patch("app.integrations.email.gmail_service.Request"):
        loaded = service.get_credentials()

    assert loaded is credentials
    assert credentials.valid is True
    assert json.loads(token_path.read_text(encoding="utf-8"))["token"] == "test-refreshed-access-token"
    assert service.last_auth_error is None


def test_revoked_refresh_token_requires_reauthorization(tmp_path):
    service = GmailService()
    token_path = tmp_path / "oauth-token.json"
    token_path.write_text("{}", encoding="utf-8")

    class MockCredentials:
        expired = True
        valid = False
        refresh_token = "test-refresh-token"
        scopes = ALL_SCOPES

        def refresh(self, _request):
            raise RuntimeError("invalid_grant")

    with patch.object(GmailService, "token_path", new_callable=PropertyMock, return_value=token_path), \
         patch("app.integrations.email.gmail_service.Credentials.from_authorized_user_file", return_value=MockCredentials()), \
         patch("app.integrations.email.gmail_service.Request"):
        assert service.get_credentials() is None

    assert service.last_auth_error == "refresh_token_revoked_or_expired"


def test_oauth_reconnection_persists_only_after_state_and_scope_validation(tmp_path):
    service = GmailService()
    token_path = tmp_path / "oauth-token.json"
    payload = json.dumps({"nonce": "unit-test", "issued_at": int(time.time())}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(settings.jwt_secret.encode(), encoded.encode(), hashlib.sha256).digest()
    state = encoded + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    class MockCredentials:
        scopes = ALL_SCOPES
        refresh_token = "test-refresh-token"

        def to_json(self):
            return json.dumps({"token": "new-test-access-token", "refresh_token": self.refresh_token, "scopes": ALL_SCOPES})

    flow = MagicMock()
    flow.credentials = MockCredentials()
    with patch.object(GmailService, "token_path", new_callable=PropertyMock, return_value=token_path), \
         patch.object(service, "create_oauth_flow", return_value=flow):
        credentials = service.complete_oauth_flow(
            code="test-authorization-code", state=state,
            redirect_uri="http://localhost:8000/api/gmail/oauth/callback",
            code_verifier="test-pkce-verifier",
        )

    flow.fetch_token.assert_called_once_with(code="test-authorization-code")
    assert credentials is flow.credentials
    assert json.loads(token_path.read_text(encoding="utf-8"))["token"] == "new-test-access-token"


def test_expired_token_without_refresh_token_requires_reauthorization(tmp_path):
    service = GmailService()
    token_path = tmp_path / "oauth-token.json"
    token_path.write_text("{}", encoding="utf-8")

    credentials = type("ExpiredCredentials", (), {
        "expired": True, "valid": False, "refresh_token": None, "scopes": ALL_SCOPES,
    })()
    with patch.object(GmailService, "token_path", new_callable=PropertyMock, return_value=token_path), \
         patch("app.integrations.email.gmail_service.Credentials.from_authorized_user_file", return_value=credentials):
        assert service.get_credentials() is None
    assert service.last_auth_error == "reauthorization_required"
