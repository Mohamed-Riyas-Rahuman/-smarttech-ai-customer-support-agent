"""Focused tests for connected, privacy-safe Langfuse instrumentation."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

from langchain_core.outputs import Generation, LLMResult

from app.services.agents import tracing
from app.services.langchain_workflow.privacy_callback import PrivacySafeLangfuseCallbackHandler


def test_root_and_child_observations_share_trace_context(monkeypatch):
    root = Mock(id="a" * 16, trace_id="b" * 32)
    client = Mock()
    client.start_as_current_observation = None
    client.start_observation.return_value = root
    monkeypatch.setattr(tracing, "propagate_attributes", None)
    monkeypatch.setattr(tracing, "get_langfuse_client", lambda: client)

    trace = tracing.start_langfuse_trace(
        name="support_workflow",
        correlation_id="c" * 32,
        source="test",
        workflow="test_email",
        input_summary={"body_present": True},
    )
    assert trace is not None
    trace.start_observation(name="retrieval", as_type="retriever", input={"operation": "retrieval"})

    child_call = client.start_observation.call_args_list[-1].kwargs
    assert child_call["trace_context"] == {"trace_id": "b" * 32, "parent_span_id": "a" * 16}
    assert trace.trace_context["trace_id"] == "b" * 32


def test_trace_attributes_are_applied_while_root_is_current(monkeypatch):
    root = Mock(id="a" * 16, trace_id="b" * 32)
    client = Mock()
    root_context = MagicMock()
    root_context.__enter__ = Mock(return_value=root)
    attributes_context = MagicMock()
    attributes_context.__enter__ = Mock(return_value=None)
    client.start_as_current_observation.return_value = root_context
    client.propagate_attributes.return_value = attributes_context
    monkeypatch.setattr(tracing, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(
        tracing,
        "propagate_attributes",
        lambda **kwargs: client.propagate_attributes(**kwargs),
    )

    trace = tracing.start_langfuse_trace(
        name="gmail_support_workflow",
        source="gmail_sync_worker",
        workflow="email_to_draft",
        tags=["gmail", "support-agent", "rag", "groq", "auto-send"],
    )

    assert trace is not None
    client.start_as_current_observation.assert_called_once()
    client.propagate_attributes.assert_called_once_with(
        trace_name="gmail_support_workflow",
        tags=["gmail", "support-agent", "rag", "groq", "auto-send"],
        as_baggage=True,
    )
    trace.finish(status="completed", output={})
    root_context.__exit__.assert_called_once()
    attributes_context.__exit__.assert_called_once()


def test_finish_ends_root_and_flushes(monkeypatch):
    root = Mock(id="a" * 16, trace_id="b" * 32)
    client = Mock()
    trace = tracing.LangfuseTrace(client, root, "b" * 32, "c" * 32)
    trace.finish(status="completed", output={"draft_created": True}, duration_ms=12)
    root.update.assert_called_once()
    root.end.assert_called_once()
    client.flush.assert_called_once()


def test_privacy_callback_preserves_prompt_response_and_usage_while_redacting_secrets():
    callback = object.__new__(PrivacySafeLangfuseCallbackHandler)
    captured = {}
    callback._delegate = lambda method, *args, **kwargs: captured.update(
        method=method, args=args, kwargs=kwargs
    )
    prompt = (
        "System: Help with support. User: Please explain my warranty. "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456 "
        "api_key=gsk_abcdefghijklmnopqrstuvwxyz"
    )
    callback.on_llm_start(
        {"name": "ChatGroq", "id": ["groq"]}, [prompt], run_id=uuid4(),
        invocation_params={"model": "openai/gpt-oss-20b", "api_key": "gsk_abcdefghijklmnopqrstuvwxyz"},
    )
    assert "Please explain my warranty." in repr(captured)
    assert "gsk_abcdefghijklmnopqrstuvwxyz" not in repr(captured)
    assert "abcdefghijklmnopqrstuvwxyz123456" not in repr(captured)
    assert "[REDACTED_SECRET]" in repr(captured)

    callback.on_llm_end(
        LLMResult(
            generations=[[Generation(text="Your warranty lasts for one year.")]],
            llm_output={
                "token_usage": {
                    "input_tokens": 13,
                    "output_tokens": 7,
                    "total_tokens": 20,
                    "prompt_tokens": 13,
                    "completion_tokens": 7,
                    "prompt_tokens_details": {"cached_tokens": 4, "customer_email": "private"},
                },
                "authorization": "Bearer abcdefghijklmnopqrstuvwxyz123456",
            },
        ),
        run_id=uuid4(),
    )
    result = captured["args"][0]
    assert result.generations[0][0].text == "Your warranty lasts for one year."
    assert "abcdefghijklmnopqrstuvwxyz123456" not in repr(captured)
    assert result.llm_output == {
        "token_usage": {
            "input_tokens": 13,
            "output_tokens": 7,
            "total_tokens": 20,
            "prompt_tokens": 13,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 4},
        }
    }


def test_privacy_callback_keeps_chat_messages_visible_and_redacts_customer_identifiers():
    from langchain_core.messages import HumanMessage, SystemMessage

    callback = object.__new__(PrivacySafeLangfuseCallbackHandler)
    captured = {}
    callback._delegate = lambda method, *args, **kwargs: captured.update(method=method, args=args, kwargs=kwargs)
    callback.on_chat_model_start(
        {"name": "ChatGroq"},
        [[SystemMessage(content="Follow support policy."), HumanMessage(
            content="My email is customer@example.test. Please check this issue."
        )]],
        run_id=uuid4(),
    )
    messages = captured["args"][1][0]
    assert messages[0].content == "Follow support policy."
    assert "Please check this issue." in messages[1].content
    assert "customer@example.test" not in messages[1].content
    assert "[REDACTED_EMAIL]" in messages[1].content


def test_approval_events_use_create_event_and_safe_context(monkeypatch):
    from app.services import langfuse_events

    client = Mock()
    context = {"trace_id": "a" * 32, "parent_span_id": "b" * 16}
    monkeypatch.setattr(tracing, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(tracing, "get_ticket_trace_context", lambda _db, _ticket_id: context)

    langfuse_events.record_approval_event("draft_approved", "support_agent", db=object(), ticket_id=4)

    client.create_event.assert_called_once_with(
        trace_context=context,
        name="draft_approved",
        metadata={"actor_role": "support_agent", "source": "dashboard", "trace_context_available": True},
    )
    client.flush.assert_called_once()


def test_tracing_failure_does_not_escape_workflow_boundary():
    client = Mock()
    client.flush.side_effect = RuntimeError("offline")
    trace = tracing.LangfuseTrace(client, Mock(), "a" * 32, "b" * 32)
    trace.finish(status="error", output={"error_type": "RuntimeError"})
    client.flush.assert_called_once()

