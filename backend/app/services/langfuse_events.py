"""Best-effort low-PII Langfuse events for human approval actions."""
from app.core.logging import logger


def record_approval_event(
    name: str,
    actor_role: str,
    *,
    db=None,
    ticket_id: int | None = None,
) -> None:
    try:
        from app.services.agents.tracing import get_langfuse_client, get_ticket_trace_context
        client = get_langfuse_client()
        if not client:
            return
        context = get_ticket_trace_context(db, ticket_id)
        client.create_event(
            trace_context=context,
            name=name,
            metadata={
                "actor_role": actor_role,
                "source": "dashboard",
                "trace_context_available": context is not None,
            },
        )
        client.flush()
    except Exception as exc:
        logger.warning("Langfuse approval event failed (%s)", type(exc).__name__)
