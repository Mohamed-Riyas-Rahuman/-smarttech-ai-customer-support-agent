"""Explicit opt-in Groq model availability and generation check."""
import os

import pytest


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_GROQ_TESTS") != "1",
    reason="Set RUN_LIVE_GROQ_TESTS=1 to make a live Groq request.",
)
def test_groq_live_connectivity_and_generation():
    from app.integrations.llm.llm_service import ensure_groq_model_available, get_llm

    status = ensure_groq_model_available(force_refresh=True)
    assert status["status"] == "available"
    response = get_llm(temperature=0.2, max_tokens=24).invoke("Reply with the single word OK.")
    assert str(response.content).strip()
