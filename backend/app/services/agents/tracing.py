"""Component 7: Tracing - End-to-end execution path monitoring with correlation IDs, timing, and Langfuse observability."""
from datetime import datetime, timezone
import json
import re
import time
from dataclasses import dataclass
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.models.agent import AgentExecution, ToolExecution
from app.services.langchain_workflow.privacy_callback import PrivacySafeLangfuseCallbackHandler

try:
    from langfuse import Langfuse
    from langfuse import propagate_attributes
    from langfuse.langchain import CallbackHandler as LangchainCallbackHandler
except ImportError:
    Langfuse = None  # type: ignore[assignment, misc]
    propagate_attributes = None  # type: ignore[assignment, misc]
    LangchainCallbackHandler = None  # type: ignore[assignment, misc]


_global_langfuse_client: Any = None
_ticket_trace_contexts: dict[int, dict[str, str]] = {}


def _remember_ticket_trace_context(ticket_id: int, context: dict[str, str]) -> None:
    _ticket_trace_contexts[ticket_id] = dict(context)
    while len(_ticket_trace_contexts) > 2048:
        _ticket_trace_contexts.pop(next(iter(_ticket_trace_contexts)))


def get_langfuse_client() -> Any:
    """Safely obtain or initialize singleton Langfuse client."""
    global _global_langfuse_client
    if _global_langfuse_client is not None:
        return _global_langfuse_client

    if not settings.langfuse_configured or Langfuse is None:
        return None

    try:
        _global_langfuse_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_base_url,
        )
        return _global_langfuse_client
    except Exception as exc:
        logger.warning("Could not initialize Langfuse client (%s)", type(exc).__name__)
        return None


@dataclass
class LangfuseTrace:
    """Root observation plus the context needed to attach child observations."""

    client: Any
    root: Any
    trace_id: str
    correlation_id: str
    observation_context: Any = None
    attributes_context: Any = None

    @property
    def trace_context(self) -> dict[str, str]:
        return {"trace_id": self.trace_id, "parent_span_id": self.root.id}

    def start_observation(self, *, name: str, as_type: str = "span", **kwargs: Any) -> Any:
        return self.client.start_observation(
            trace_context=self.trace_context,
            name=name,
            as_type=as_type,
            **kwargs,
        )

    def callback_handler(self, *, parent_span_id: str | None = None) -> Any | None:
        if LangchainCallbackHandler is None:
            return None
        try:
            context = {"trace_id": self.trace_id}
            parent_id = parent_span_id or self.root.id
            if parent_id:
                context["parent_span_id"] = parent_id
            return PrivacySafeLangfuseCallbackHandler(
                public_key=settings.langfuse_public_key,
                trace_context=context,
            )
        except Exception as exc:
            logger.warning("Could not create Langfuse callback (%s)", type(exc).__name__)
            return None

    def create_event(
        self,
        *,
        name: str,
        input: Any = None,
        output: Any = None,
        metadata: Any = None,
        level: str = "DEFAULT",
    ) -> Any:
        return self.client.create_event(
            trace_context=self.trace_context,
            name=name,
            input=input,
            output=output,
            metadata=metadata,
            level=level,
        )

    def finish(self, *, status: str, output: dict[str, Any], duration_ms: int | None = None) -> None:
        try:
            safe_output = dict(output)
            metadata = {"status": status}
            if duration_ms is not None:
                metadata["duration_ms"] = max(0, int(duration_ms))
            self.root.update(
                output=safe_output,
                metadata=metadata,
                level="ERROR" if status == "error" else "DEFAULT",
            )
            self.root.end()
        except Exception as exc:
            logger.warning("Could not complete Langfuse root observation (%s)", type(exc).__name__)
        finally:
            for context in (self.attributes_context, self.observation_context):
                if context is not None:
                    try:
                        context.__exit__(None, None, None)
                    except Exception as exc:
                        logger.warning("Could not close Langfuse trace context (%s)", type(exc).__name__)
            flush_langfuse(self.client)


