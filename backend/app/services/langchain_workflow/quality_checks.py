"""Safety and sentiment quality evaluation chains using LangChain."""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda

from app.core.config import settings
from app.core.logging import logger
from app.integrations.llm.llm_service import get_llm
from app.services.langchain_workflow.prompts import (
    SENTIMENT_ANALYSIS_PROMPT,
    SAFETY_GROUNDING_CHECK_PROMPT,
)


def _read_bool(value: Any, *, default: bool) -> bool:
    """Read evaluator booleans without treating non-empty strings as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return default


def _read_confidence(value: Any, *, default: float = 0.0) -> float:
    """Return a finite confidence score constrained to its documented range."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if not (score == score and abs(score) != float("inf")):
        return default
    return min(1.0, max(0.0, score))


@dataclass
class SentimentCheckResult:
    customer_sentiment: str  # positive, neutral, negative, frustrated
    is_empathetic_and_appropriate: bool
    tone_score: float
    tone_feedback: str


@dataclass
class SafetyCheckResult:
    is_relevant: bool
    is_grounded: bool
    policy_compliant: bool
    confidence_score: float
    unsupported_statements: List[str] = field(default_factory=list)
    requires_human_review: bool = False
    safety_reasoning: str = ""


@dataclass
class QualityEvaluationResult:
    passed: bool
    confidence_score: float
    sentiment: SentimentCheckResult
    safety: SafetyCheckResult
    requires_human_review: bool
    rejection_reasons: List[str] = field(default_factory=list)


def _clean_json_str(text: str) -> dict:
    """Safely extract dict from JSON string or markdown code fence."""
    clean = text.strip()
    # 1. Check for fenced code block anywhere in text
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1), strict=False)
        except Exception:
            pass

    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean)
        clean = clean.strip()
    try:
        return json.loads(clean, strict=False)
    except Exception:
        # Try raw decode from first '{'
        start = clean.find("{")
        if start >= 0:
            try:
                data, _ = json.JSONDecoder(strict=False).raw_decode(clean[start:])
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        # Fallback regex parse
        res = {}
        for line in clean.splitlines():
            m = re.match(r'"?(\w+)"?\s*:\s*(.+),?', line.strip())
            if m:
                k, v = m.group(1), m.group(2).strip().strip('",')
                if v.lower() == "true":
                    res[k] = True
                elif v.lower() == "false":
                    res[k] = False
                else:
                    try:
                        res[k] = float(v)
                    except ValueError:
                        res[k] = v
        return res


