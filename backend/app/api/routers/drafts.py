"""Draft and Approval Center router enforcing human-in-the-loop policies."""

from datetime import datetime, timezone
import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_current_user,
    get_db,
    require_agent_or_above,
    require_manager_or_admin,
)
from app.core.logging import logger
from app.models.audit import AuditLog
from app.models.draft import Draft
from app.models.agent import AgentExecution
from app.models.email import EmailMessage
from app.models.ticket import SupportTicket, TicketMessage
from app.models.user import User
from app.schemas.draft import (
    DraftApproveRequest,
    DraftCreate,
    DraftOut,
    DraftRejectRequest,
    DraftSendResponse,
    DraftUpdate,
)
from app.services.draft_policy import (
    DraftState,
    PolicyError,
    approve,
    assert_can_send,
    edit,
)
from app.services.integrations.gmail import gmail_service


router = APIRouter(prefix="/api/drafts", tags=["Drafts & Approvals"])


@router.get("", response_model=list[DraftOut])
def list_drafts(
    status_filter: str | None = Query(
        None,
        alias="status",
        description=(
            "Filter by draft status "
            "(pending_review, approved, rejected, sent, send_ambiguous)"
        ),
    ),
    ticket_id: int | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List email drafts for review, approval, or inspection."""

    query = select(Draft)

    if status_filter:
        query = query.where(Draft.status == status_filter)

    if ticket_id is not None:
        query = query.where(Draft.ticket_id == ticket_id)

    drafts = db.scalars(
        query.order_by(Draft.created_at.desc())
        .offset(skip)
        .limit(limit)
    ).all()
    output = []
    for draft in drafts:
        execution = db.scalar(select(AgentExecution).where(
            (AgentExecution.draft_id == draft.id) | (AgentExecution.ticket_id == draft.ticket_id)
        ).order_by(AgentExecution.created_at.desc()).limit(1)) if draft.ticket_id else None
        item = DraftOut.model_validate(draft).model_dump()
        item["confidence_score"] = execution.confidence_score if execution else None
        output.append(item)
    return output


@router.get("/{draft_id}", response_model=DraftOut)
def get_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve draft details by ID."""

    draft = db.scalar(
        select(Draft).where(Draft.id == draft_id)
    )

    if not draft:
        raise HTTPException(
            status_code=404,
            detail="Draft not found",
        )

    return draft


@router.post(
    "",
    response_model=DraftOut,
    status_code=status.HTTP_201_CREATED,
)
def create_draft(
    payload: DraftCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """Create a new response draft in pending_review status."""

    draft = Draft(
        ticket_id=payload.ticket_id,
        email_id=payload.email_id,
        recipient=payload.recipient,
        subject=payload.subject,
        body=payload.body,
        status="pending_review",
        citations=payload.citations,
    )

    db.add(draft)
    db.commit()
    db.refresh(draft)

    return draft


@router.patch("/{draft_id}", response_model=DraftOut)
def edit_draft(
    draft_id: int,
    payload: DraftUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """
    Edit draft content.

    Any edit invalidates previous approval and resets the status
    to pending_review.
    """

    draft = db.scalar(
        select(Draft).where(Draft.id == draft_id)
    )

    if not draft:
        raise HTTPException(
            status_code=404,
            detail="Draft not found",
        )

    state = DraftState(
        body=draft.body,
        status=draft.status,
        approved_by=draft.approved_by,
        approved_hash=draft.approved_body_hash,
    )

    try:
        new_state = edit(state, payload.body)
    except PolicyError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    draft.body = new_state.body
    draft.status = new_state.status
    draft.approved_by = None
    draft.approved_body_hash = None

    if payload.subject is not None:
        draft.subject = payload.subject

    if payload.recipient is not None:
        draft.recipient = payload.recipient

    db.add(
        AuditLog(
            user_id=current_user.id,
            action="edit_draft_invalidated_approval",
            entity_type="draft",
            entity_id=str(draft.id),
            details=(
                f"Draft {draft.id} edited by {current_user.email}. "
                "Previous approvals invalidated."
            ),
        )
    )

    db.commit()
    db.refresh(draft)

    from app.services.langfuse_events import record_approval_event
    record_approval_event(
        "draft_edited", current_user.role, db=db, ticket_id=draft.ticket_id
    )

    return draft


@router.post("/{draft_id}/approve", response_model=DraftOut)
def approve_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager_or_admin),
):
    """
    Approve a response draft.

    Approval is bound to the SHA-256 hash of the current body.
    Only a manager/reviewer or administrator may approve.
    """

    draft = db.scalar(
        select(Draft).where(Draft.id == draft_id)
    )

    if not draft:
        raise HTTPException(
            status_code=404,
            detail="Draft not found",
        )

    state = DraftState(
        body=draft.body,
        status=draft.status,
        approved_by=draft.approved_by,
        approved_hash=draft.approved_body_hash,
    )

    policy_role = (
        "admin"
        if current_user.role == "admin"
        else "reviewer"
    )

    try:
        new_state = approve(
            state,
            user_id=current_user.id,
            role=policy_role,
        )
    except PolicyError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    draft.status = new_state.status
    draft.approved_by = current_user.id
    draft.approved_body_hash = new_state.approved_hash
    draft.rejected_by = None
    draft.rejection_reason = None

    db.add(
        AuditLog(
            user_id=current_user.id,
            action="approve_draft",
            entity_type="draft",
            entity_id=str(draft.id),
            details=(
                f"Draft {draft.id} approved by "
                f"{current_user.email} ({current_user.role}). "
                f"Hash: {draft.approved_body_hash[:12]}..."
            ),
        )
    )

    db.commit()
    db.refresh(draft)

    from app.services.langfuse_events import record_approval_event
    record_approval_event(
        "draft_approved", current_user.role, db=db, ticket_id=draft.ticket_id
    )

    return draft


@router.post("/{draft_id}/reject", response_model=DraftOut)
def reject_draft(
    draft_id: int,
    payload: DraftRejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager_or_admin),
):
    """Reject a response draft with a mandatory reason."""

    draft = db.scalar(
        select(Draft).where(Draft.id == draft_id)
    )

    if not draft:
        raise HTTPException(
            status_code=404,
            detail="Draft not found",
        )

    if draft.status in ("sent", "send_ambiguous"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot reject a draft that has already been "
                "dispatched or whose delivery outcome is uncertain."
            ),
        )

    draft.status = "rejected"
    draft.approved_by = None
    draft.approved_body_hash = None
    draft.rejected_by = current_user.id
    draft.rejection_reason = payload.reason

    if draft.ticket_id:
        ticket = db.scalar(
            select(SupportTicket).where(
                SupportTicket.id == draft.ticket_id
            )
        )

        if ticket:
            ticket.status = "in_progress"

    db.add(
        AuditLog(
            user_id=current_user.id,
            action="reject_draft",
            entity_type="draft",
            entity_id=str(draft.id),
            details=(
                f"Draft {draft.id} rejected by "
                f"{current_user.email}. "
                f"Reason: {payload.reason}"
            ),
        )
    )

    db.commit()
    db.refresh(draft)

    from app.services.langfuse_events import record_approval_event
    record_approval_event(
        "draft_rejected", current_user.role, db=db, ticket_id=draft.ticket_id
    )

    return draft


