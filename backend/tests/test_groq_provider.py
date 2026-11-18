"""Groq configuration, availability, and shared-client safeguards."""
import httpx
import pytest

from app.integrations.llm import llm_service
from app.core.config import settings


@pytest.fixture(autouse=True)
def reset_model_cache():
    llm_service._model_cache.clear()
    yield
    llm_service._model_cache.clear()


def configure(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_unit_test_secret")
    monkeypatch.setattr(settings, "groq_model", "openai/gpt-oss-20b")
    monkeypatch.setattr(settings, "groq_base_url", "https://api.groq.com/openai/v1")


def test_groq_and_embedding_environment_defaults(monkeypatch):
    configure(monkeypatch)
    assert settings.groq_model == "openai/gpt-oss-20b"
    assert settings.groq_base_url == "https://api.groq.com/openai/v1"
    assert settings.embedding_provider == "huggingface"
    assert settings.embedding_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert settings.embedding_dimension == 384


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited"), (503, "provider_error")],
)
def test_model_availability_reports_provider_http_errors_without_secrets(monkeypatch, status_code, expected):
    configure(monkeypatch)
    monkeypatch.setattr(llm_service.httpx, "get", lambda *_args, **_kwargs: type("Response", (), {"status_code": status_code})())
    status = llm_service.groq_model_status(force_refresh=True)
    assert status["status"] == expected
    assert status["http_status"] == status_code
    assert "gsk_unit_test_secret" not in str(status)


def test_model_availability_rejects_missing_model_and_malformed_response(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(llm_service.httpx, "get", lambda *_args, **_kwargs: type("Response", (), {"status_code": 200, "json": lambda _self: []})())
    status = llm_service.groq_model_status(force_refresh=True)
    assert status["status"] == "invalid_response"


def test_model_availability_timeout_is_clear_and_secret_free(monkeypatch):
    configure(monkeypatch)
    def timeout(*_args, **_kwargs):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(llm_service.httpx, "get", timeout)
    status = llm_service.groq_model_status(force_refresh=True)
    assert status["status"] == "timeout"
    assert "gsk_unit_test_secret" not in str(status)


def test_chat_model_factory_uses_only_configured_groq_endpoint(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(llm_service, "ensure_groq_model_available", lambda **_kwargs: {"status": "available"})
    captured = {}
    monkeypatch.setattr(llm_service, "ChatOpenAI", lambda **kwargs: captured.update(kwargs) or object())
    llm_service.get_llm()
    assert captured["model"] == "openai/gpt-oss-20b"
    assert captured["base_url"] == "https://api.groq.com/openai/v1"
    assert captured["api_key"] == "gsk_unit_test_secret"
    assert "openrouter" not in captured["base_url"]
