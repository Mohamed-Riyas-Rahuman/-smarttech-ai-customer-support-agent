"""Append-only aggregate dashboard snapshots for historical reporting."""
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class DashboardSnapshot(Base):
    __tablename__ = "dashboard_snapshots"
    __table_args__ = (Index("ix_dashboard_snapshots_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    incoming_emails: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_emails: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_responses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tickets_awaiting_approval: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_responses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_sends: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attention_needed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    escalated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

