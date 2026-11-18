"""Persist support workflow outcomes for dashboard and audit reporting."""
import json
from datetime import datetime, timezone
from app.models.agent import AgentExecution


def safe_workflow_error(error: Exception) -> str:
    """Convert provider/runtime exceptions into useful non-secret diagnostics."""
    error_type = type(error).__name__
    provider_message = str(error).lower()
    status_code = getattr(error, "status_code", None) or getattr(error, "status", None)
    if "free-models-per-day" in provider_message or ("free model" in provider_message and "per-day" in provider_message):
        return "The configured LLM provider returned HTTP 429 because its daily free-model request limit was reached. No reply was generated; retry after reset or configure a funded model."
    if "insufficient_quota" in provider_message or "add credits" in provider_message:
        return "The configured LLM provider has no available credits. No reply was generated; restore provider quota before retrying."
    if "rate limit" in provider_message or "too many requests" in provider_message:
        return "The LLM provider rate limit was reached. No reply was generated; retry after the provider limit resets."
    if provider_message.startswith((
        "groq is not configured",
        "groq rejected ",
        "groq model ",
        "configured groq model ",
        "groq rate limited ",
        "groq model availability check ",
    )):
        return str(error)[:240]
    if "APIStatus" in error_type or "OpenAI" in error_type or "HTTPStatus" in error_type:
        status = f", HTTP {status_code}" if status_code else ""
        return f"Groq request failed ({error_type}{status}). Check the configured model, API key permissions, and account quota, then retry."
    if isinstance(error, (TimeoutError,)) or "Timeout" in error_type:
        return f"Response generation timed out ({error_type}). Retry after checking provider availability."
    if isinstance(error, ValueError):
        return f"Response validation failed: {str(error)[:240]}"
    return f"Response generation failed ({error_type}). Check service configuration and retry."


def record_workflow_execution(db, ticket, *, outcome=None, error: Exception | None = None):
    safe_error = safe_workflow_error(error) if error else None
    diagnostics = (
        getattr(error, "workflow_diagnostics", None)
        if error else getattr(outcome, "diagnostics", None)
    )
    summary = json.dumps(diagnostics, ensure_ascii=False) if diagnostics else (
        f"Workflow finished ({outcome.action_taken})." if outcome else None
    )
    execution = AgentExecution(
        ticket_id=ticket.id,
        draft_id=getattr(outcome, "draft_id", None),
        status="failed" if error else "completed",
        user_intent=ticket.subject,
        issue_category=ticket.category,
        priority_recommendation=ticket.priority,
        escalation_recommended=bool(outcome and not outcome.quality_checks_passed),
        confidence_score=getattr(outcome, "confidence_score", None),
        reasoning_summary=summary,
        error_message=safe_error,
        execution_time_ms=(
            getattr(outcome, "execution_duration_ms", None)
            or (diagnostics or {}).get("duration_ms")
        ),
        created_at=datetime.now(timezone.utc),
    )
    db.add(execution)
    return execution
