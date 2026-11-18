"""SupportTicket and TicketMessage models."""
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_number: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    customer_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("customers.id"), nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    device_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("devices.id"), nullable=True, index=True)
    assigned_to_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="open", index=True)  # open, in_progress, awaiting_approval, resolved, closed
    priority: Mapped[str] = mapped_column(String(50), default="medium", index=True)  # low, medium, high, urgent
    category: Mapped[str] = mapped_column(String(50), default="general", index=True)  # order, warranty, technical, returns, billing, general
    sentiment: Mapped[str] = mapped_column(String(50), default="neutral")  # positive, neutral, negative, frustrated
    escalation_status: Mapped[str] = mapped_column(String(50), default="none", index=True)  # none, recommended, escalated

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer = relationship("Customer", back_populates="tickets")
    order = relationship("Order", back_populates="tickets")
    device = relationship("Device", back_populates="tickets")
    assigned_user = relationship("User", foreign_keys=[assigned_to_user_id])
    messages = relationship("TicketMessage", back_populates="ticket", cascade="all, delete-orphan", order_by="TicketMessage.created_at")
    drafts = relationship("Draft", back_populates="ticket", cascade="all, delete-orphan")
    agent_executions = relationship("AgentExecution", back_populates="ticket", cascade="all, delete-orphan")

    def __init__(self, **kwargs):
        sender_email = kwargs.pop("sender_email", None)
        super().__init__(**kwargs)
        self._sender_email = sender_email

    @property
    def sender_email(self) -> str | None:
        if hasattr(self, "_sender_email") and self._sender_email:
            return self._sender_email
        if self.messages:
            for m in self.messages:
                if m.sender_type == "customer" and m.sender_email:
                    return m.sender_email
        return self.customer.email if self.customer else None

    @sender_email.setter
    def sender_email(self, val: str | None) -> None:
        self._sender_email = val


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, ForeignKey("support_tickets.id"), nullable=False, index=True)
    sender_type: Mapped[str] = mapped_column(String(50), default="customer")  # customer, agent, system, ai
    sender_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal_note: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    ticket = relationship("SupportTicket", back_populates="messages")
