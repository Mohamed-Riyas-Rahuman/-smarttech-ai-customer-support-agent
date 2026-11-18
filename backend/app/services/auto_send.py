"""
Automatic Email Response Pipeline & Safety Verification Engine.

Manages automatic response eligibility, safety guardrails, persistent
configuration, and live Gmail reply dispatch.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.models.audit import AuditLog, SystemSetting
from app.models.draft import Draft
from app.models.ticket import SupportTicket, TicketMessage
from app.services.integrations.gmail import gmail_service

# Immutable process-start environment value for runtime precedence reporting.
ENV_AUTO_SEND_ENABLED = bool(settings.auto_send_enabled)


# ---------------------------------------------------------------------------
# Persistent Auto-Send Configuration
# ---------------------------------------------------------------------------

def is_auto_send_enabled(db: Session | None = None) -> bool:
    """
    Check whether automatic sending is enabled.

    The persistent database setting takes priority over application config.
    If the database cannot be read, use the configured fallback.
    """

    if db is not None:
        try:
            value = db.scalar(
                select(SystemSetting.value).where(
                    SystemSetting.key == "auto_send_enabled"
                )
            )

            if value is not None:
                return str(value).strip().lower() in ("true", "1", "yes")

        except Exception as exc:
            logger.warning(
                "Could not read auto_send_enabled from database (%s)",
                type(exc).__name__,
            )
            # Do not fall back to an environment value that may enable live
            # dispatch when the authoritative persisted setting is unreadable.
            return False

    return ENV_AUTO_SEND_ENABLED


def get_confidence_threshold(db: Session | None = None) -> float:
    """Read the administrator's persisted threshold; fail closed on bad values."""
    if db is not None:
        try:
            value = db.scalar(select(SystemSetting.value).where(SystemSetting.key == "ai_confidence_threshold"))
            if value is not None:
                threshold = float(value)
                return threshold if 0.0 <= threshold <= 1.0 else 0.95
        except Exception as exc:
            logger.warning("Could not read confidence threshold (%s); using safe 0.95 default", type(exc).__name__)
            return 0.95
    return float(settings.confidence_threshold)


def set_auto_send_enabled(
    db: Session,
    enabled: bool,
    user_id: int | None = None,
) -> SystemSetting:
    """
    Persistently enable or disable automatic sending.
    """

    str_value = "true" if enabled else "false"

    setting = db.scalar(
        select(SystemSetting).where(
            SystemSetting.key == "auto_send_enabled"
        )
    )

    if setting:
        setting.value = str_value

        if user_id is not None:
            setting.updated_by_user_id = user_id

    else:
        setting = SystemSetting(
            key="auto_send_enabled",
            value=str_value,
            description=(
                "Controls automatic email dispatch and manual-review mode."
            ),
            is_secret=False,
            updated_by_user_id=user_id,
        )
        db.add(setting)

    # Retain compatibility for UI/tests that inspect the in-memory setting;
    # effective dispatch always consults the persisted row first.
    settings.auto_send_enabled = enabled

    db.add(
        AuditLog(
            user_id=user_id,
            action="update_setting",
            entity_type="setting",
            entity_id="auto_send_enabled",
            details=(
                f"auto_send_enabled changed to {str_value} (persistent)"
            ),
        )
    )

    db.commit()
    db.refresh(setting)

    logger.info("auto_send_enabled updated to %s", str_value)

    return setting


# ---------------------------------------------------------------------------
# Auto-Send Eligibility & Risk Guardrails
# ---------------------------------------------------------------------------


def customer_requires_human_approval(subject: str = "", message: str = "") -> bool:
    """Compatibility predicate: only concrete phishing/fraud indicators require review."""
    from app.services.email_risk import assess_inbound_email_risk

    return assess_inbound_email_risk(subject, message).requires_human_review


_RELEVANCE_STOPWORDS = {"what", "when", "where", "which", "would", "could", "please", "help", "with", "from", "that", "this", "have", "your", "about", "does", "the", "and", "for", "can", "you", "how", "why"}


