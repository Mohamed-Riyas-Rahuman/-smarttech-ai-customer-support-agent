"""Initial models: deduplicated emails and drafts with approval state."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (UniqueConstraint("provider_message_id", name="uq_email_provider_msg"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_message_id: Mapped[str] = mapped_column(String(255))
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    sender: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(998), default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("email_messages.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending_review")
    approved_by: Mapped[int | None] = mapped_column(nullable=True)
    approved_body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    send_attempts: Mapped[int] = mapped_column(default=0)
