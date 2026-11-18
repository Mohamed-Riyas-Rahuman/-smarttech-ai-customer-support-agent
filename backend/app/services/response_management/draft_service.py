"""Draft creation and auto-send coordination service."""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.integrations.email.gmail_service import gmail_service
from app.models.draft import Draft
from app.models.ticket import SupportTicket


def _validate_response_body(body: str) -> str:
    """Reject empty, citation-only, and provider-error text before persistence."""
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Cannot save a response with an empty email body.")
    content = body.strip()
    if re.match(r"(?s)^\s*\{\s*\"subject\"\s*:", content) or re.search(r"(?im)^\s*Sources\s*:\s*$", content):
        raise ValueError("Cannot save a JSON-wrapped response or internal source metadata as an email body.")
    if re.search(r"(?im)^\s*(?:source:|citation:|\[evidence\s+\d+\]|relevance:\s*\d+%?)\s*.*$", content) and not re.search(r"(?im)^\s*(?:hi|hello|dear|thank you|thanks|we|i|please|our)\b", content):
        raise ValueError("Cannot save a response that contains citations without a customer-facing message.")
    if re.search(r"(?i)\b(?:api[_ -]?error|traceback|authentication failed|rate limit exceeded|error\s*[:=])", content):
        raise ValueError("Cannot save a provider error as a customer response.")
    return content


@dataclass
class DraftOutcome:
    draft_id: int
    status: str  # pending_review, sent, send_failed, or send_ambiguous
    is_sent: bool
    recipient: str
    subject: str
    gmail_draft_id: Optional[str] = None
    gmail_message_id: Optional[str] = None
    action_taken: str = "saved_as_draft"