def evaluate_auto_send_eligibility(
    db: Session,
    ticket: SupportTicket,
    draft: Draft,
    agent_res: Any | None = None,
) -> Tuple[bool, str]:
    """
    Evaluate whether a response draft qualifies for automatic dispatch.

    Returns:
        (eligible, reason)
    """

    from app.services.email_risk import assess_inbound_email_risk

    # 1. Administrator setting check.
    if not is_auto_send_enabled(db):
        return (
            False,
            "Automatic sending is disabled by administrator setting.",
        )

    # 2. Gmail authorization and sending permissions.
    if (
        not gmail_service.is_authorized()
        or not gmail_service.has_send_permission()
    ):
        return (
            False,
            "Gmail integration is not authorized with send permissions.",
        )

    # 3. Draft status checks.
    if draft.status == "sent":
        return False, "Draft has already been sent to the customer."

    if draft.status in (
        "rejected",
        "send_failed",
        "send_ambiguous",
    ):
        return (
            False,
            f"Draft is in '{draft.status}' state and requires review.",
        )

    # 5. Gather customer message context.
    customer_messages = [
        message.message_body or ""
        for message in ticket.messages
        if message.sender_type == "customer"
    ]

    # 6. Approval is based on the original customer's explicit request.
    risk_assessment = assess_inbound_email_risk(ticket.subject, " ".join(customer_messages))
    if risk_assessment.requires_human_review:
        return False, risk_assessment.reason or "Inbound email contains a phishing or fraud indicator; human review is required."
    # 10. Privacy and customer identity safeguards.
    if ticket.customer_id is None or not ticket.customer:
        return (
            False,
            "Customer identity is unverified against an authorized customer record; manual review is required.",
        )

    from app.services.response_validation import validate_draft_customer_identity
    identity_error = validate_draft_customer_identity(db, ticket, draft.recipient, draft.body)
    if identity_error:
        return False, identity_error

    # Check available model confidence before deeper content checks so the
    # dashboard reports the most actionable reason when confidence is absent.
    confidence_score = getattr(agent_res, "confidence_score", None) if agent_res else None
    from app.models.agent import AgentExecution
    last_execution = db.scalar(
        select(AgentExecution)
        .where(AgentExecution.ticket_id == ticket.id)
        .order_by(AgentExecution.created_at.desc())
    )
    if last_execution:
        if confidence_score is None:
            confidence_score = last_execution.confidence_score
    threshold = get_confidence_threshold(db)
    if confidence_score is None:
        return False, "AI confidence score is unavailable; manual review is required."
    try:
        confidence_score = float(confidence_score)
    except (TypeError, ValueError):
        return False, "AI confidence score is invalid; manual review is required."
    if not 0.0 <= confidence_score <= 1.0 or confidence_score < threshold:
        return False, f"AI confidence score ({confidence_score:.2f}) does not meet the approval threshold ({threshold:.2f})."

    # 11. Clarification gate.
    # A clarification category alone does not block sending; the other
    # recipient, confidence, escalation, and safety checks still apply.

    # 12. Knowledge-grounding verification for technical troubleshooting inquiries.
    citations = list(getattr(agent_res, "citations", []) or []) if agent_res else []

    if not citations and draft.citations:
        try:
            citation_data = (
                json.loads(draft.citations)
                if isinstance(draft.citations, str)
                else draft.citations
            )

            if isinstance(citation_data, list):
                citations = citation_data if isinstance(citation_data, list) else []

        except (TypeError, ValueError, json.JSONDecodeError):
            citations = []

    # Evaluate the generated reply, not just the customer's inquiry.
    reply_text = (draft.body or "").lower()

    # Require citations for replies that actually provide technical guidance.
    technical_instruction_markers = (

        "follow these steps",
        "try the following",
        "run this command",
        "install the driver",
        "update the firmware",
        "reset the device",
        "reconfigure the device",
        "change the settings",
        "disconnect and reconnect",
        "replace the component",
    )

    technical_query = (ticket.subject or "").lower() + " " + " ".join(customer_messages).lower()
    # A technical answer still needs evidence. The sole no-citation exception
    # is an honest statement that the documentation does not specify an answer.
    docs_missing_answer = bool(
        re.search(
            r"\b(?:documentation|manual|available information)\b.{0,100}\b"
            r"(?:does not specify|doesn't specify|does not state|doesn't state|does not cover|"
            r"doesn't cover|does not contain|doesn't contain|is not available|isn't available)\b",
            reply_text,
        )
        or re.search(
            r"\b(?:couldn't|could not|cannot|can't)\s+(?:find|identify|match|verify)\b"
            r".{0,100}\b(?:exact\s+)?(?:product|model|device)\b",
            reply_text,
        )
        or re.search(
            r"\b(?:cannot|can't|unable to)\s+verify\b.{0,80}\b"
            r"(?:specifications|product-specific|model-specific)\b",
            reply_text,
        )
    )
    is_technical = not docs_missing_answer and (
        ticket.category in {"technical", "troubleshooting"}
        or any(marker in reply_text for marker in technical_instruction_markers)
        or any(term in technical_query for term in ("troubleshoot", "firmware", "technical", "device error", "overclock", "overheat", "battery"))
    )

    if is_technical:
        from app.models.knowledge import KnowledgeDocument
        valid_citations = []
        indexed_docs = db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.is_indexed.is_(True))).all()
        for citation in citations:
            if isinstance(citation, str):
                source = citation.strip().lower()
                score_match = re.search(r"\brelevance\s+(\d+(?:\.\d+)?)%", source)
                relevant = bool(score_match and float(score_match.group(1)) / 100 >= 0.40)
                valid = relevant and any(doc.title.lower() in source or doc.filename.lower() in source for doc in indexed_docs)
            elif isinstance(citation, dict):
                source = str(citation.get("document") or citation.get("document_title") or citation.get("citation") or "").lower()
                try:
                    relevant = float(citation.get("relevance", citation.get("similarity", 0))) >= 0.40
                except (TypeError, ValueError):
                    relevant = False
                valid = relevant and any(doc.title.lower() in source or doc.filename.lower() in source for doc in indexed_docs)
            else:
                valid = False
            if valid:
                valid_citations.append(citation)
        if not valid_citations:
            return False, "No valid, relevant citations from the indexed knowledge base; manual review is required."

    # A response that does not address any meaningful term in the customer's request must be reviewed.
    query_tokens = {word for word in re.findall(r"[a-z0-9]+", technical_query) if len(word) > 2 and word not in _RELEVANCE_STOPWORDS}
    reply_tokens = {word for word in re.findall(r"[a-z0-9]+", reply_text) if len(word) > 2}
    if not docs_missing_answer and query_tokens and not (query_tokens & reply_tokens):
        return False, "Response relevance to the customer's question could not be verified; manual review is required."


    # 13. Confidence and escalation checks.
    # 14. Recipient and loop-prevention checks.
    recipient = (draft.recipient or "").strip().lower()

    if not recipient or not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+",
        recipient,
    ):
        return False, "Invalid or missing recipient email address."

    try:
        status_data = gmail_service.get_authorization_status() or {}
    except Exception as exc:
        logger.warning(
            "Unable to verify connected Gmail account: %s",
            exc,
        )
        return (
            False,
            "Could not verify the support mailbox; manual review is required.",
        )

    connected_email = (
        status_data.get("connected_email") or ""
    ).strip().lower()

    if (not connected_email or not status_data.get("is_authorized") or not status_data.get("has_send_permission")
            or status_data.get("permissions_sufficient") is False
            or status_data.get("gmail_api_available") is False
            or status_data.get("connection_state") not in (None, "connected")):
        return (
            False,
            "Connected Gmail account and send permission could not be verified.",
        )

    if recipient == connected_email:
        return (
            False,
            "Recipient matches the support mailbox; reply blocked.",
        )

    expected_recipient = (ticket.sender_email or (ticket.customer.email if ticket.customer else "") or "").strip().lower()
    if not expected_recipient or recipient != expected_recipient:
        return False, "Draft recipient does not match the verified ticket customer."
    if not ticket.customer.email or ticket.customer.email.strip().lower() != recipient:
        return False, "Draft recipient does not match the verified customer's actual email address."

    return (
        True,
        "Routine inquiry passed the configured automatic-send checks.",
    )