def _get_original_email_thread(
    draft: Draft,
    db: Session,
) -> tuple[str | None, str | None, str | None]:
    """
    Find the original email's thread and provider message IDs.

    These values allow Gmail replies to remain associated with
    the original support conversation.
    """

    if draft.email:
        return (
            draft.email.thread_id,
            draft.email.rfc_message_id,
            _build_references(draft.email.references_header, draft.email.rfc_message_id),
        )

    if draft.ticket_id:
        original_email = db.scalar(
            select(EmailMessage)
            .where(
                EmailMessage.ticket_id == draft.ticket_id
            )
            .order_by(EmailMessage.received_at.asc())
        )

        if original_email:
            return (
                original_email.thread_id,
                original_email.rfc_message_id,
                _build_references(original_email.references_header, original_email.rfc_message_id),
            )

    return None, None, None


def _build_references(references: str | None, message_id: str | None) -> str | None:
    values = [part for part in (references or "").split() if part]
    if message_id and message_id not in values:
        values.append(message_id)
    return " ".join(values) or None


@router.post(
    "/{draft_id}/send",
    response_model=DraftSendResponse,
)
def send_approved_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """
    Send an approved draft through Gmail.

    Safety requirements:
    1. Verify approval and the approved body hash.
    2. Require Gmail authorization.
    3. Never simulate email delivery.
    4. Mark the draft send_ambiguous before contacting Gmail.
    5. Mark the draft sent only after a confirmed Gmail response.
    6. Never automatically retry an ambiguous send.
    """

    draft = db.scalar(
        select(Draft).where(Draft.id == draft_id)
    )

    if not draft:
        raise HTTPException(
            status_code=404,
            detail="Draft not found",
        )
    # Validate status, approval, and body hash first.
    state = DraftState(
        body=draft.body,
        status=draft.status,
        approved_by=draft.approved_by,
        approved_hash=draft.approved_body_hash,
    )

    approval_was_invalidated = db.scalar(select(AuditLog.id).where(
        AuditLog.entity_type == "draft",
        AuditLog.entity_id == str(draft.id),
        AuditLog.action == "edit_draft_invalidated_approval",
    ).limit(1)) is not None
    if draft.status in ("sent", "send_ambiguous") or (
        draft.status == "pending_review" and approval_was_invalidated
    ):
        raise HTTPException(status_code=409, detail="Draft state changed; refresh and review before sending.")

    try:
        assert_can_send(state)
    except PolicyError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Draft cannot be sent: {exc}",
        ) from exc

    # Validate the recipient before attempting delivery.
    if not draft.recipient or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", draft.recipient.strip()):
        raise HTTPException(
            status_code=400,
            detail="Draft has no valid recipient address.",
        )

    # Verify OAuth, actual grants, connected identity, and profile access
    # separately. A valid token alone is not enough to send in production.
    try:
        gmail_status = gmail_service.get_authorization_status() or {}
    except Exception as exc:
        logger.error(
            "Could not verify Gmail connection (%s)", type(exc).__name__
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not verify the connected Gmail account. "
                "No email was intentionally sent."
            ),
        ) from exc

    if not gmail_status.get("is_authorized"):
        raise HTTPException(
            status_code=503,
            detail=(
                "Gmail authorization is unavailable or expired. Reconnect Gmail "
                "before attempting to send."
            ),
        )

    if not gmail_status.get("has_send_permission"):
        raise HTTPException(status_code=503, detail="Gmail send permission is missing. Reauthorize Gmail before sending.")

    connected_email = (gmail_status.get("connected_email") or "").strip().lower()
    if not connected_email:
        raise HTTPException(status_code=503, detail="The connected Gmail profile could not be verified. No email was sent.")

    if draft.recipient.strip().lower() == connected_email:
        raise HTTPException(status_code=409, detail="Draft recipient matches the support mailbox.")

    # For ticket-linked drafts, only reply to the ticket's verified sender.
    # Free-standing drafts are still governed by reviewer authorization.
    if draft.ticket_id:
        ticket_owner = db.scalar(select(SupportTicket).where(SupportTicket.id == draft.ticket_id))
        approved_recipient = (ticket_owner.sender_email if ticket_owner else "") or (
            ticket_owner.customer.email if ticket_owner and ticket_owner.customer else ""
        )
        if not approved_recipient or draft.recipient.strip().lower() != approved_recipient.strip().lower():
            raise HTTPException(status_code=409, detail="Draft recipient does not match the ticket customer.")
        from app.services.response_validation import validate_draft_customer_identity
        identity_error = validate_draft_customer_identity(db, ticket_owner, draft.recipient, draft.body)
        if identity_error:
            raise HTTPException(status_code=409, detail=identity_error)

    thread_id, in_reply_to, references = _get_original_email_thread(
        draft,
        db,
    )

    # Persist the attempt before contacting Gmail.
    # A timeout after this point may mean Gmail accepted the email
    # even though the application did not receive the response.
    draft.send_attempts = (draft.send_attempts or 0) + 1
    draft.status = "send_ambiguous"

    db.add(
        AuditLog(
            user_id=current_user.id,
            action="send_email_attempt",
            entity_type="draft",
            entity_id=str(draft.id),
            details=(
                f"Live Gmail send attempt started for draft "
                f"{draft.id}, recipient {draft.recipient}."
            ),
        )
    )

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception(
            f"Could not record the send attempt for draft {draft_id}."
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not record the send attempt. "
                "Gmail was not called by this request."
            ),
        ) from exc

    # Call Gmail only after the attempt has been recorded.
    try:
        gmail_result = gmail_service.send_reply(
            to_email=draft.recipient,
            subject=draft.subject,
            body_text=draft.body,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
        )
    except Exception as exc:
        logger.warning("Gmail send outcome requires reconciliation for draft %s (%s)", draft_id, type(exc).__name__)
        try:
            gmail_result = gmail_service.reconcile_sent_message(
                to_email=draft.recipient,
                subject=draft.subject,
                thread_id=thread_id,
            )
        except Exception as reconciliation_error:
            db.add(AuditLog(
                user_id=current_user.id, action="send_email_ambiguous",
                entity_type="draft", entity_id=str(draft_id),
                details=f"Send result was uncertain and Gmail reconciliation failed ({type(reconciliation_error).__name__}).",
            ))
            db.commit()
            raise HTTPException(
                status_code=502,
                detail="Gmail send outcome is uncertain. Draft remains send_ambiguous; reconcile before retrying.",
            ) from exc

        if not isinstance(gmail_result, dict) or not gmail_result.get("id"):
            draft.status = "send_failed"
            if draft.ticket_id:
                ticket = db.get(SupportTicket, draft.ticket_id)
                if ticket and ticket.status == "awaiting_approval":
                    ticket.status = "in_progress"
            db.add(AuditLog(
                user_id=current_user.id, action="send_email_failed",
                entity_type="draft", entity_id=str(draft_id),
                details=f"Gmail Sent search confirmed no matching message after send error ({type(exc).__name__}).",
            ))
            db.commit()
            raise HTTPException(
                status_code=502,
                detail="Gmail confirmed no sent message. Draft is marked send_failed and may be reviewed before another attempt.",
            ) from exc
        # Reconciliation found a message ID, so run the normal sent-state and
        # audit finalization path below without issuing a second send.

    # A successful result must contain a Gmail message ID.
    # An empty or unexpected response is not proof of acceptance.
    if (
        not isinstance(gmail_result, dict)
        or not gmail_result.get("id")
    ):
        logger.error(
            f"Gmail returned no confirmed message ID for "
            f"draft {draft_id}: {gmail_result!r}"
        )

        db.add(
            AuditLog(
                user_id=current_user.id,
                action="send_email_ambiguous",
                entity_type="draft",
                entity_id=str(draft_id),
                details=(
                    f"Gmail returned no confirmed message ID for "
                    f"draft {draft_id}. Check Gmail Sent before retrying."
                ),
            )
        )

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                f"Could not record the missing Gmail message ID "
                f"for draft {draft_id}."
            )

        raise HTTPException(
            status_code=502,
            detail=(
                "Gmail returned no confirmed message ID. "
                "The draft remains send_ambiguous. Check Gmail Sent "
                "before considering another attempt."
            ),
        )

    # Gmail returned a message ID. Finalize the database state.
    now = datetime.now(timezone.utc)

    draft = db.scalar(
        select(Draft).where(Draft.id == draft_id)
    )

    if not draft:
        db.rollback()
        logger.error(
            f"Gmail accepted draft {draft_id}, but the draft "
            "record could not be reloaded."
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Gmail returned a message ID, but the draft record "
                "could not be reloaded. Reconcile Gmail Sent with "
                "the database before retrying."
            ),
        )

    draft.status = "sent"
    draft.sent_at = now

    if draft.ticket_id:
        db.add(
            TicketMessage(
                ticket_id=draft.ticket_id,
                sender_type="agent",
                sender_email=current_user.email,
                sender_name=current_user.full_name,
                message_body=draft.body,
            )
        )

        ticket = db.scalar(
            select(SupportTicket).where(
                SupportTicket.id == draft.ticket_id
            )
        )

        # Sending a reply does not automatically resolve an issue.
        if ticket and ticket.status == "resolved":
            ticket.status = "in_progress"
            ticket.resolved_at = None

    provider_id = gmail_result.get("id")
    provider_thread_id = gmail_result.get("thread_id")
    draft.provider_message_id = provider_id

    db.add(
        AuditLog(
            user_id=current_user.id,
            action="send_email_live_gmail",
            entity_type="draft",
            entity_id=str(draft.id),
            details=(
                f"Gmail accepted draft {draft.id} for "
                f"{draft.recipient}; "
                f"provider_message_id={provider_id}; "
                f"provider_thread_id={provider_thread_id}"
            ),
        )
    )

    try:
        db.commit()
    except Exception as exc:
        db.rollback()

        logger.exception(
            "Gmail accepted the message, but database finalization "
            f"failed for draft {draft_id}. Reconcile Gmail Sent "
            "before retrying."
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Gmail returned a message ID, but the database "
                "finalization failed. Check Gmail Sent and reconcile "
                "the draft before attempting another send."
            ),
        ) from exc

    from app.services.langfuse_events import record_approval_event
    record_approval_event(
        "approved_draft_sent", current_user.role, db=db, ticket_id=draft.ticket_id
    )

    return DraftSendResponse(
        status="sent",
        draft_id=draft.id,
        sent_at=now,
        mode="live_gmail",
        message=(
            f"Gmail accepted the email for {draft.recipient}. "
            "This confirms Gmail acceptance, not final inbox delivery."
        ),
    )