class DraftService:
    """Manages saving responses as Pending Drafts or executing approved auto-send workflows."""

    def __init__(self, db: Session):
        self.db = db

    def save_pending_draft(
        self,
        ticket_id: int,
        email_id: Optional[int],
        recipient: str,
        subject: str,
        body: str,
        citations: Optional[List[str]] = None,
        thread_id: Optional[str] = None,
        rejection_reason: Optional[str] = None,
    ) -> DraftOutcome:
        """
        Save the AI-generated response as a Pending Draft for human review.
        Also creates a draft in Gmail if authorized.
        """
        body = _validate_response_body(body)
        citations_json = json.dumps(citations or [])

        # Create or update Draft record
        draft = Draft(
            ticket_id=ticket_id,
            email_id=email_id,
            recipient=recipient,
            subject=subject,
            body=body,
            status="pending_review",
            citations=citations_json,
            rejection_reason=rejection_reason,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(draft)
        self.db.commit()
        self.db.refresh(draft)

        # Integrate with Gmail draft API if connected
        gmail_draft_id = None
        if gmail_service.is_authorized() and thread_id:
            try:
                ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
                ticket_num = ticket.ticket_number if ticket else ""
                created = gmail_service.create_draft_for_ticket(
                    ticket_id=ticket_id,
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    thread_id=thread_id,
                    ticket_number=ticket_num,
                )
                if created and "id" in created:
                    gmail_draft_id = created["id"]
                    logger.info("Created Gmail Pending Draft %s for ticket %s", gmail_draft_id, ticket_id)
            except Exception as e:
                logger.warning("Could not sync draft to Gmail API (%s).", type(e).__name__)

        logger.info("Saved Draft #%d as 'pending_review' for ticket #%d", draft.id, ticket_id)
        return DraftOutcome(
            draft_id=draft.id,
            status=draft.status,
            is_sent=False,
            recipient=recipient,
            subject=subject,
            gmail_draft_id=gmail_draft_id,
            action_taken="saved_as_draft",
        )

    def process_delivery_or_draft(
        self,
        ticket_id: int,
        email_id: Optional[int],
        recipient: str,
        subject: str,
        body: str,
        passed_safety: bool,
        requires_human_review: bool,
        confidence_score: float,
        citations: Optional[List[str]] = None,
        thread_id: Optional[str] = None,
        grounding_validated: bool = False,
    ) -> DraftOutcome:
        """
        Coordinate delivery:
        If auto-send is enabled AND safety checks passed AND no human review is required
        AND confidence >= threshold: send automatically.
        Otherwise: retain as Pending Draft for human review.
        """
        from app.services.auto_send import (
            is_auto_send_enabled,
            process_auto_send_for_draft,
        )

        body = _validate_response_body(body)

        # Retries can generate a new draft, but must never send another reply
        # for an email whose send is complete or whose outcome is uncertain.
        if email_id is not None:
            existing_send = self.db.scalar(
                select(Draft)
                .where(
                    Draft.email_id == email_id,
                    Draft.status.in_(("sent", "send_ambiguous")),
                )
                .order_by(Draft.created_at.desc())
            )
            if existing_send:
                return DraftOutcome(
                    draft_id=existing_send.id,
                    status=existing_send.status,
                    is_sent=existing_send.status == "sent",
                    recipient=existing_send.recipient,
                    subject=existing_send.subject,
                    gmail_message_id=existing_send.provider_message_id,
                    action_taken="already_sent" if existing_send.status == "sent" else "send_ambiguous",
                )

        from app.services.auto_send import get_confidence_threshold
        threshold = get_confidence_threshold(self.db)
        can_auto_send = (
            is_auto_send_enabled(self.db)
            and passed_safety
            and not requires_human_review
            and confidence_score >= threshold
        )
        auto_send_block_reason = None
        if is_auto_send_enabled(self.db) and not can_auto_send:
            if not passed_safety:
                auto_send_block_reason = "Auto-Send blocked: response did not pass grounding and safety validation."
            elif requires_human_review:
                auto_send_block_reason = "Auto-Send blocked: this response requires human review."
            elif confidence_score < threshold:
                auto_send_block_reason = "Auto-Send blocked: response confidence is below the configured threshold."

        if can_auto_send:
            # Persist the response before evaluating the detailed dispatch
            # guardrails. The dispatcher leaves ineligible replies for review.
            draft = Draft(
                ticket_id=ticket_id,
                email_id=email_id,
                recipient=recipient,
                subject=subject,
                body=body,
                status="pending_review",
                citations=json.dumps(citations or []),
                rejection_reason=auto_send_block_reason,
                created_at=datetime.now(timezone.utc),
            )
            self.db.add(draft)
            self.db.commit()
            self.db.refresh(draft)

            try:
                ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
                if not ticket:
                    raise ValueError("Support ticket disappeared before auto-send evaluation.")
                result = process_auto_send_for_draft(
                    db=self.db,
                    ticket=ticket,
                    draft=draft,
                    agent_res=SimpleNamespace(
                        confidence_score=confidence_score,
                        citations=citations or [],
                        grounding_validated=grounding_validated,
                    ),
                    thread_id=thread_id,
                )
                self.db.refresh(draft)
                if result.get("auto_sent"):
                    return DraftOutcome(
                        draft_id=draft.id,
                        status="sent",
                        is_sent=True,
                        recipient=recipient,
                        subject=subject,
                        gmail_message_id=result.get("gmail_message_id"),
                        action_taken="sent_automatically",
                    )
                return DraftOutcome(
                    draft_id=draft.id,
                    status=draft.status,
                    is_sent=False,
                    recipient=recipient,
                    subject=subject,
                    action_taken=(
                        "saved_as_draft"
                        if draft.status == "pending_review"
                        else draft.status
                    ),
                )
            except Exception as exc:
                self.db.rollback()
                logger.error(
                    "Auto-send eligibility failed (%s); retaining response for review.",
                    type(exc).__name__,
                )
                retained = self.db.get(Draft, draft.id)
                if retained:
                    return DraftOutcome(
                        draft_id=retained.id,
                        status=retained.status,
                        is_sent=retained.status == "sent",
                        recipient=retained.recipient,
                        subject=retained.subject,
                        action_taken=(
                            "sent_automatically"
                            if retained.status == "sent"
                            else (
                                "saved_as_draft"
                                if retained.status == "pending_review"
                                else retained.status
                            )
                        ),
                    )
                return self.save_pending_draft(
                    ticket_id=ticket_id,
                    email_id=email_id,
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    citations=citations,
                    thread_id=thread_id,
                    rejection_reason=auto_send_block_reason,
                )

        # Default: Save as Pending Draft
        return self.save_pending_draft(
            ticket_id=ticket_id,
            email_id=email_id,
            recipient=recipient,
            subject=subject,
            body=body,
            citations=citations,
            thread_id=thread_id,
            rejection_reason=auto_send_block_reason,
        )

