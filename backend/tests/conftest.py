"""Keep unit tests offline and prevent an application-owned worker thread."""
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


class _FakeGenerationChain:
    def invoke(self, values):
        subject = values.get("email_subject", "your question")
        evidence = values.get("retrieved_knowledge", "")
        if "No relevant documentation" not in evidence and evidence.strip():
            excerpt = evidence.split("Content:\n", 1)[-1].split("\n----------------------------------------", 1)[0]
            body = f"Hello,\n\nHere is information from the verified SmartTech documentation:\n{excerpt[:700]}\n\nBest regards,\nSmartTech Support Team"
        else:
            body = "Hello,\n\nThank you for contacting SmartTech Support. We have saved your request for a support specialist to review.\n\nBest regards,\nSmartTech Support Team"
        return {"subject": f"Re: {subject}", "body": body}


class _FakeQualityChecker:
    def __init__(self, callbacks=None):
        pass

    def run_all_checks(self, **_kwargs):
        return SimpleNamespace(
            passed=True,
            confidence_score=0.90,
            sentiment=SimpleNamespace(customer_sentiment="neutral"),
            safety=SimpleNamespace(is_grounded=True, policy_compliant=True),
            requires_human_review=True,
            rejection_reasons=[],
        )


@pytest.fixture(autouse=True)
def isolate_external_services(monkeypatch):
    """No unit test may start background sync or make a paid provider request."""
    from app.services import sync_worker
    from app.services.agents import orchestrator
    from app.services.langchain_workflow import workflow
    from app.services.agents import tracing
    from app.integrations.llm import llm_service

    monkeypatch.setattr(sync_worker, "start_worker", lambda: None)
    monkeypatch.setattr(sync_worker, "stop_worker", lambda: None)
    # Deterministic orchestration fixtures stay fully offline; Groq is exercised
    # through dedicated mocked-provider and explicitly opt-in live tests.
    monkeypatch.setattr(orchestrator, "is_llm_configured", lambda: False)
    if os.environ.get("RUN_LIVE_GROQ_TESTS") != "1":
        # Leave all external provider calls disabled during normal tests while
        # preserving provider-status logic in unit tests that mock HTTP.
        monkeypatch.setattr(llm_service.settings, "groq_api_key", "")
    monkeypatch.setattr(workflow, "start_langfuse_trace", lambda **_kwargs: None)
    monkeypatch.setattr(tracing, "get_langfuse_client", lambda: None)
    monkeypatch.setattr(workflow, "get_response_generation_chain", lambda callbacks=None: _FakeGenerationChain())
    monkeypatch.setattr(workflow, "QualityChecker", _FakeQualityChecker)
