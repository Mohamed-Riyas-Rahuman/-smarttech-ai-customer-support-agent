"""Langfuse callback that records LangChain events without customer payloads."""
from typing import Any

from langchain_core.documents import Document
import re

from langchain_core.outputs import ChatGeneration, Generation, LLMResult

from app.core.logging import logger

try:
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler
except ImportError:
    LangfuseCallbackHandler = None


class PrivacySafeLangfuseCallbackHandler(LangfuseCallbackHandler or object):
    """Keep useful LLM content while removing credentials and customer identifiers."""

    _SECRET_RE = re.compile(
        r"(?i)(\b(?:authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|"
        r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*)"
        r")([^\s,;\"']+)|\b(?:gsk_[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{12,})\b"
    )
    _EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    _PHONE_RE = re.compile(r"\b(?:\+?\d[\d .()\-]{7,}\d)\b")
    _SENSITIVE_KEYS = re.compile(r"(?:password|api[_-]?key|token|secret|authorization|credential)", re.I)

    @classmethod
    def _sanitize_text(cls, value: str) -> str:
        value = cls._SECRET_RE.sub(lambda match: (match.group(1) or "") + "[REDACTED_SECRET]", value)
        value = cls._EMAIL_RE.sub("[REDACTED_EMAIL]", value)
        return cls._PHONE_RE.sub("[REDACTED_PHONE]", value)

    @classmethod
    def _sanitize(cls, value: Any, key: str | None = None) -> Any:
        if key and cls._SENSITIVE_KEYS.search(key):
            return "[REDACTED]"
        if isinstance(value, str):
            return cls._sanitize_text(value)
        if isinstance(value, dict):
            return {k: cls._sanitize(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._sanitize(item) for item in value)
        return value

    @staticmethod
    def _redact(value: Any) -> Any:
        return PrivacySafeLangfuseCallbackHandler._sanitize(value)

    @classmethod
    def _redact_kwargs(cls, kwargs: dict[str, Any]) -> dict[str, Any]:
        safe_kwargs = {}
        invocation_params = kwargs.get("invocation_params")
        if isinstance(invocation_params, dict):
            safe_model_params = {
                key: value
                for key, value in invocation_params.items()
                if key in {
                    "model", "model_name", "temperature", "max_tokens",
                    "top_p", "frequency_penalty", "presence_penalty",
                }
                and isinstance(value, (str, int, float, bool, type(None)))
            }
            if safe_model_params:
                safe_kwargs["invocation_params"] = safe_model_params
        if "inputs" in kwargs:
            safe_kwargs["inputs"] = cls._sanitize(kwargs["inputs"])
        return safe_kwargs

    @staticmethod
    def _safe_serialized(serialized: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(serialized, dict):
            return {}
        safe = {}
        for key in ("name", "id"):
            value = serialized.get(key)
            if isinstance(value, str):
                safe[key] = value[:160]
            elif key == "id" and isinstance(value, list):
                safe[key] = [str(item)[:160] for item in value[:4]]
        return safe

    def _delegate(self, method: str, *args, **kwargs):
        try:
            callback = getattr(super(PrivacySafeLangfuseCallbackHandler, self), method)
            return callback(*args, **kwargs)
        except Exception as exc:
            logger.warning("Langfuse callback %s failed (%s)", method, type(exc).__name__)
            return None

    @staticmethod
    def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        if metadata is None:
            return None
        safe_keys = {"ls_provider", "ls_model_name", "ls_model_type"}
        return {
            key: value if key in safe_keys else "[REDACTED]"
            for key, value in metadata.items()
        }

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        return self._delegate(
            "on_chain_start", self._safe_serialized(serialized), self._redact(inputs), run_id=run_id,
            parent_run_id=parent_run_id, tags=None, metadata=self._safe_metadata(metadata),
            **self._redact_kwargs(kwargs),
        )

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        return self._delegate(
            "on_chain_end",
            self._redact(outputs), run_id=run_id, parent_run_id=parent_run_id,
            **self._redact_kwargs(kwargs),
        )

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        return self._delegate(
            "on_llm_start", self._safe_serialized(serialized), [self._sanitize(p) for p in prompts], run_id=run_id,
            parent_run_id=parent_run_id, tags=None, metadata=self._safe_metadata(metadata),
            **self._redact_kwargs(kwargs),
        )

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        safe_messages = [
            [message.model_copy(update={
                "content": self._sanitize(message.content),
                "additional_kwargs": self._sanitize(message.additional_kwargs),
                "response_metadata": self._sanitize(message.response_metadata),
            }) for message in group]
            for group in messages
        ]
        return self._delegate(
            "on_chat_model_start", self._safe_serialized(serialized), safe_messages, run_id=run_id,
            parent_run_id=parent_run_id, tags=None, metadata=self._safe_metadata(metadata),
            **self._redact_kwargs(kwargs),
        )

    def on_llm_end(self, response: LLMResult, *, run_id, parent_run_id=None, **kwargs):
        safe_generations = []
        for group in response.generations:
            safe_group = []
            for generation in group:
                generation_info = generation.generation_info or {}
                safe_generation_info = {
                    key: value
                    for key, value in generation_info.items()
                    if key in {"finish_reason", "model_name"}
                    and isinstance(value, (str, int, float, bool, type(None)))
                }
                if isinstance(generation, ChatGeneration):
                    update = {
                        "content": self._sanitize(generation.message.content),
                        "additional_kwargs": self._sanitize(generation.message.additional_kwargs),
                        "response_metadata": self._sanitize(generation.message.response_metadata),
                    }
                    message = generation.message.model_copy(update=update)
                    safe_group.append(
                        ChatGeneration(message=message, generation_info=safe_generation_info)
                    )
                else:
                    safe_group.append(
                        Generation(text=self._sanitize(generation.text), generation_info=safe_generation_info)
                    )
            safe_generations.append(safe_group)
        llm_output = response.llm_output or {}
        token_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        safe_usage = {
            key: value
            for key, value in token_usage.items()
            if key in {
                "prompt_tokens", "completion_tokens", "total_tokens",
                "input_tokens", "output_tokens",
            }
            and isinstance(value, int)
        } if isinstance(token_usage, dict) else {}
        if isinstance(token_usage, dict):
            for detail_key in ("prompt_tokens_details", "completion_tokens_details"):
                details = token_usage.get(detail_key)
                if isinstance(details, dict):
                    safe_details = {
                        key: value
                        for key, value in details.items()
                        if key in {"cached_tokens", "reasoning_tokens", "audio_tokens", "text_tokens"}
                        and isinstance(value, int)
                    }
                    if safe_details:
                        safe_usage[detail_key] = safe_details
        safe_llm_output = {"token_usage": safe_usage} if safe_usage else {}
        model_name = llm_output.get("model_name")
        if isinstance(model_name, str) and len(model_name) <= 120:
            safe_llm_output["model_name"] = model_name
        safe_response = LLMResult(
            generations=safe_generations,
            llm_output=safe_llm_output,
        )
        return self._delegate(
            "on_llm_end",
            safe_response, run_id=run_id, parent_run_id=parent_run_id,
            **self._redact_kwargs(kwargs),
        )

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        return self._delegate(
            "on_tool_start", self._safe_serialized(serialized), "[REDACTED]", run_id=run_id,
            parent_run_id=parent_run_id, tags=None, metadata=self._safe_metadata(metadata),
            **self._redact_kwargs(kwargs),
        )

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        return self._delegate(
            "on_tool_end",
            "[REDACTED]", run_id=run_id, parent_run_id=parent_run_id,
            **self._redact_kwargs(kwargs),
        )

    def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        return self._delegate(
            "on_retriever_start", self._safe_serialized(serialized), "[REDACTED]", run_id=run_id,
            parent_run_id=parent_run_id, tags=None, metadata=self._safe_metadata(metadata),
            **self._redact_kwargs(kwargs),
        )

    def on_retriever_end(self, documents, *, run_id, parent_run_id=None, **kwargs):
        redacted_documents = [Document(page_content="[REDACTED]") for _ in documents]
        return self._delegate(
            "on_retriever_end",
            redacted_documents, run_id=run_id, parent_run_id=parent_run_id,
            **self._redact_kwargs(kwargs),
        )

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        return self._delegate(
            "on_chain_error",
            RuntimeError("Chain execution failed"), run_id=run_id,
            parent_run_id=parent_run_id, **self._redact_kwargs(kwargs),
        )

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        return self._delegate(
            "on_llm_error",
            RuntimeError("LLM execution failed"), run_id=run_id,
            parent_run_id=parent_run_id, **self._redact_kwargs(kwargs),
        )

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        return self._delegate(
            "on_tool_error",
            RuntimeError("Tool execution failed"), run_id=run_id,
            parent_run_id=parent_run_id, **self._redact_kwargs(kwargs),
        )

    def on_retriever_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        return self._delegate(
            "on_retriever_error",
            RuntimeError("Retriever execution failed"), run_id=run_id,
            parent_run_id=parent_run_id, **self._redact_kwargs(kwargs),
        )
