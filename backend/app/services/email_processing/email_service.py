"""Email ingestion, extraction, and context assembly service."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.customer import Customer
from app.models.device import Device
from app.models.email import EmailMessage
from app.models.order import Order
from app.models.ticket import SupportTicket, TicketMessage


@dataclass
class CustomerEmailPayload:
    """Standardized email payload passed directly into the LangChain workflow."""
    email_id: int
    ticket_id: int
    customer_id: Optional[int]
    sender_email: str
    sender_name: str
    subject: str
    body_text: str
    thread_id: str
    provider_message_id: str
    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    customer_context: Dict[str, Any] = field(default_factory=dict)
    matched_device_id: Optional[int] = None
    matched_order_id: Optional[int] = None


class EmailService:
    """Processes incoming emails, manages customer/ticket associations, and extracts context."""

    def __init__(self, db: Session):
        self.db = db

    def capture_incoming_email(
        self,
        sender: str,
        subject: str,
        body_text: str,
        thread_id: Optional[str] = None,
        provider_message_id: Optional[str] = None,
        recipient: str = "support@smarttech.local",
        body_html: Optional[str] = None,
    ) -> CustomerEmailPayload:
        """
        Capture and store an incoming customer email, link/create customer and ticket,
        and assemble full context for LangChain processing.
        """
        # Clean sender and extract name and email
        _header_name, sender_email = self._parse_sender(sender)
        prov_id = provider_message_id or f"msg_{datetime.now(timezone.utc).timestamp()}_{sender_email}"
        th_id = thread_id or prov_id

        # 1. Deduplication check on EmailMessage
        existing_email = self.db.scalar(
            select(EmailMessage).where(EmailMessage.provider_message_id == prov_id)
        )
        if existing_email and existing_email.ticket_id:
            logger.info("Previously ingested email found; reusing its ticket context.")
            return self.extract_context_for_ticket(existing_email.ticket_id, current_email=existing_email)

        # 2. Find or create customer
        customer = self._get_or_create_customer(sender_email)

        # 3. Match or create support ticket
        ticket = self._find_or_create_ticket(customer, subject, th_id)

        # 4. Save EmailMessage
        email_record = existing_email or EmailMessage(
            provider_message_id=prov_id,
            thread_id=th_id,
            ticket_id=ticket.id,
            sender=sender_email,
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            is_processed=False,
            received_at=datetime.now(timezone.utc),
        )
        if not existing_email:
            self.db.add(email_record)
            self.db.flush()

        # 5. Append message to ticket conversation history
        ticket_msg = TicketMessage(
            ticket_id=ticket.id,
            sender_type="customer",
            sender_email=sender_email,
            message_body=body_text,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(ticket_msg)
        self.db.commit()
        self.db.refresh(ticket)
        self.db.refresh(email_record)

        # 6. Assemble context
        return self.extract_context_for_ticket(ticket.id, current_email=email_record)

    def extract_context_for_ticket(
        self,
        ticket_id: int,
        current_email: Optional[EmailMessage] = None,
    ) -> CustomerEmailPayload:
        """Extract all conversation context and customer relationship data for a ticket."""
        ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
        if not ticket:
            raise ValueError(f"SupportTicket {ticket_id} not found.")

        # If current_email not provided, take the latest email for this ticket
        if not current_email:
            current_email = self.db.scalar(
                select(EmailMessage)
                .where(EmailMessage.ticket_id == ticket_id)
                .order_by(EmailMessage.received_at.desc())
            )

        # Address from the current message is authoritative for this reply.
        sender_email = (current_email.sender if current_email else ticket.sender_email) or "unknown@customer.local"
        customer = ticket.customer

        # Only use a name already stored on the matched customer record. Names
        # from untrusted From headers are never used in generated replies.
        verified_name = (
            (customer.full_name or "").strip()
            if customer and customer.email.strip().casefold() == sender_email.strip().casefold()
            else ""
        )
        sender_name = verified_name if verified_name and verified_name.casefold() != "customer" else "there"
        subject = ticket.subject
        body_text = current_email.body_text if current_email else (
            ticket.messages[-1].message_body if ticket.messages else ""
        )

        # Build conversation history
        history: List[Dict[str, str]] = []
        for msg in ticket.messages:
            history.append({
                "sender_type": msg.sender_type,
                "sender_email": msg.sender_email or "",
                "message": msg.message_body,
                "timestamp": msg.created_at.isoformat() if msg.created_at else "",
            })

        # Customer device/order context
        customer_ctx: Dict[str, Any] = {
            "customer_id": customer.id if customer else None,
            "name": sender_name,
            "email": sender_email,
            "phone": customer.phone if customer else None,
            "tier": "standard",
            "orders": [],
            "devices": [],
        }

        matched_device_id = ticket.device_id
        matched_order_id = ticket.order_id

        if customer:
            # Query recent orders
            orders = self.db.scalars(
                select(Order).where(Order.customer_id == customer.id).order_by(Order.order_date.desc()).limit(3)
            ).all()
            for o in orders:
                customer_ctx["orders"].append({
                    "order_number": o.order_number,
                    "status": o.status,
                    "date": o.order_date.isoformat() if o.order_date else "",
                    "total_amount": float(o.total_amount) if o.total_amount else 0.0,
                })
                if not matched_order_id:
                    matched_order_id = o.id

            # Query registered devices
            devices = self.db.scalars(
                select(Device).where(Device.customer_id == customer.id).limit(5)
            ).all()
            for d in devices:
                customer_ctx["devices"].append({
                    "serial_number": d.serial_number,
                    "product_name": d.product.name if d.product else "SmartTech Device",
                    "status": "registered" if d.is_registered else "unregistered",
                })
                if not matched_device_id:
                    matched_device_id = d.id

        return CustomerEmailPayload(
            email_id=current_email.id if current_email else 0,
            ticket_id=ticket.id,
            customer_id=customer.id if customer else None,
            sender_email=sender_email,
            sender_name=sender_name,
            subject=subject,
            body_text=body_text,
            thread_id=current_email.thread_id if current_email else f"ticket_{ticket.id}",
            provider_message_id=current_email.provider_message_id if current_email else f"ticket_{ticket.id}",
            conversation_history=history,
            customer_context=customer_ctx,
            matched_device_id=matched_device_id,
            matched_order_id=matched_order_id,
        )

    def _get_or_create_customer(self, email: str, name: str = "Customer") -> Customer:
        """Find customer by email or create new profile."""
        clean_email = email.strip().lower()
        customer = self.db.scalar(select(Customer).where(Customer.email == clean_email))
        if not customer:
            customer = Customer(
                email=clean_email,
                full_name="Customer",
                created_at=datetime.now(timezone.utc),
            )
            self.db.add(customer)
            self.db.commit()
            self.db.refresh(customer)
        return customer

    def _find_or_create_ticket(self, customer: Customer, subject: str, thread_id: str) -> SupportTicket:
        """Match existing open ticket via thread or create a new ticket."""
        # 1. Match by thread_id via EmailMessage
        existing_email = self.db.scalar(
            select(EmailMessage).where(EmailMessage.thread_id == thread_id)
        )
        if existing_email and existing_email.ticket_id:
            ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == existing_email.ticket_id))
            if ticket and ticket.status not in {"closed"} and ticket.customer_id == customer.id:
                logger.info("Matched incoming email to an active ticket by thread.")
                return ticket

        # 2. Create new ticket
        ticket_number = f"TICK-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{abs(hash(thread_id)) % 10000:04d}"
        ticket = SupportTicket(
            ticket_number=ticket_number,
            customer_id=customer.id,
            subject=subject or "Customer Support Inquiry",
            status="open",
            priority="medium",
            category="general",
            sentiment="neutral",
            created_at=datetime.now(timezone.utc),
        )
        ticket.sender_email = customer.email
        self.db.add(ticket)
        self.db.commit()
        self.db.refresh(ticket)
        logger.info("Created a new support ticket for the resolved sender account.")
        return ticket

    def _parse_sender(self, raw_sender: str) -> tuple[str, str]:
        """Extract (name, email) from raw From header like 'Jane Doe <jane@example.com>'."""
        if not raw_sender:
            return ("Valued Customer", "unknown@customer.local")
        match = re.search(r"^(.*?)\s*<([^>]+)>$", raw_sender.strip())
        if match:
            name = match.group(1).strip().strip('"\'')
            email = match.group(2).strip().lower()
            return (name or email.split("@")[0], email)
        clean = raw_sender.strip().lower()
        return (clean.split("@")[0].capitalize(), clean)

