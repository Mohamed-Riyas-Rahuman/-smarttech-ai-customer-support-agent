"""LangChain chains for response generation."""
import json
import re
from typing import Any, Dict
from pydantic import BaseModel, Field
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda

from app.core.logging import logger
from app.integrations.llm.llm_service import get_llm
from app.services.langchain_workflow.prompts import get_support_prompt


class EmailResponseOutput(BaseModel):
    """Structured format for generated customer support response."""
    subject: str = Field(description="The response email subject line")
    body: str = Field(description="The full professional email body")


_PROVIDER_ERROR_TEXT = re.compile(
    r"(?i)\b(?:api[_ -]?error|traceback|authentication failed|rate limit exceeded|error\s*[:=])"
)


def _parse_json_or_fallback(text: str) -> Dict[str, str]:
    """Parse a structured email response; never persist an unparsed JSON wrapper or fail on unescaped plain text."""
    if not isinstance(text, str):
        text = str(text or "")
    clean = text.strip()
    if not clean:
        raise ValueError("The language model returned an empty response.")

    # 1. Look for markdown code fence with JSON
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1), strict=False)
            if isinstance(parsed, dict) and parsed.get("body"):
                body = str(parsed["body"]).strip()
                if body and not _PROVIDER_ERROR_TEXT.search(body):
                    return {
                        "subject": str(parsed.get("subject") or "SmartTech Support Follow-up").strip(),
                        "body": _clean_email_body(body),
                    }
        except Exception:
            pass

    # Strip whole outer fence if present
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean)
        clean = clean.strip()

    # 2. Try direct json.loads and repaired json
    data = None
    candidates = [clean]
    repaired = re.sub(r'\\(?!["\\/bfnrtu])', "", clean)
    if repaired != clean:
        candidates.append(repaired)
    for candidate in candidates:
        try:
            data = json.loads(candidate, strict=False)
            break
        except (json.JSONDecodeError, TypeError):
            start = candidate.find("{")
            if start >= 0:
                try:
                    data, _ = json.JSONDecoder(strict=False).raw_decode(candidate[start:])
                    break
                except json.JSONDecodeError:
                    pass
    if isinstance(data, dict) and isinstance(data.get("body"), str):
        body = data["body"].strip()
        if not body:
            raise ValueError("The language model returned an empty email body.")
        if _PROVIDER_ERROR_TEXT.search(body):
            raise ValueError("The language model returned an error instead of a customer response.")
        return {"subject": str(data.get("subject") or "SmartTech Support Follow-up").strip(), "body": _clean_email_body(body)}

    # 3. Regex extraction for JSON keys if JSON was malformed (e.g. unescaped newlines in body)
    subject_match = re.search(r'["\']?subject["\']?\s*:\s*["\']([^"\'\n\r]+)["\']', clean, re.I)
    body_match = re.search(r'["\']?body["\']?\s*:\s*["\'](.*?)["\']\s*(?:,\s*["\']|\}\s*$)', clean, re.DOTALL | re.I)
    if body_match:
        extracted_body = body_match.group(1).strip()
        if extracted_body and not _PROVIDER_ERROR_TEXT.search(extracted_body):
            extracted_subject = subject_match.group(1).strip() if subject_match else "SmartTech Support Follow-up"
            return {
                "subject": extracted_subject,
                "body": _clean_email_body(extracted_body),
            }

    # 4. Check for "Subject:" line followed by body
    lines = clean.split("\n", 1)
    if len(lines) > 1 and lines[0].strip().lower().startswith("subject:"):
        subject_line = lines[0].split(":", 1)[1].strip()
        body_part = lines[1].strip()
        if body_part and not _PROVIDER_ERROR_TEXT.search(body_part):
            return {
                "subject": subject_line or "SmartTech Support Follow-up",
                "body": _clean_email_body(body_part),
            }

    # 5. Check if the model returned plain conversational email text directly
    # (Must not be unparsed or broken JSON structure)
    if (
        not clean.startswith("{")
        and not clean.endswith("}")
        and not _PROVIDER_ERROR_TEXT.search(clean)
        and len(clean) >= 20
        and any(c.isalpha() for c in clean)
    ):
        first_line = clean.split("\n", 1)[0].strip()
        if first_line.lower().startswith("re:") or first_line.lower().startswith("subject"):
            parts = clean.split("\n", 1)
            subject = re.sub(r"(?i)^subject\s*:\s*", "", parts[0]).strip()
            body = parts[1].strip() if len(parts) > 1 else ""
            if body:
                return {"subject": subject, "body": _clean_email_body(body)}

        return {
            "subject": "SmartTech Support Follow-up",
            "body": _clean_email_body(clean),
        }

    raise ValueError("The language model response was not valid structured email content.")


def _clean_email_body(body: str) -> str:
    """Convert supported Markdown to clean plain text while retaining paragraphs and lists."""
    body = body.replace("\\n", "\n").replace("\\r", "")
    body = re.sub(r"(?<!\\)\\([*_`#])", r"\1", body)
    body = re.sub(r"^\s{0,3}#{1,6}\s+", "", body, flags=re.M)
    body = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), body)
    body = re.sub(r"\*(.+?)\*|_(.+?)_", lambda m: m.group(1) or m.group(2), body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    body = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", body)
    return body.strip()


def get_response_generation_chain(callbacks=None):
    """
    Build the LangChain chain: Prompt | LLM | JSON Parser.
    Returns Runnable chain accepting prompt variables and returning EmailResponseOutput dict.
    """
    prompt = get_support_prompt()
    llm = get_llm(temperature=0.2)

    chain = (
        prompt
        | (llm.with_config({"callbacks": callbacks}) if callbacks else llm)
        | StrOutputParser()
        | RunnableLambda(_parse_json_or_fallback)
    )
    return chain

