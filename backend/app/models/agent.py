"""AgentExecution and ToolExecution models for AI observability and auditing."""
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class AgentExecution(Base):
    __tablename__ = "agent_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, ForeignKey("support_tickets.id"), nullable=False, index=True)
    draft_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("drafts.id"), nullable=True)

    status: Mapped[str] = mapped_column(String(50), default="completed", index=True)  # running, completed, failed
    user_intent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    priority_recommendation: Mapped[str | None] = mapped_column(String(50), nullable=True)
    escalation_recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    ticket = relationship("SupportTicket", back_populates="agent_executions")
    tool_executions = relationship("ToolExecution", back_populates="agent_execution", cascade="all, delete-orphan")


class ToolExecution(Base):
    __tablename__ = "tool_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_execution_id: Mapped[int] = mapped_column(Integer, ForeignKey("agent_executions.id"), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input_parameters: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded input
    output_result: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded result
    status: Mapped[str] = mapped_column(String(50), default="success")  # success, error
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    agent_execution = relationship("AgentExecution", back_populates="tool_executions")
