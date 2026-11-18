"""Ticket status management service."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.ticket import SupportTicket


class TicketService:
    """Maintains correct ticket statuses across the support lifecycle."""

    def __init__(self, db: Session):
        self.db = db

    def update_after_workflow(
        self,
        ticket_id: int,
        is_sent: bool,
        sentiment: Optional[str] = None,
        category: Optional[str] = None,
    ) -> SupportTicket:
        """
        Update ticket status based on workflow outcome:
        - If sent: Mark as 'resolved'
        - If saved as draft: Mark as 'pending_approval' (Pending Human Approval)
        """
        ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
        if not ticket:
            raise ValueError(f"SupportTicket {ticket_id} not found.")

        if is_sent:
            ticket.status = "resolved"
            ticket.resolved_at = datetime.now(timezone.utc)
            logger.info("Marked ticket %s as 'resolved'", ticket.ticket_number)
        else:
            ticket.status = "awaiting_approval"  # Pending Human Approval
            logger.info("Marked ticket %s as 'awaiting_approval' (Pending Human Approval)", ticket.ticket_number)

        if sentiment:
            ticket.sentiment = sentiment
        if category:
            ticket.category = category

        ticket.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(ticket)
        return ticket

    def mark_resolved(self, ticket_id: int) -> SupportTicket:
        """Mark ticket explicitly as resolved upon manual agent approval and sending."""
        ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
        if not ticket:
            raise ValueError(f"SupportTicket {ticket_id} not found.")

        ticket.status = "resolved"
        ticket.resolved_at = datetime.now(timezone.utc)
        ticket.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(ticket)
        logger.info("Ticket %s marked resolved after manual approval and dispatch.", ticket.ticket_number)
        return ticket

