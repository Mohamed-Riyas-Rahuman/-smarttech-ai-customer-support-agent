"""Live dashboard aggregates and append-only weekly snapshot helpers."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import String, cast, func, select, exists
from sqlalchemy.orm import Session
from app.models.agent import AgentExecution
from app.models.draft import Draft
from app.models.email import EmailMessage
from app.models.ticket import SupportTicket
from app.models.dashboard import DashboardSnapshot
from app.models.audit import AuditLog


def dashboard_aggregates(db: Session) -> dict[str, int]:
    active_ticket = SupportTicket.status.notin_(("resolved", "closed"))
    awaiting_draft = exists(select(Draft.id).where(
        Draft.ticket_id == SupportTicket.id, Draft.status == "pending_review"
    ))
    attention = active_ticket & (
        SupportTicket.priority.in_(("high", "urgent"))
        | SupportTicket.escalation_status.in_(("recommended", "escalated"))
        | (SupportTicket.status == "awaiting_approval")
        | awaiting_draft
    )
    return {
        "incoming_emails": db.scalar(select(func.count(EmailMessage.id)).where(
            func.lower(EmailMessage.sender) != func.lower(EmailMessage.recipient)
        )) or 0,
        "processed_emails": db.scalar(select(func.count(EmailMessage.id)).where(
            EmailMessage.is_processed.is_(True), EmailMessage.ticket_id.is_not(None),
            func.lower(EmailMessage.sender) != func.lower(EmailMessage.recipient)
        )) or 0,
        "ai_successes": db.scalar(select(func.count(func.distinct(AgentExecution.ticket_id))).where(
            AgentExecution.status == "completed"
        )) or 0,
        "ai_failures": db.scalar(select(func.count(AgentExecution.id)).where(
            AgentExecution.status == "failed"
        )) or 0,
        "pending_responses": db.scalar(select(func.count(Draft.id)).where(
            Draft.status == "pending_review"
        )) or 0,
        "tickets_awaiting_approval": db.scalar(select(func.count(func.distinct(SupportTicket.id))).where(
            active_ticket & ((SupportTicket.status == "awaiting_approval") | awaiting_draft)
        )) or 0,
        "sent_responses": db.scalar(select(func.count(Draft.id)).where(
            Draft.status == "sent",
            exists(select(AuditLog.id).where(
                AuditLog.entity_type == "draft",
                AuditLog.entity_id == cast(Draft.id, String),
                AuditLog.action.in_(("send_email_live_gmail", "auto_send_email")),
            )),
        )) or 0,
        "failed_sends": db.scalar(select(func.count(Draft.id)).where(
            Draft.status.in_(("failed", "send_failed"))
        )) or 0,
        "auto_sent": db.scalar(select(func.count(func.distinct(AuditLog.entity_id))).where(
            AuditLog.entity_type == "draft", AuditLog.action.in_(("auto_send_email", "auto_send_email_reconciled"))
        )) or 0,
        "blocked_auto_sends": db.scalar(select(func.count(Draft.id)).where(
            Draft.status == "pending_review", Draft.rejection_reason.is_not(None)
        )) or 0,
        "attention_needed": db.scalar(select(func.count(SupportTicket.id)).where(attention)) or 0,
        "high_priority": db.scalar(select(func.count(SupportTicket.id)).where(
            active_ticket, SupportTicket.priority == "high"
        )) or 0,
        "escalated": db.scalar(select(func.count(SupportTicket.id)).where(
            active_ticket, SupportTicket.escalation_status == "escalated"
        )) or 0,
    }


def save_snapshot(db: Session, *, snapshot_type: str, force: bool = False) -> DashboardSnapshot:
    """Save an immutable aggregate. Auto snapshots are serialized by PG advisory lock."""
    if snapshot_type == "automatic":
        db.execute(select(func.pg_advisory_xact_lock(731902041)))
        latest = db.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "automatic"
        ).order_by(DashboardSnapshot.created_at.desc()).limit(1))
        now = datetime.now(timezone.utc)
        if latest and latest.created_at + timedelta(days=7) > now:
            return latest
        period_start = latest.created_at if latest else now - timedelta(days=7)
        period_end = now
    else:
        now = datetime.now(timezone.utc)
        latest = db.scalar(select(DashboardSnapshot).order_by(DashboardSnapshot.created_at.desc()).limit(1))
        if not force and latest and latest.created_at >= now - timedelta(days=7):
            raise ValueError("A snapshot already exists in the current seven-day period.")
        period_start = latest.created_at if latest else now - timedelta(days=7)
        period_end = now
    snapshot = DashboardSnapshot(
        period_start=period_start, period_end=period_end,
        snapshot_type=snapshot_type, **dashboard_aggregates(db),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def snapshot_dict(snapshot: DashboardSnapshot) -> dict:
    return {key: getattr(snapshot, key) for key in (
        "id", "period_start", "period_end", "created_at", "snapshot_type",
        "incoming_emails", "processed_emails", "ai_successes", "ai_failures",
        "pending_responses", "tickets_awaiting_approval", "sent_responses",
        "failed_sends", "attention_needed", "high_priority", "escalated",
    )}