def start_langfuse_trace(
    *,
    name: str,
    correlation_id: str | None = None,
    source: str,
    workflow: str,
    input_summary: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> LangfuseTrace | None:
    """Start one privacy-safe root observation for an application workflow."""

    client = get_langfuse_client()
    if client is None:
        return None
    correlation_id = correlation_id or uuid.uuid4().hex
    try:
        root_context = None
        attributes_context = None
        root_options = {
            "trace_context": {"trace_id": correlation_id},
            "name": name,
            "as_type": "agent",
            "input": input_summary or {},
            "metadata": {"source": source, "workflow": workflow},
        }
        start_current = getattr(client, "start_as_current_observation", None)
        if callable(start_current):
            root_context = start_current(**root_options, end_on_exit=False)
            root = root_context.__enter__()
            if propagate_attributes is not None:
                attributes_context = propagate_attributes(
                    trace_name=name,
                    tags=tags or [],
                    as_baggage=True,
                )
                attributes_context.__enter__()
        else:
            root = client.start_observation(**root_options)
        trace = LangfuseTrace(
            client=client,
            root=root,
            trace_id=str(root.trace_id),
            correlation_id=correlation_id,
            observation_context=root_context,
            attributes_context=attributes_context,
        )
        return trace
    except Exception as exc:
        for context in (locals().get("attributes_context"), locals().get("root_context")):
            if context is not None:
                try:
                    context.__exit__(None, None, None)
                except Exception:
                    pass
        logger.warning("Could not start Langfuse root observation (%s)", type(exc).__name__)
        return None


def flush_langfuse(client: Any | None = None) -> None:
    """Flush at workflow boundaries without allowing tracing to block support."""

    active_client = client or get_langfuse_client()
    if active_client is None:
        return
    try:
        active_client.flush()
    except Exception as exc:
        logger.warning("Langfuse flush failed (%s)", type(exc).__name__)


def get_ticket_trace_context(db: Session | None, ticket_id: int | None) -> dict[str, str] | None:
    """Find a safe trace context for later approval events, if it is available."""

    if ticket_id is None:
        return None
    if ticket_id in _ticket_trace_contexts:
        return dict(_ticket_trace_contexts[ticket_id])
    if db is None:
        return None
    try:
        execution = db.scalar(
            select(AgentExecution)
            .where(AgentExecution.ticket_id == ticket_id)
            .order_by(AgentExecution.created_at.desc())
            .limit(1)
        )
        summary = execution.reasoning_summary if execution else None
        if summary:
            marker = re.search(r"\[langfuse_trace_id:([0-9a-f]{32})\]", summary)
            if marker:
                return {"trace_id": marker.group(1)}
            try:
                diagnostics = json.loads(summary)
            except (TypeError, ValueError):
                diagnostics = None
            if isinstance(diagnostics, dict):
                trace_id = diagnostics.get("langfuse_trace_id")
                parent_span_id = diagnostics.get("langfuse_root_span_id")
                if isinstance(trace_id, str) and re.fullmatch(r"[0-9a-f]{32}", trace_id):
                    context = {"trace_id": trace_id}
                    if isinstance(parent_span_id, str) and re.fullmatch(r"[0-9a-f]{16}", parent_span_id):
                        context["parent_span_id"] = parent_span_id
                    return context
    except Exception as exc:
        logger.warning("Could not load Langfuse approval context (%s)", type(exc).__name__)
    return None


class AgentExecutionTracer:
    """Observability tracer writing to PostgreSQL and synchronizing with Langfuse."""

    def __init__(self, db: Session):
        self.db = db
        self.langfuse = get_langfuse_client()
        self._lf_spans: dict[int, Any] = {}
        self._lf_trace_ids: dict[int, str] = {}
        self._lf_trace_contexts: dict[int, dict[str, str]] = {}
        self._active_execution_by_ticket: dict[int, int] = {}
        self._execution_started_at: dict[int, float] = {}

    def start_trace(self, ticket_id: int) -> tuple[int, str]:
        """Initialize an observability trace for an agent run with a unique correlation ID."""
        correlation_id = uuid.uuid4().hex
        execution = AgentExecution(
            ticket_id=ticket_id,
            status="running",
            reasoning_summary=f"Trace ID: {correlation_id}",
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(execution)
        self.db.flush()
        self._active_execution_by_ticket[ticket_id] = execution.id
        self._execution_started_at[execution.id] = time.perf_counter()

        trace = start_langfuse_trace(
            name="smarttech_support_workflow",
            correlation_id=correlation_id,
            source="support_orchestrator",
            workflow="agent_ticket_execution",
            input_summary={"ticket_present": True},
        )
        if trace:
            context = trace.trace_context
            self._lf_spans[execution.id] = trace.root
            self._lf_trace_ids[execution.id] = trace.trace_id
            self._lf_trace_contexts[execution.id] = context
            _remember_ticket_trace_context(ticket_id, context)

        return execution.id, correlation_id

    def get_langchain_callback(self, execution_id: int) -> Any | None:
        """Create a version-compatible Langfuse callback handler for LangChain agent runs."""
        if not self.langfuse or LangchainCallbackHandler is None:
            return None

        trace_id = self._lf_trace_ids.get(execution_id)
        try:
            if trace_id:
                return PrivacySafeLangfuseCallbackHandler(
                    public_key=settings.langfuse_public_key,
                    trace_context=self._lf_trace_contexts.get(
                        execution_id,
                        {"trace_id": trace_id},
                    ),
                )
            return PrivacySafeLangfuseCallbackHandler(public_key=settings.langfuse_public_key)
        except Exception as exc:
            logger.warning("Could not create LangChain Langfuse callback (%s)", type(exc).__name__)
            return None

    def record_step(
        self,
        execution_id: int,
        step_name: str,
        input_params: dict[str, Any] | None = None,
        output_result: Any = None,
        status: str = "success",
        duration_ms: int = 0,
    ) -> None:
        """Record an individual execution step or tool call into the trace log."""
        safe_inputs = self._sanitize_data(input_params) if input_params else {}
        safe_outputs = self._sanitize_data(output_result) if output_result else {}

        tool_exec = ToolExecution(
            agent_execution_id=execution_id,
            tool_name=step_name,
            input_parameters=json.dumps(safe_inputs)[:2000],
            output_result=json.dumps(safe_outputs)[:4000],
            status=status,
            duration_ms=duration_ms,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(tool_exec)
        self.db.flush()

        # Log observation to Langfuse if available
        if self.langfuse and execution_id in self._lf_trace_ids:
            try:
                self.langfuse.create_event(
                    trace_context=self._lf_trace_contexts.get(
                        execution_id,
                        {"trace_id": self._lf_trace_ids[execution_id]},
                    ),
                    name=step_name,
                    input=safe_inputs,
                    output=safe_outputs,
                    level="ERROR" if status == "error" else "DEFAULT",
                    metadata={"duration_ms": max(0, int(duration_ms)), "status": status},
                )
            except Exception as exc:
                logger.warning("Langfuse step event failed (%s)", type(exc).__name__)

    def complete_trace(
        self,
        execution_id: int,
        draft_id: int | None,
        intent: str,
        category: str,
        priority: str,
        escalation_recommended: bool,
        confidence_score: float,
        reasoning_summary: str,
        total_time_ms: int,
        status: str = "completed",
        error_message: str | None = None,
    ) -> None:
        """Finalize agent execution trace with overall metrics."""
        execution = self.db.get(AgentExecution, execution_id)
        trace_id = self._lf_trace_ids.get(execution_id)
        if trace_id:
            if execution:
                _remember_ticket_trace_context(
                    execution.ticket_id,
                    self._lf_trace_contexts.get(execution_id, {"trace_id": trace_id}),
                )
        if execution:
            execution.draft_id = draft_id
            execution.status = status
            execution.user_intent = intent[:255] if intent else None
            execution.issue_category = category[:100] if category else None
            execution.priority_recommendation = priority[:50] if priority else None
            execution.escalation_recommended = escalation_recommended
            execution.confidence_score = confidence_score
            execution.reasoning_summary = (
                f"[langfuse_trace_id:{trace_id}]\n{reasoning_summary or ''}"
                if trace_id else reasoning_summary
            )
            execution.error_message = error_message
            execution.execution_time_ms = total_time_ms
            self.db.flush()

        # Finalize Langfuse span if active
        root_span = self._lf_spans.pop(execution_id, None)

        if root_span:
            try:
                root_span.update(
                    output={
                        "status": status,
                        "intent": intent,
                        "category": category,
                        "priority": priority,
                        "escalation_recommended": escalation_recommended,
                        "confidence_score": confidence_score,
                    },
                    metadata={
                        "reasoning_summary_redacted": bool(reasoning_summary),
                        "error_occurred": bool(error_message),
                        "total_time_ms": total_time_ms,
                    },
                )
                root_span.end()
                flush_langfuse(self.langfuse)
            except Exception as exc:
                logger.warning("Langfuse trace completion failed (%s)", type(exc).__name__)
        self._lf_trace_ids.pop(execution_id, None)
        self._lf_trace_contexts.pop(execution_id, None)
        if execution:
            self._active_execution_by_ticket.pop(execution.ticket_id, None)
        self._execution_started_at.pop(execution_id, None)

    def fail_active_trace(self, ticket_id: int, error: Exception) -> None:
        """End an orchestrator trace when any non-LLM workflow stage fails."""

        execution_id = self._active_execution_by_ticket.pop(ticket_id, None)
        if execution_id is None:
            return
        execution = self.db.get(AgentExecution, execution_id)
        trace_id = self._lf_trace_ids.get(execution_id)
        started = self._execution_started_at.pop(execution_id, None)
        duration_ms = (
            max(0, int((time.perf_counter() - started) * 1000))
            if started is not None else 0
        )
        if execution:
            execution.status = "failed"
            execution.error_message = f"Agent workflow failed ({type(error).__name__})."
            execution.execution_time_ms = duration_ms
            if trace_id:
                execution.reasoning_summary = f"[langfuse_trace_id:{trace_id}]"
            try:
                self.db.flush()
            except Exception:
                logger.warning("Could not persist failed agent execution status")
        root = self._lf_spans.pop(execution_id, None)
        self._lf_trace_ids.pop(execution_id, None)
        self._lf_trace_contexts.pop(execution_id, None)
        if root:
            try:
                root.update(
                    output={"status": "failed", "error_type": type(error).__name__},
                    metadata={"duration_ms": duration_ms, "error_occurred": True},
                    level="ERROR",
                )
                root.end()
                flush_langfuse(self.langfuse)
            except Exception as exc:
                logger.warning("Langfuse failed-trace completion failed (%s)", type(exc).__name__)

    def _sanitize_data(self, data: Any) -> Any:
        """Redact customer content and identifiers before external or local tracing."""
        if isinstance(data, dict):
            sanitized = {}
            for k, v in data.items():
                lower_k = str(k).lower()
                sensitive_fragments = (
                    "password", "token", "key", "secret", "auth", "credential",
                    "email", "phone", "address", "subject", "body", "message",
                    "content", "query", "serial", "tracking", "customer_id", "order",
                    "ticket_id", "order_id", "device_id", "user_id", "reasoning",
                    "customer_name", "full_name", "display_name", "error", "recipient",
                    "sender", "summary", "document_content", "account", "bank", "payment",
                )
                if lower_k == "name" or any(fragment in lower_k for fragment in sensitive_fragments):
                    sanitized[k] = "[REDACTED]"
                else:
                    sanitized[k] = self._sanitize_data(v)
            return sanitized
        elif isinstance(data, list):
            return [self._sanitize_data(x) for x in data]
        elif isinstance(data, str):
            data = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", data)
            data = re.sub(r"\b(?:\+?\d[\d .()-]{7,}\d)\b", "[REDACTED_PHONE]", data)
            return re.sub(
                r"\b(?:gsk_[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{12,}|"
                r"Bearer\s+[A-Za-z0-9._-]{12,})\b",
                "[REDACTED_SECRET]",
                data,
                flags=re.I,
            )
        return data
