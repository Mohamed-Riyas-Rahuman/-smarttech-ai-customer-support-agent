"""Single Groq model factory and safe model-availability check."""
import time
from typing import Optional

import httpx
from langchain_openai import ChatOpenAI
from langchain_core.language_models.chat_models import BaseChatModel

from app.core.config import settings
from app.core.logging import logger

_MODEL_CACHE_TTL_SECONDS = 300
_model_cache: dict[str, tuple[float, dict]] = {}


def groq_model_status(*, force_refresh: bool = False) -> dict:
    """Check key validity and model availability using Groq's models endpoint."""
    api_key = settings.groq_api_key.strip()
    model = settings.groq_model.strip()
    if not api_key or api_key.startswith("CHANGE_ME"):
        return {"status": "unconfigured", "model": model, "message": "GROQ_API_KEY is not configured."}
    if not model:
        return {"status": "model_unconfigured", "message": "GROQ_MODEL is empty."}

    cache_key = settings.groq_base_url.rstrip("/") + ":" + model
    cached = _model_cache.get(cache_key)
    now = time.monotonic()
    if cached and not force_refresh and now - cached[0] < _MODEL_CACHE_TTL_SECONDS:
        return dict(cached[1])

    try:
        response = httpx.get(
            f"{settings.groq_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
    except httpx.TimeoutException:
        result = {"status": "timeout", "model": model, "message": "Groq model availability check timed out."}
    except httpx.ConnectError as exc:
        result = {
            "status": "connection_failed", "model": model,
            "error_type": type(exc).__name__,
            "message": "Could not reach api.groq.com over HTTPS. Check outbound DNS, TLS, firewall, or proxy access.",
        }
    except httpx.HTTPError as exc:
        result = {
            "status": "connection_failed", "model": model,
            "error_type": type(exc).__name__,
            "message": "Groq model availability check failed. Check outbound HTTPS connectivity and TLS configuration.",
        }
    else:
        if response.status_code in (401, 403):
            result = {"status": "authentication_failed", "model": model, "http_status": response.status_code, "message": f"Groq rejected the configured API key or its permissions (HTTP {response.status_code})."}
        elif response.status_code == 429:
            result = {"status": "rate_limited", "model": model, "http_status": 429, "message": "Groq rate limited the model availability check."}
        elif response.status_code != 200:
            result = {"status": "provider_error", "model": model, "http_status": response.status_code, "message": f"Groq model availability check returned HTTP {response.status_code}."}
        else:
            try:
                available_ids = {item.get("id") for item in response.json().get("data", []) if isinstance(item, dict)}
            except (AttributeError, TypeError, ValueError):
                result = {"status": "invalid_response", "model": model, "message": "Groq returned an invalid models response."}
            else:
                if model in available_ids:
                    result = {"status": "available", "model": model, "message": "Configured Groq model is available."}
                else:
                    result = {"status": "model_unavailable", "model": model, "message": "Configured Groq model was not listed for this API account."}

    # Avoid keeping transient network/provider failures on the dashboard for
    # the full cache interval. A recovered connection is checked next request.
    if result["status"] not in {"connection_failed", "timeout", "provider_error", "rate_limited"}:
        _model_cache[cache_key] = (now, result)
    return dict(result)


def ensure_groq_model_available(*, force_refresh: bool = False) -> dict:
    status = groq_model_status(force_refresh=force_refresh)
    if status["status"] != "available":
        raise RuntimeError(status["message"])
    return status


def get_llm(
    temperature: float = 0.2,
    model_name: Optional[str] = None,
    max_tokens: int = 1200,
) -> BaseChatModel:
    """Construct the shared Groq-backed LangChain chat model."""
    api_key = settings.groq_api_key.strip()
    if not api_key or api_key.startswith("CHANGE_ME"):
        raise RuntimeError("Groq is not configured. Set GROQ_API_KEY before generating replies.")
    selected_model = (model_name or settings.groq_model).strip()
    if selected_model != settings.groq_model:
        # No workflow may silently select an unconfigured alternate model.
        raise RuntimeError("Requested model does not match configured GROQ_MODEL.")
    ensure_groq_model_available()
    logger.debug("Initializing Groq chat model=%s", selected_model)
    return ChatOpenAI(
        model=selected_model,
        api_key=api_key,
        base_url=settings.groq_base_url.rstrip("/"),
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=45,
        max_retries=1,
    )


def is_llm_configured() -> bool:
    """Return whether Groq credentials and a model ID are configured."""
    key = settings.groq_api_key.strip()
    return bool(key and len(key) > 10 and not key.startswith("CHANGE_ME") and settings.groq_model.strip())
