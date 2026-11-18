"""Gmail API service: OAuth2 flow, inbox monitoring/retrieval, and threaded reply sending."""
import base64
import email.utils
import hashlib
import hmac
import json
from email.message import EmailMessage as PyEmailMessage
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build, Resource

from app.core.config import BACKEND_DIR, settings
from app.core.logging import logger


class GmailService:
    def __init__(self):
        self._credential_lock = threading.RLock()
        self.last_auth_error: Optional[str] = None
        self._permanent_failure_signature: tuple[int, int] | None = None
        self._refresh_retry_after = 0.0
        self._profile_cache_signature: tuple[int, int] | None = None
        self._profile_cache_email: str | None = None
        self._profile_cache_error: str | None = None
        self._profile_cache_api_available: bool | None = None
        self._profile_cache_until = 0.0
        self.scopes = [s.strip() for s in settings.gmail_scopes.split() if s.strip()]
        if "https://www.googleapis.com/auth/gmail.send" not in self.scopes:
            self.scopes.append("https://www.googleapis.com/auth/gmail.send")
        if "https://www.googleapis.com/auth/gmail.modify" not in self.scopes:
            self.scopes.append("https://www.googleapis.com/auth/gmail.modify")

    @property
    def credentials_path(self) -> Path:
        raw_path = Path(settings.gmail_credentials_file)
        if raw_path.is_absolute():
            return raw_path
        return BACKEND_DIR / raw_path

    @property
    def token_path(self) -> Path:
        raw_path = Path(settings.gmail_token_file)
        if raw_path.is_absolute():
            return raw_path
        return BACKEND_DIR / raw_path

    def has_credentials_file(self) -> bool:
        return self.credentials_path.is_file()

    def has_token_file(self) -> bool:
        return self.token_path.is_file()

    def get_credentials(self) -> Optional[Credentials]:
        """Loads and refreshes OAuth2 credentials from token file if available."""
        with self._credential_lock:
            if not self.has_token_file():
                self.last_auth_error = "token_missing"
                return None
            try:
                # Read actual token grants. Supplying requested scopes here can
                # make ungranted permissions appear present in google-auth.
                creds = Credentials.from_authorized_user_file(str(self.token_path))
                if creds and creds.expired:
                    if not creds.refresh_token:
                        self.last_auth_error = "reauthorization_required"
                        return None
                    stat = self.token_path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    if signature == self._permanent_failure_signature:
                        return None
                    if time.monotonic() < self._refresh_retry_after:
                        return None
                    try:
                        creds.refresh(Request())
                        self.save_credentials(creds)
                    except Exception as exc:
                        self.last_auth_error = self._classify_oauth_error(exc)
                        if self.last_auth_error in {"refresh_token_revoked_or_expired", "oauth_client_configuration_error"}:
                            self._permanent_failure_signature = signature
                        else:
                            self._refresh_retry_after = time.monotonic() + 30
                        logger.warning("Gmail token refresh failed (%s)", self.last_auth_error)
                        return None
                if creds and creds.valid:
                    self.last_auth_error = None
                    self._permanent_failure_signature = None
                    self._refresh_retry_after = 0.0
                    return creds
                self.last_auth_error = "reauthorization_required"
                return None
            except Exception as exc:
                self.last_auth_error = self._classify_oauth_error(exc)
                logger.warning("Gmail credentials could not be loaded (%s)", self.last_auth_error)
                return None

    @staticmethod
    def _classify_oauth_error(exc: Exception) -> str:
        message = str(exc).lower()
        response_status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            response_status = int(response_status) if response_status is not None else None
        except (TypeError, ValueError):
            response_status = None
        if "invalid_grant" in message:
            return "refresh_token_revoked_or_expired"
        if "invalid_client" in message or "unauthorized_client" in message:
            return "oauth_client_configuration_error"
        if "redirect_uri_mismatch" in message:
            return "oauth_redirect_uri_mismatch"
        if "scope" in message or "insufficient" in message:
            return "required_permissions_missing"
        if "accessnotconfigured" in message or "api has not been used" in message:
            return "gmail_api_unavailable"
        if response_status == 403:
            return "gmail_api_unavailable"
        if response_status and response_status >= 500:
            return "gmail_api_unavailable"
        if response_status == 401:
            return "reauthorization_required"
        if "timeout" in message or "connection" in message:
            return "oauth_network_error"
        return "oauth_refresh_failed"

    @staticmethod
    def _authorization_error_message(code: str | None) -> str | None:
        """Human-readable OAuth diagnostics with no provider response/token data."""
        messages = {
            "oauth_network_error": "Could not reach Google OAuth over HTTPS. Check outbound DNS, TLS, firewall, or proxy access; the stored credentials were not rejected.",
            "oauth_client_configuration_error": "Google rejected the OAuth client configuration. Verify the client type, client ID/secret pair, and redirect URI in Google Cloud Console.",
            "oauth_redirect_uri_mismatch": "Google rejected the callback URI. It must exactly match the redirect URI configured for this OAuth client.",
            "refresh_token_revoked_or_expired": "The Gmail refresh token was revoked or expired. Reconnect Gmail to authorize this account again.",
            "reauthorization_required": "Gmail authorization must be renewed. Reconnect the account and grant both requested permissions.",
            "required_permissions_missing": "The saved Gmail grant is missing a required permission. Reconnect and approve gmail.modify and gmail.send.",
            "gmail_api_unavailable": "The Gmail API request was rejected. Check that Gmail API is enabled for the OAuth client’s Google Cloud project.",
            "profile_verification_failed": "Google did not return a verifiable Gmail account profile.",
            "token_missing": "No saved Gmail authorization is present. Connect a Gmail account.",
        }
        return messages.get(code) if code else None

    def save_credentials(self, creds: Credentials) -> None:
        """Saves authorized user credentials to the configured token path."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="gmail-token-", suffix=".tmp", dir=self.token_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, self.token_path)
            self._permanent_failure_signature = None
            self._refresh_retry_after = 0.0
            self.last_auth_error = None
            self._profile_cache_signature = None
            self._profile_cache_email = None
            self._profile_cache_error = None
            self._profile_cache_api_available = None
            self._profile_cache_until = 0.0
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def is_authorized(self) -> bool:
        """Checks a valid token and the actual required permission grants."""
        creds = self.get_credentials()
        granted = set(creds.scopes or []) if creds else set()
        return bool(creds and creds.valid and all(
            any(required in scope or scope == "https://mail.google.com/" for scope in granted)
            for required in self.scopes
        ))

    def get_authorization_status(self) -> Dict[str, Any]:
        """Returns safe inspection of Gmail integration without leaking secrets."""
        has_creds = self.has_credentials_file()
        has_tok = self.has_token_file()
        creds = self.get_credentials()
        is_auth = creds is not None and creds.valid

        connected_email = None
        granted_scopes = []
        gmail_api_available: bool | None = None
        if is_auth:
            granted_scopes = list(creds.scopes) if creds.scopes else []
            stat = self.token_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self._profile_cache_signature and time.monotonic() < self._profile_cache_until:
                connected_email = self._profile_cache_email
                gmail_api_available = self._profile_cache_api_available
                if self._profile_cache_error:
                    self.last_auth_error = self._profile_cache_error
            else:
                try:
                    service = self.get_client()
                    if service:
                        profile = service.users().getProfile(userId="me").execute()
                        gmail_api_available = True
                        candidate_email = profile.get("emailAddress")
                        if isinstance(candidate_email, str) and candidate_email.strip():
                            connected_email = candidate_email.strip().lower()
                    self._profile_cache_email = connected_email
                    self._profile_cache_error = None if connected_email else "profile_verification_failed"
                except Exception as e:
                    gmail_api_available = False
                    self._profile_cache_error = self._classify_oauth_error(e)
                    self.last_auth_error = self._profile_cache_error
                    logger.warning("Gmail profile verification failed (%s)", self.last_auth_error)
                self._profile_cache_api_available = gmail_api_available
                self._profile_cache_signature = signature
                self._profile_cache_until = time.monotonic() + (60 if connected_email else 10)

        # Check required scopes
        has_send = any("gmail.send" in s or "mail.google.com" in s for s in granted_scopes)
        has_modify = any("gmail.modify" in s or "mail.google.com" in s for s in granted_scopes)
        has_required_scopes = has_send and has_modify

        if not has_tok:
            connection_state = "not_connected"
        elif not is_auth:
            connection_state = (
                "authorization_expired"
                if self.last_auth_error in {"refresh_token_revoked_or_expired", "reauthorization_required"}
                else (self.last_auth_error or "authorization_required")
            )
        elif not has_required_scopes:
            connection_state = "permission_missing"
        elif not connected_email:
            connection_state = self._profile_cache_error or self.last_auth_error or "profile_verification_failed"
        else:
            connection_state = "connected"

        return {
            "credentials_file_exists": has_creds,
            "token_file_exists": has_tok,
            "is_authorized": is_auth,
            "connected_email": connected_email,
            "gmail_api_available": gmail_api_available,
            "granted_scopes": granted_scopes,
            "required_scopes": self.scopes,
            "has_send_permission": has_send,
            "has_modify_permission": has_modify,
            "permissions_sufficient": has_required_scopes,
            "reauthorization_required": self.last_auth_error in {"refresh_token_revoked_or_expired", "reauthorization_required", "required_permissions_missing"},
            "authorization_error": self.last_auth_error,
            "authorization_error_message": self._authorization_error_message(self.last_auth_error),
            "connection_state": connection_state,
            "status": connection_state,
        }

    def has_send_permission(self) -> bool:
        """Check whether the authorized credentials grant gmail.send scope."""
        status = self.get_authorization_status()
        return bool(status.get("has_send_permission"))

    def create_oauth_flow(self, redirect_uri: str = "http://localhost:8000/api/gmail/oauth/callback", state: str | None = None, code_verifier: str | None = None) -> Flow:
        """Creates an OAuth flow instance using client credentials."""
        if not self.has_credentials_file():
            raise FileNotFoundError(f"Gmail credentials file not found at {self.credentials_path}")

        try:
            client_config = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("The Gmail OAuth client file is unreadable or invalid JSON.") from exc
        web_config = client_config.get("web")
        installed_config = client_config.get("installed")
        if isinstance(web_config, dict):
            authorized_redirects = web_config.get("redirect_uris", [])
            if redirect_uri not in authorized_redirects:
                raise ValueError(
                    "GMAIL_REDIRECT_URI is not listed in this Web OAuth client's authorized redirect URIs. "
                    "Add the exact configured URI in Google Cloud Console."
                )
        elif isinstance(installed_config, dict):
            raise ValueError(
                "This is a Desktop OAuth client, which uses Google's local loopback authorization flow. "
                "The Settings callback is a Web-server redirect and cannot use this client. "
                "To reconnect with the existing Desktop client, run `python scripts/authorize_gmail.py`; "
                "it uses the configured credentials and existing token path."
            )
        else:
            raise ValueError(
                "The OAuth client file must contain a Google Web or Desktop application client."
            )

        # Pass the already parsed Web client configuration directly. This keeps
        # validation and flow construction tied to the same JSON structure and
        # supports the `web` object emitted by Google Cloud's Web application
        # OAuth client download.
        flow = Flow.from_client_config(
            client_config,
            scopes=self.scopes,
            redirect_uri=redirect_uri,
            state=state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=code_verifier is None,
        )
        return flow

    def get_authorization_url(self, redirect_uri: str = "http://localhost:8000/api/gmail/oauth/callback") -> Tuple[str, str, str]:
        """Generates Google authorization URL, signed state, and PKCE verifier."""
        payload = json.dumps({"nonce": secrets.token_urlsafe(32), "issued_at": int(time.time())}, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(settings.jwt_secret.encode(), encoded.encode(), hashlib.sha256).digest()
        state = encoded + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")
        flow = self.create_oauth_flow(redirect_uri=redirect_uri, state=state)
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        return auth_url, state, flow.code_verifier

    def complete_oauth_flow(self, code: str, redirect_uri: str = "http://localhost:8000/api/gmail/oauth/callback", state: str | None = None, code_verifier: str | None = None) -> Credentials:
        """Validates state, exchanges the PKCE code, and saves tokens."""
        try:
            encoded, signature_text = (state or "").split(".", 1)
            signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
            expected = hmac.new(settings.jwt_secret.encode(), encoded.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("bad signature")
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if time.time() - int(payload["issued_at"]) > 600 or time.time() < int(payload["issued_at"]):
                raise ValueError("expired")
        except Exception:
            raise ValueError("OAuth state is invalid, expired, or does not match the redirect URI. Restart Gmail authorization.")
        if not code_verifier:
            raise ValueError("OAuth PKCE verification data is missing. Restart Gmail authorization.")
        flow = self.create_oauth_flow(redirect_uri=redirect_uri, state=state, code_verifier=code_verifier)
        flow.fetch_token(code=code)
        creds = flow.credentials
        granted = set(creds.scopes or [])
        if not all(any(required in scope or scope == "https://mail.google.com/" for scope in granted) for required in self.scopes):
            raise PermissionError("Google did not grant all required Gmail permissions. Reauthorize and approve the requested permissions.")
        if not creds.refresh_token:
            raise PermissionError(
                "Google did not return a refresh token. Restart authorization and approve offline access with consent."
            )
        self.save_credentials(creds)
        self.last_auth_error = None
        return creds

    def get_client(self) -> Optional[Resource]:
        """Builds an authenticated Gmail API client service."""
        creds = self.get_credentials()
        if not creds:
            return None
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def fetch_incoming_emails(self, query: str = "is:unread", max_results: int = 10) -> List[Dict[str, Any]]:
        """
        Retrieves incoming customer emails matching search query.
        Returns normalized list of parsed email payloads.
        """
        service = self.get_client()
        if not service:
            raise RuntimeError("Gmail client is not authorized. Complete Gmail OAuth first.")

        try:
            results = service.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_results,
            ).execute()
        except Exception as e:
            logger.error("Failed to list messages from Gmail API (%s).", type(e).__name__)
            raise

        messages_meta = results.get("messages", [])
        parsed_emails = []

        for m_meta in messages_meta:
            msg_id = m_meta["id"]
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=msg_id,
                    format="full",
                ).execute()
                parsed = self._parse_message(msg)
                if parsed:
                    parsed_emails.append(parsed)
            except Exception as e:
                logger.error("Error fetching Gmail message (%s).", type(e).__name__)

        return parsed_emails

    def _parse_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        msg_id = msg.get("id")
        thread_id = msg.get("threadId")
        payload = msg.get("payload", {})
        headers_list = payload.get("headers", [])
        headers = {h["name"].lower(): h["value"] for h in headers_list if "name" in h and "value" in h}

        sender_raw = headers.get("from", "")
        sender_name, sender_email = email.utils.parseaddr(sender_raw)
        recipient_raw = headers.get("to", "")
        _, recipient_email = email.utils.parseaddr(recipient_raw)
        subject = headers.get("subject", "(No Subject)")
        message_id_header = headers.get("message-id", "")
        references = headers.get("references", "")

        body_text, body_html = self._extract_body(payload)

        return {
            "id": msg_id,
            "thread_id": thread_id,
            "message_id_header": message_id_header,
            "references": references,
            "sender_raw": sender_raw,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "recipient_email": recipient_email,
            "subject": subject,
            "body_text": body_text.strip(),
            "body_html": body_html,
            "snippet": msg.get("snippet", ""),
        }

    def _extract_body(self, payload: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        """Extracts text/plain and text/html bodies from payload parts."""
        text_body = ""
        html_body = None

        def _walk_parts(part: Dict[str, Any]):
            nonlocal text_body, html_body
            mime_type = part.get("mimeType", "")
            data_b64 = part.get("body", {}).get("data")
            if data_b64:
                try:
                    decoded = base64.urlsafe_b64decode(data_b64).decode("utf-8", errors="replace")
                    if mime_type == "text/plain" and not text_body:
                        text_body = decoded
                    elif mime_type == "text/html" and not html_body:
                        html_body = decoded
                except Exception:
                    pass

            for subpart in part.get("parts", []):
                _walk_parts(subpart)

        _walk_parts(payload)
        return text_body, html_body

    def send_reply(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Sends an RFC 2822 email through the Gmail API attached to the original thread.
        Maintains In-Reply-To, References, and threadId for perfect Gmail threading.
        """
        service = self.get_client()
        if not service:
            raise RuntimeError("Gmail client is not authorized. Cannot send email.")

        profile = service.users().getProfile(userId="me").execute()
        from_email = profile.get("emailAddress", "support@smarttech.local")

        msg = PyEmailMessage()
        msg["To"] = to_email
        msg["From"] = from_email
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to
        msg.set_content(body_text)

        raw_bytes = msg.as_bytes()
        raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

        payload: Dict[str, Any] = {"raw": raw_b64}
        if thread_id:
            payload["threadId"] = thread_id

        try:
            sent_msg = service.users().messages().send(userId="me", body=payload).execute()
            logger.info(f"Email sent via Gmail API: id={sent_msg.get('id')}, threadId={sent_msg.get('threadId')}")
            return {
                "id": sent_msg.get("id"),
                "thread_id": sent_msg.get("threadId"),
                "to": to_email,
                "from": from_email,
                "subject": subject,
            }
        except Exception as e:
            logger.error("Gmail send failed (%s)", type(e).__name__)
            raise

    def create_draft_for_ticket(
        self,
        ticket_id: int,
        recipient: str,
        subject: str,
        body: str,
        thread_id: Optional[str] = None,
        ticket_number: str = "",
    ) -> Dict[str, Any]:
        """
        Create a Gmail draft attached to the conversation thread for review.
        """
        service = self.get_client()
        if not service:
            return {}

        try:
            profile = service.users().getProfile(userId="me").execute()
            from_email = profile.get("emailAddress", "support@smarttech.local")

            msg = PyEmailMessage()
            msg["To"] = recipient
            msg["From"] = from_email
            msg["Subject"] = subject
            msg.set_content(body)

            raw_bytes = msg.as_bytes()
            raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

            msg_payload: Dict[str, Any] = {"raw": raw_b64}
            if thread_id:
                msg_payload["threadId"] = thread_id

            draft = service.users().drafts().create(
                userId="me",
                body={"message": msg_payload},
            ).execute()
            logger.info("Gmail draft created for ticket %s: id=%s", ticket_id, draft.get("id"))
            return draft
        except Exception as e:
            logger.warning("Could not sync draft to Gmail API (%s).", type(e).__name__)
            return {}

    def reconcile_sent_message(
        self,
        to_email: str,
        subject: str,
        thread_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Reconcile whether a reply was already dispatched via Gmail to avoid duplicate sends
        following timeouts or ambiguous network responses.
        """
        service = self.get_client()
        if not service:
            raise RuntimeError("Gmail API client unavailable during send reconciliation.")
        try:
            q = f"to:{to_email} in:sent"
            if thread_id:
                q += f" thread:{thread_id}"
            results = service.users().messages().list(userId="me", q=q, maxResults=5).execute()
            messages = results.get("messages", [])
            for m in messages:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject", "To"])
                    .execute()
                )
                headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
                if headers.get("subject", "").strip().lower() == subject.strip().lower():
                    logger.info("Reconciled an existing sent message after an ambiguous send result.")
                    return {
                        "id": m["id"],
                        "thread_id": msg.get("threadId"),
                        "to": to_email,
                        "subject": subject,
                    }
        except Exception as e:
            logger.warning("Gmail reconciliation lookup failed (%s)", type(e).__name__)
            raise RuntimeError("Gmail send reconciliation could not be completed.") from e
        return None


gmail_service = GmailService()