class QualityChecker:
    """Executes LangChain-based sentiment and safety quality verification."""

    def __init__(self, callbacks=None):
        self.llm = get_llm(temperature=0.0)
        self.callbacks = callbacks

        # Build sentiment chain
        self.sentiment_chain = (
            SENTIMENT_ANALYSIS_PROMPT
            | (self.llm.with_config({"callbacks": self.callbacks}) if self.callbacks else self.llm)
            | StrOutputParser()
            | RunnableLambda(_clean_json_str)
        )

        # Build safety chain
        self.safety_chain = (
            SAFETY_GROUNDING_CHECK_PROMPT
            | (self.llm.with_config({"callbacks": self.callbacks}) if self.callbacks else self.llm)
            | StrOutputParser()
            | RunnableLambda(_clean_json_str)
        )

    def evaluate_sentiment(
        self,
        subject: str,
        email_body: str,
        response_body: str,
    ) -> SentimentCheckResult:
        """Run sentiment analysis on customer inquiry and support tone."""
        try:
            res = self.sentiment_chain.invoke({
                "subject": subject,
                "email_body": email_body,
                "response_body": response_body,
            })
            sentiment = res.get("customer_sentiment", "neutral").lower()
            if sentiment not in {"positive", "neutral", "negative", "frustrated"}:
                sentiment = "neutral"

            return SentimentCheckResult(
                customer_sentiment=sentiment,
                is_empathetic_and_appropriate=_read_bool(
                    res.get("is_empathetic_and_appropriate"), default=False
                ),
                tone_score=_read_confidence(res.get("tone_score"), default=0.0),
                tone_feedback=str(res.get("tone_feedback", "Tone is appropriate.")),
            )
        except Exception as e:
            logger.warning("Sentiment check failed (%s); quality gate will fail closed.", type(e).__name__)
            return SentimentCheckResult(
                customer_sentiment="neutral",
                is_empathetic_and_appropriate=False,
                tone_score=0.0,
                tone_feedback="Automated tone validation failed; response must not be auto-sent.",
            )

    def evaluate_safety(
        self,
        subject: str,
        email_body: str,
        retrieved_knowledge: str,
        response_body: str,
    ) -> SafetyCheckResult:
        """Run response grounding, relevance, and policy validation."""
        try:
            res = self.safety_chain.invoke({
                "subject": subject,
                "email_body": email_body,
                "retrieved_knowledge": retrieved_knowledge,
                "response_body": response_body,
            })

            is_relevant = _read_bool(res.get("is_relevant"), default=False)
            is_grounded = _read_bool(res.get("is_grounded"), default=False)
            policy_compliant = _read_bool(res.get("policy_compliant"), default=False)
            confidence = _read_confidence(res.get("confidence_score"))
            unsupported = res.get("unsupported_statements", [])
            if not isinstance(unsupported, list):
                unsupported = [str(unsupported)] if unsupported else []

            requires_review = _read_bool(res.get("requires_human_review"), default=True)
            reasoning = str(res.get("safety_reasoning", "Standard safety check executed."))

            return SafetyCheckResult(
                is_relevant=is_relevant,
                is_grounded=is_grounded,
                policy_compliant=policy_compliant,
                confidence_score=confidence,
                unsupported_statements=unsupported,
                requires_human_review=requires_review,
                safety_reasoning=reasoning,
            )
        except Exception as e:
            logger.warning("Grounding check failed (%s); quality gate will fail closed.", type(e).__name__)
            return SafetyCheckResult(
                is_relevant=False,
                is_grounded=False,
                policy_compliant=False,
                confidence_score=0.0,
                unsupported_statements=["Automated grounding validation did not complete."],
                requires_human_review=False,
                safety_reasoning="Automated grounding validation failed; response must not be auto-sent.",
            )

    def run_all_checks(
        self,
        subject: str,
        email_body: str,
        retrieved_knowledge: str,
        response_body: str,
        auto_send_enabled: bool | None = None,
        confidence_threshold: float | None = None,
    ) -> QualityEvaluationResult:
        """
        Execute combined sentiment and safety verification.
        Determines overall pass status and whether human review is required.
        """
        sentiment_res = self.evaluate_sentiment(subject, email_body, response_body)
        safety_res = self.evaluate_safety(subject, email_body, retrieved_knowledge, response_body)

        rejection_reasons = []
        if not sentiment_res.is_empathetic_and_appropriate:
            rejection_reasons.append(f"Tone issues: {sentiment_res.tone_feedback}")
        if not safety_res.is_relevant:
            rejection_reasons.append("Response does not directly address the customer's issue.")
        if not safety_res.is_grounded:
            rejection_reasons.append(f"Ungrounded statements detected: {', '.join(safety_res.unsupported_statements)}")
        if not safety_res.policy_compliant:
            rejection_reasons.append("Response violates SmartTech warranty or support policy.")
        threshold = settings.confidence_threshold if confidence_threshold is None else confidence_threshold
        if safety_res.confidence_score < threshold:
            rejection_reasons.append(
                f"Confidence score ({safety_res.confidence_score:.2f}) is below threshold ({threshold:.2f})."
            )

        passed = len(rejection_reasons) == 0
        # Quality failures gate sending and should cause regeneration/fallback;
        # they do not mean the customer asked for a human. Approval intent is
        # determined from the original request by the workflow.
        # auto_send_enabled controls dispatch only, never customer intent.
        requires_human_review = False

        failed_checks = []
        if not sentiment_res.is_empathetic_and_appropriate:
            failed_checks.append("tone")
        if not safety_res.is_relevant:
            failed_checks.append("relevance")
        if not safety_res.is_grounded:
            failed_checks.append("grounding")
        if not safety_res.policy_compliant:
            failed_checks.append("policy")
        if safety_res.confidence_score < threshold:
            failed_checks.append("confidence")
        if safety_res.requires_human_review:
            failed_checks.append("safety_review")
        logger.info(
            "Quality evaluation completed (passed=%s, failed_checks=%s, "
            "confidence=%.2f, safety_review=%s, unsupported_statement_count=%d)",
            passed,
            ",".join(failed_checks) or "none",
            safety_res.confidence_score,
            safety_res.requires_human_review,
            len(safety_res.unsupported_statements),
        )

        return QualityEvaluationResult(
            passed=passed,
            confidence_score=safety_res.confidence_score,
            sentiment=sentiment_res,
            safety=safety_res,
            requires_human_review=requires_human_review,
            rejection_reasons=rejection_reasons,
        )