# ---------------------------------------------------------------------------
# Auto-Send Dispatcher
# ---------------------------------------------------------------------------

def process_auto_send_for_draft(
    db: Session,
    ticket: SupportTicket,
    draft: Draft,
    agent_res: Any | None = None,
    thread_id: str | None = None,
    message_id_header: str | None = None,
    references: str | None = None,
) -> dict:
    """
    Evaluate and execute an automatic reply when eligible.

    Ineligible drafts remain available for manual review.
    """

    # 1. Resolve the recipient before eligibility validation.
    resolved_recipient = (
        draft.recipient or ticket.sender_email or ""
    ).strip()

    if (
        resolved_recipient
        and resolved_recipient.lower() != "customer@example.com"
    ):
        draft.recipient = resolved_recipient

    # 2. Check eligibility using all safety guardrails.
    eligible, reason = evaluate_auto_send_eligibility(
        db,
        ticket,
        draft,
        agent_res,
    )

    # 3. Independent fail-closed auto-send check.
    if not is_auto_send_enabled(db):
        draft.rejection_reason = "Automatic sending is disabled by administrator setting."
        db.commit()
        logger.info(
            "Auto-send blocked because automatic sending is disabled. "
            "Draft %s",
            draft.id,
        )

        return {
            "auto_sent": False,
            "eligible": False,
            "reason": (
                "Automatic sending is disabled; "
                "draft retained for review."
            ),
            "draft_status": draft.status,
            "ticket_status": ticket.status,
        }

    # 4. Leave ineligible drafts for review.
    if not eligible:
        draft.rejection_reason = reason
        db.commit()
        logger.info(
            "Ticket %s draft %d not auto-sent: %s",
            ticket.ticket_number,
            draft.id,
            reason,
        )

        draft.rejection_reason = reason
        db.commit()

        return {
            "auto_sent": False,
            "eligible": False,
            "reason": reason,
            "draft_status": draft.status,
            "ticket_status": ticket.status,
        }

    # Claim the draft before contacting Gmail so concurrent workers cannot
    # dispatch the same reply twice.
    locked_draft = db.scalar(select(Draft).where(Draft.id == draft.id).with_for_update())
    if not locked_draft or locked_draft.status not in ("pending_review", "approved"):
        return {"auto_sent": False, "eligible": False, "reason": "Draft state changed before dispatch; manual review is required.", "draft_status": locked_draft.status if locked_draft else "missing", "ticket_status": ticket.status}
    locked_draft.status = "send_ambiguous"
    locked_draft.send_attempts = (locked_draft.send_attempts or 0) + 1
    db.add(AuditLog(user_id=None, action="auto_send_attempt", entity_type="draft", entity_id=str(draft.id), details="Automatic Gmail send attempt recorded before dispatch."))
    db.commit()
    db.refresh(draft)

    # 5. Resolve threading metadata and send the reply.
    try:
        if not thread_id or not message_id_header:
            from app.models.email import EmailMessage

            original_email = db.scalar(
                select(EmailMessage)
                .where(EmailMessage.ticket_id == ticket.id)
                .order_by(EmailMessage.received_at.asc())
            )

            if original_email:
                thread_id = thread_id or original_email.thread_id

                message_id_header = (
                    message_id_header
                    or original_email.rfc_message_id
                )
                if references is None:
                    references = original_email.references_header

        logger.info(
            "Auto-sending eligible response for ticket %s, "
            "draft %d, recipient %s",
            ticket.ticket_number,
            draft.id,
            draft.recipient,
        )

        send_result = gmail_service.send_reply(
            to_email=draft.recipient,
            subject=draft.subject,
            body_text=draft.body,
            thread_id=thread_id,
            in_reply_to=message_id_header,
            references=references,
        )

        if not isinstance(send_result, dict) or not send_result.get("id"):
            raise RuntimeError("Gmail returned no confirmed message ID.")

        # 6. Record successful dispatch.
        now = datetime.now(timezone.utc)

        draft.status = "sent"
        draft.sent_at = now
        draft.provider_message_id = send_result.get("id")

        ticket.status = "resolved"
        ticket.resolved_at = now

        status_data = gmail_service.get_authorization_status() or {}

        own_email = status_data.get("connected_email")

        db.add(
            TicketMessage(
                ticket_id=ticket.id,
                sender_type="agent",
                sender_email=own_email,
                sender_name="SmartTech Support Agent (Auto-Dispatch)",
                message_body=draft.body,
            )
        )

        db.add(
            AuditLog(
                user_id=None,
                action="auto_send_email",
                entity_type="draft",
                entity_id=str(draft.id),
                details=(
                    f"Auto-sent response for ticket "
                    f"{ticket.ticket_number} "
                    f"(Gmail id: {send_result.get('id')}). "
                    f"Reason: {reason}"
                ),
            )
        )

        db.commit()

        logger.info(
            "Auto-send SUCCESS: ticket %s, draft %d, Gmail ID %s",
            ticket.ticket_number,
            draft.id,
            send_result.get("id"),
        )

        return {
            "auto_sent": True,
            "eligible": True,
            "gmail_message_id": send_result.get("id"),
            "reason": reason,
            "draft_status": "sent",
            "ticket_status": "resolved",
        }

    except Exception as exc:
        logger.error(
            "Auto-send failed for ticket %s, draft %d (%s)",
            ticket.ticket_number,
            draft.id,
            type(exc).__name__,
        )

        # 7. Reconcile before deciding whether the message was sent.
        try:
            reconciled = gmail_service.reconcile_sent_message(
                to_email=draft.recipient,
                subject=draft.subject,
                thread_id=thread_id,
            )

        except Exception as reconcile_exc:
            logger.error(
                "Gmail send reconciliation failed for draft %d: %s",
                draft.id,
                reconcile_exc,
            )

            # Never retry an uncertain send automatically.
            draft.status = "send_ambiguous"
            db.commit()

            return {
                "auto_sent": False,
                "eligible": True,
                "reason": (
                    "Gmail send outcome is uncertain. "
                    "Verify the Sent folder before retrying."
                ),
                "error": type(exc).__name__,
                "draft_status": "send_ambiguous",
                "ticket_status": ticket.status,
            }

        if reconciled:
            now = datetime.now(timezone.utc)

            draft.status = "sent"
            draft.sent_at = now
            draft.provider_message_id = reconciled.get("id")
            ticket.status = "resolved"
            ticket.resolved_at = now

            connected_email = (gmail_service.get_authorization_status() or {}).get("connected_email")
            db.add(TicketMessage(
                ticket_id=ticket.id,
                sender_type="agent",
                sender_email=connected_email,
                sender_name="SmartTech Support Agent (Reconciled)",
                message_body=draft.body,
            ))
            db.add(AuditLog(
                user_id=None,
                action="auto_send_email_reconciled",
                entity_type="draft",
                entity_id=str(draft.id),
                details=f"Gmail confirmed delivery; provider_message_id={reconciled.get('id')}.",
            ))

            db.commit()

            logger.info(
                "Auto-send reconciled: Gmail confirms the message was sent."
            )

            return {
                "auto_sent": True,
                "eligible": True,
                "reconciled": True,
                "gmail_message_id": reconciled.get("id"),
                "reason": "Gmail confirmed that the message was sent.",
                "draft_status": "sent",
                "ticket_status": "resolved",
            }

        draft.status = "send_failed"

        if ticket.status == "awaiting_approval":
            ticket.status = "in_progress"

        db.commit()

        return {
            "auto_sent": False,
            "eligible": True,
            "error": type(exc).__name__,
            "reason": f"Live send execution failed ({type(exc).__name__}); Gmail confirmed no matching sent message.",
            "draft_status": "send_failed",
            "ticket_status": ticket.status,
        }
