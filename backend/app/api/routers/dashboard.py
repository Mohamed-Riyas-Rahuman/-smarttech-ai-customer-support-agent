"""Dashboard and Analytics router with real database metrics."""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_user, get_db, require_admin, require_manager_or_admin
from app.models.agent import AgentExecution
from app.models.audit import AuditLog
from app.models.customer import Customer
from app.models.device import Device
from app.models.draft import Draft
from app.models.email import EmailMessage
from app.models.ticket import SupportTicket, TicketMessage
from app.models.user import User
from app.schemas.dashboard import AnalyticsReport, DashboardStats
from app.services.dashboard_snapshots import dashboard_aggregates, save_snapshot, snapshot_dict
from app.models.dashboard import DashboardSnapshot

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard & Analytics"])


@router.get("/stats", response_model=DashboardStats)
def get_dashboard_stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Calculate live metrics from database tables for operations dashboard."""
    now = datetime.now(timezone.utc)
    one_day_ago = now - timedelta(days=1)
    seven_days_ago = now - timedelta(days=7)

    # Ticket statistics
    total_open = db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status.in_(["open", "in_progress"]))) or 0
    new_tickets = db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.created_at >= one_day_ago)) or 0
    awaiting_approval = db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status == "awaiting_approval")) or 0
    high_priority = db.scalar(select(func.count(SupportTicket.id)).where(
        SupportTicket.status.notin_(("resolved", "closed")), SupportTicket.priority == "high"
    )) or 0
    escalated = db.scalar(select(func.count(SupportTicket.id)).where(
        SupportTicket.status.notin_(("resolved", "closed")), SupportTicket.escalation_status == "escalated"
    )) or 0

    total_cust = db.scalar(select(func.count(Customer.id))) or 0
    total_dev = db.scalar(select(func.count(Device.id))) or 0
    total_tickets = db.scalar(select(func.count(SupportTicket.id))) or 0
    total_resolved = db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.status == "resolved")) or 0
    approved_drafts = db.scalar(select(func.count(Draft.id)).where(Draft.status == "approved")) or 0
    from app.models.knowledge import KnowledgeDocument
    knowledge_documents = db.scalar(select(func.count(KnowledgeDocument.id)).where(KnowledgeDocument.is_indexed.is_(True))) or 0
    total_drafts_pending = db.scalar(select(func.count(Draft.id)).where(Draft.status == "pending_review")) or 0

    # Operational Email & AI Metrics
    operational = dashboard_aggregates(db)
    incoming_emails = operational["incoming_emails"]
    processed_emails = operational["processed_emails"]
    ai_success = operational["ai_successes"]
    ai_failed = operational["ai_failures"]
    pending_responses = operational["pending_responses"]
    sent_responses = operational["sent_responses"]
    failed_sending = operational["failed_sends"]

    attention_filter = (
        SupportTicket.status.notin_(("resolved", "closed")) & (
        SupportTicket.priority.in_(["high", "urgent"])
        | SupportTicket.escalation_status.in_(["recommended", "escalated"])
        | (SupportTicket.status == "awaiting_approval")
        | exists(select(Draft.id).where(Draft.ticket_id == SupportTicket.id, Draft.status == "pending_review"))
        )
    )
    tickets_needing_attention_cnt = db.scalar(select(func.count(SupportTicket.id)).where(attention_filter)) or 0

    # Status distribution
    status_rows = db.execute(
        select(SupportTicket.status, func.count(SupportTicket.id)).group_by(SupportTicket.status)
    ).all()
    status_dist = [{"status": r[0], "count": r[1]} for r in status_rows]

    # Priority distribution
    priority_rows = db.execute(
        select(SupportTicket.priority, func.count(SupportTicket.id)).group_by(SupportTicket.priority)
    ).all()
    priority_dist = [{"priority": r[0], "count": r[1]} for r in priority_rows]

    message_rows = db.execute(select(
        TicketMessage.ticket_id, TicketMessage.sender_type, TicketMessage.created_at
    ).where(TicketMessage.sender_type.in_(("customer", "agent"))).order_by(
        TicketMessage.ticket_id, TicketMessage.created_at
    )).all()
    first_times = {}
    response_durations = []
    for ticket_id, sender_type, created_at in message_rows:
        state = first_times.setdefault(ticket_id, {"customer": None, "agent": None})
        if sender_type == "customer" and state["customer"] is None:
            state["customer"] = created_at
        elif sender_type == "agent" and state["customer"] is not None and state["agent"] is None:
            state["agent"] = created_at
            response_durations.append((created_at - state["customer"]).total_seconds() / 3600)
    avg_first_response = round(sum(response_durations) / len(response_durations), 2) if response_durations else 0.0

    # Ticket trend over past 7 days
    ticket_trend = []
    for i in range(6, -1, -1):
        day_start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        cnt = db.scalar(
            select(func.count(SupportTicket.id)).where(
                SupportTicket.created_at >= day_start,
                SupportTicket.created_at < day_end,
            )
        ) or 0
        ticket_trend.append({"date": day_start.strftime("%b %d"), "count": cnt})

    # Recent Audit Log / Activity
    recent_logs = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(8)).all()
    recent_activity = [
        {
            "id": log.id,
            "action": log.action,
            "details": log.details,
            "timestamp": log.created_at.isoformat(),
        }
        for log in recent_logs
    ]

    # Recent Agent Errors
    recent_err_execs = db.scalars(
        select(AgentExecution)
        .where((AgentExecution.status == "failed") | (AgentExecution.error_message.is_not(None)))
        .order_by(AgentExecution.created_at.desc())
        .limit(6)
    ).all()
    recent_agent_errors = [
        {
            "id": ex.id,
            "ticket_id": ex.ticket_id,
            "status": ex.status,
            "error_message": ex.error_message,
            "category": ex.issue_category,
            "timestamp": ex.created_at.isoformat(),
        }
        for ex in recent_err_execs
    ]

    # Top Tickets Requiring Attention
    attention_tickets = db.scalars(
        select(SupportTicket)
        .where(attention_filter)
        .order_by(SupportTicket.created_at.desc())
        .limit(6)
    ).all()
    tickets_requiring_attention_list = [
        {
            "id": t.id,
            "ticket_number": t.ticket_number,
            "subject": t.subject,
            "customer_name": t.customer.full_name if t.customer else None,
            "priority": t.priority,
            "status": t.status,
            "escalation_status": t.escalation_status,
            "created_at": t.created_at.isoformat(),
        }
        for t in attention_tickets
    ]

    # Top Pending Drafts
    pending_drafts = db.scalars(
        select(Draft)
        .where(Draft.status == "pending_review")
        .order_by(Draft.created_at.desc())
        .limit(6)
    ).all()
    pending_drafts_list = [
        {
            "id": d.id,
            "ticket_id": d.ticket_id,
            "recipient": d.recipient,
            "subject": d.subject,
            "body_preview": d.body[:150] if d.body else "",
            "citations": d.citations,
            "rejection_reason": d.rejection_reason,
            "confidence_score": (db.scalar(select(AgentExecution.confidence_score).where(
                (AgentExecution.draft_id == d.id) | (AgentExecution.ticket_id == d.ticket_id)
            ).order_by(AgentExecution.created_at.desc()).limit(1)) if d.ticket_id else None),
            "created_at": d.created_at.isoformat(),
        }
        for d in pending_drafts
    ]

    return DashboardStats(
        total_open_tickets=total_open,
        new_tickets=new_tickets,
        tickets_awaiting_approval=operational["tickets_awaiting_approval"],
        high_priority_tickets=operational["high_priority"],
        escalated_tickets=operational["escalated"],
        avg_first_response_time_hours=avg_first_response,
        total_customers=total_cust,
        total_devices=total_dev,
        total_tickets=total_tickets,
        total_resolved_tickets=total_resolved,
        approved_drafts_count=approved_drafts,
        knowledge_documents_count=knowledge_documents,
        total_drafts_pending=total_drafts_pending,
        recent_activity=recent_activity,
        status_distribution=status_dist,
        priority_distribution=priority_dist,
        ticket_trend=ticket_trend,
        incoming_emails_count=incoming_emails,
        processed_emails_count=processed_emails,
        ai_success_count=ai_success,
        ai_failed_count=ai_failed,
        pending_responses_count=pending_responses,
        sent_responses_count=sent_responses,
        failed_sending_count=failed_sending,
        auto_sent_count=operational["auto_sent"],
        blocked_auto_sends_count=operational["blocked_auto_sends"],
        tickets_requiring_attention_count=operational["attention_needed"],
        recent_agent_errors=recent_agent_errors,
        tickets_requiring_attention=tickets_requiring_attention_list,
        pending_drafts_list=pending_drafts_list,
    )


@router.get("/snapshots")
def list_dashboard_snapshots(db: Session = Depends(get_db), current_user: User = Depends(require_manager_or_admin)):
    rows = db.scalars(select(DashboardSnapshot).order_by(DashboardSnapshot.created_at.desc()).limit(52)).all()
    output = [snapshot_dict(row) for row in rows]
    for row in output:
        row["period_start"] = row["period_start"].isoformat()
        row["period_end"] = row["period_end"].isoformat()
        row["created_at"] = row["created_at"].isoformat()
    compare_keys = (
        "incoming_emails", "processed_emails", "ai_successes", "ai_failures",
        "pending_responses", "tickets_awaiting_approval", "sent_responses", "failed_sends",
    )
    for index, row in enumerate(output):
        row["comparison"] = ({key: row[key] - output[index + 1][key] for key in compare_keys}
                              if index + 1 < len(output) else None)
    return {"snapshots": output}


@router.post("/snapshots")
def create_dashboard_snapshot(
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    try:
        row = save_snapshot(db, snapshot_type="manual", force=force)
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail=str(exc))
    result = snapshot_dict(row)
    for key in ("period_start", "period_end", "created_at"):
        result[key] = result[key].isoformat()
    return result



@router.get("/analytics", response_model=AnalyticsReport)
def get_analytics_report(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Comprehensive performance analytics and AI drafting metrics."""
    now = datetime.now(timezone.utc)

    # 1. Volume by day (last 14 days)
    volume_by_day = []
    for i in range(13, -1, -1):
        day = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        cnt = db.scalar(
            select(func.count(SupportTicket.id)).where(
                SupportTicket.created_at >= day,
                SupportTicket.created_at < day + timedelta(days=1),
            )
        ) or 0
        volume_by_day.append({"date": day.strftime("%m/%d"), "tickets": cnt})

    # 2. Category distribution
    cat_rows = db.execute(
        select(SupportTicket.category, func.count(SupportTicket.id)).group_by(SupportTicket.category)
    ).all()
    cat_dist = [{"category": r[0].capitalize(), "count": r[1]} for r in cat_rows]

    # 3. Priority distribution
    prio_rows = db.execute(
        select(SupportTicket.priority, func.count(SupportTicket.id)).group_by(SupportTicket.priority)
    ).all()
    prio_dist = [{"priority": r[0].capitalize(), "count": r[1]} for r in prio_rows]

    # 4. Agent workload
    agents = db.scalars(select(User).where(User.role.in_(["agent", "manager"]))).all()
    agent_workload = []
    for ag in agents:
        assigned_cnt = db.scalar(
            select(func.count(SupportTicket.id)).where(
                SupportTicket.assigned_to_user_id == ag.id,
                SupportTicket.status.in_(["open", "in_progress", "awaiting_approval"]),
            )
        ) or 0
        resolved_cnt = db.scalar(
            select(func.count(SupportTicket.id)).where(
                SupportTicket.assigned_to_user_id == ag.id,
                SupportTicket.status == "resolved",
            )
        ) or 0
        agent_workload.append({
            "agent_id": ag.id,
            "name": ag.full_name,
            "role": ag.role,
            "active_tickets": assigned_cnt,
            "resolved_tickets": resolved_cnt,
        })

    # 5. AI metrics
    total_ai_runs = db.scalar(select(func.count(AgentExecution.id))) or 0
    successful_runs = db.scalar(select(func.count(AgentExecution.id)).where(AgentExecution.status == "completed")) or 0
    avg_conf = db.scalar(select(func.avg(AgentExecution.confidence_score))) or 0.0

    ai_metrics = {
        "total_executions": total_ai_runs,
        "success_rate": round((successful_runs / total_ai_runs * 100), 1) if total_ai_runs else None,
        "avg_confidence": round(float(avg_conf), 2) if total_ai_runs and avg_conf is not None else None,
        "avg_duration_ms": int(db.scalar(select(func.avg(AgentExecution.execution_time_ms))) or 0) if total_ai_runs else None,
    }

    # 6. Draft approval metrics
    total_drafts = db.scalar(select(func.count(Draft.id))) or 0
    approved_drafts = db.scalar(select(func.count(Draft.id)).where(Draft.status.in_(["approved", "sent"]))) or 0
    rejected_drafts = db.scalar(select(func.count(Draft.id)).where(Draft.status == "rejected")) or 0
    pending_drafts = db.scalar(select(func.count(Draft.id)).where(Draft.status == "pending_review")) or 0

    draft_metrics = {
        "total_drafts": total_drafts,
        "approved": approved_drafts,
        "rejected": rejected_drafts,
        "pending": pending_drafts,
        "approval_rate": round((approved_drafts / (approved_drafts + rejected_drafts) * 100), 1) if (approved_drafts + rejected_drafts) else None,
    }

    return AnalyticsReport(
        ticket_volume_by_day=volume_by_day,
        category_distribution=cat_dist,
        priority_distribution=prio_dist,
        agent_workload=agent_workload,
        ai_metrics=ai_metrics,
        draft_approval_metrics=draft_metrics,
    )
