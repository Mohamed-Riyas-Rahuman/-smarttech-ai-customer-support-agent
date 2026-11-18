"""Support Ticket management router."""
from app.models.email import EmailMessage
from app.services.integrations.gmail import gmail_service
from datetime import datetime, timezone
import random
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from app.api.dependencies import get_current_user, get_db, require_agent_or_above
from app.models.audit import AuditLog
from app.models.customer import Customer
from app.models.draft import Draft
from app.models.ticket import SupportTicket, TicketMessage
from app.models.user import User
from app.schemas.ticket import TicketCreate, TicketDetailOut, TicketMessageCreate, TicketMessageOut, TicketOut, TicketUpdate

router = APIRouter(prefix="/api/tickets", tags=["Tickets"])


@router.get("", response_model=list[TicketOut])
def list_tickets(
    status_filter: str | None = Query(None, alias="status"),
    priority_filter: str | None = Query(None, alias="priority"),
    category_filter: str | None = Query(None, alias="category"),
    assigned_to: int | None = Query(None),
    customer_id: int | None = Query(None),
    q: str | None = Query(None, description="Search subject or ticket number"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List support tickets with multiple query filters and pagination."""
    query = select(SupportTicket).options(selectinload(SupportTicket.customer))

    if status_filter:
        query = query.where(SupportTicket.status == status_filter)
    if priority_filter:
        query = query.where(SupportTicket.priority == priority_filter)
    if category_filter:
        query = query.where(SupportTicket.category == category_filter)
    if assigned_to:
        query = query.where(SupportTicket.assigned_to_user_id == assigned_to)
    if customer_id:
        query = query.where(SupportTicket.customer_id == customer_id)
    if q:
        term = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(SupportTicket.subject).like(term),
                func.lower(SupportTicket.ticket_number).like(term),
            )
        )

    return db.scalars(query.order_by(SupportTicket.created_at.desc()).offset(skip).limit(limit)).all()


@router.get("/{ticket_id}", response_model=TicketDetailOut)
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve full ticket details with complete message history."""
    ticket = db.scalar(
        select(SupportTicket)
        .options(
            selectinload(SupportTicket.customer),
            selectinload(SupportTicket.messages),
            selectinload(SupportTicket.drafts),
        )
        .where(SupportTicket.id == ticket_id)
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    has_pending = any(d.status == "pending_review" for d in ticket.drafts)
    detail = TicketDetailOut.model_validate(ticket)
    # For Gmail-origin messages, an unverified From display name/signature must
    # not be treated as the customer's verified identity in the ticket UI.
    if any(
        message.sender_type == "customer" and message.sender_name == "there"
        for message in ticket.messages
    ):
        detail.customer_name = "Customer"
    detail.has_pending_draft = has_pending
    return detail


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """Create a new support ticket with optional initial message."""
    cust = db.scalar(select(Customer).where(Customer.id == payload.customer_id))
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    ticket_num = f"TCK-2026-{random.randint(100, 999)}"
    while db.scalar(select(SupportTicket).where(SupportTicket.ticket_number == ticket_num)):
        ticket_num = f"TCK-2026-{random.randint(100, 999)}"

    ticket = SupportTicket(
        ticket_number=ticket_num,
        customer_id=payload.customer_id,
        order_id=payload.order_id,
        device_id=payload.device_id,
        assigned_to_user_id=current_user.id,
        subject=payload.subject,
        priority=payload.priority,
        category=payload.category,
        status="open",
        sentiment="neutral",
        escalation_status="none",
    )
    db.add(ticket)
    db.flush()

    if payload.initial_message:
        msg = TicketMessage(
            ticket_id=ticket.id,
            sender_type="agent",
            sender_email=current_user.email,
            sender_name=current_user.full_name,
            message_body=payload.initial_message,
            is_internal_note=False,
        )
        db.add(msg)

    db.add(AuditLog(
        user_id=current_user.id,
        action="create_ticket",
        entity_type="ticket",
        entity_id=str(ticket.id),
        details=f"Created ticket {ticket.ticket_number} for customer {cust.email}",
    ))

    db.commit()
    db.refresh(ticket)
    return ticket


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """Update ticket status, priority, category, or agent assignment."""
    ticket = db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    updates = payload.model_dump(exclude_unset=True)
    if "status" in updates and updates["status"] == "resolved" and ticket.status != "resolved":
        ticket.resolved_at = datetime.now(timezone.utc)

    for field, val in updates.items():
        setattr(ticket, field, val)

    db.add(AuditLog(
        user_id=current_user.id,
        action="update_ticket",
        entity_type="ticket",
        entity_id=str(ticket.id),
        details=f"Updated ticket {ticket.ticket_number}: {list(updates.keys())}",
    ))

    db.commit()
    db.refresh(ticket)
    return ticket


@router.post(
    "/{ticket_id}/messages",
    response_model=TicketMessageOut,
    status_code=status.HTTP_201_CREATED,
)
def add_ticket_message(
    ticket_id: int,
    payload: TicketMessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """Send a customer reply through Gmail or save an internal note."""

    ticket = db.scalar(
        select(SupportTicket)
        .options(selectinload(SupportTicket.customer))
        .where(SupportTicket.id == ticket_id)
    )

    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    message_body = payload.message_body.strip()

    if not message_body:
        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty.",
        )

    # Internal notes are saved locally and never emailed.
    if payload.is_internal_note:
        msg = TicketMessage(
            ticket_id=ticket.id,
            sender_type="agent",
            sender_email=current_user.email,
            sender_name=current_user.full_name,
            message_body=message_body,
            is_internal_note=True,
        )

        db.add(msg)

        if ticket.status == "open":
            ticket.status = "in_progress"

        db.commit()
        db.refresh(msg)
        return msg

    # Resolve the customer's email address.
    customer_email = (
        ticket.sender_email
        or (ticket.customer.email if ticket.customer else None)
    )

    if not customer_email:
        raise HTTPException(
            status_code=400,
            detail="Cannot send reply: customer email address is missing.",
        )

    # Find the original incoming email for Gmail threading.
    original_email = db.scalar(
        select(EmailMessage)
        .where(
            EmailMessage.ticket_id == ticket.id,
            func.lower(EmailMessage.sender) == customer_email.lower(),
        )
        .order_by(EmailMessage.received_at.asc())
    )

    if not original_email:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot send reply: the original Gmail message "
                "was not found for this ticket."
            ),
        )

    if not gmail_service.is_authorized():
        raise HTTPException(
            status_code=503,
            detail=(
                "Gmail is not authorized. Complete Gmail authorization "
                "before sending manual replies."
            ),
        )

    # Send the actual email before recording it as a sent reply.
    try:
        gmail_result = gmail_service.send_reply(
            to_email=customer_email,
            subject=ticket.subject,
            body_text=message_body,
            thread_id=original_email.thread_id,
            in_reply_to=original_email.provider_message_id,
            references=original_email.provider_message_id,
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail=(
                "Gmail could not confirm the reply. Check Gmail Sent "
                "before retrying, to avoid sending a duplicate."
            ),
        )

    if not isinstance(gmail_result, dict) or not gmail_result.get("id"):
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail=(
                "Gmail did not return a message ID. Check Gmail Sent "
                "before retrying."
            ),
        )

    # Gmail accepted the message. Record it in the ticket thread.
    msg = TicketMessage(
        ticket_id=ticket.id,
        sender_type="agent",
        sender_email=current_user.email,
        sender_name=current_user.full_name,
        message_body=message_body,
        is_internal_note=False,
    )

    db.add(msg)

    if ticket.status == "open":
        ticket.status = "in_progress"

    db.add(
        AuditLog(
            user_id=current_user.id,
            action="send_manual_reply",
            entity_type="ticket",
            entity_id=str(ticket.id),
            details=(
                f"Manual reply sent to {customer_email}; "
                f"Gmail message ID: {gmail_result['id']}"
            ),
        )
    )

    db.commit()
    db.refresh(msg)

    return msg
