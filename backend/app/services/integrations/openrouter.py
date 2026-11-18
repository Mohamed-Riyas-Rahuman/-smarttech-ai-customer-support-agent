"""Inactive compatibility shim; OpenRouter requests have been removed.

Kept to avoid deleting a project file that may be referenced by external local
tools. Application code must use app.integrations.llm.llm_service (Groq).
"""


class OpenRouterClient:
    def __init__(self, *_args, **_kwargs):
        raise RuntimeError("OpenRouter support has been removed; configure the shared Groq client.")


openrouter_client = None
