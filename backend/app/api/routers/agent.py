"""AI Support Agent execution router using the existing grounded ReAct orchestration."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from app.api.dependencies import get_current_user, get_db, require_agent_or_above
from app.models.agent import AgentExecution
from app.models.email import EmailMessage
from app.models.draft import Draft
from app.models.ticket import SupportTicket
from app.models.user import User
from app.schemas.agent import AgentExecutionOut, AgentRunRequest, AgentRunResponse
from app.services.agents.orchestrator import AgentOrchestrator
from app.services.langchain_workflow.workflow import LangChainSupportWorkflow
from app.services.workflow_audit import record_workflow_execution, safe_workflow_error

router = APIRouter(prefix="/api/agent", tags=["AI Agent"])


@router.post("/run", response_model=AgentRunResponse)
def run_agent_for_ticket(
    payload: AgentRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """Execute LangChain customer support workflow with PostgreSQL pgvector retrieval and quality checks."""
    try:
        ticket = db.scalar(select(SupportTicket).where(SupportTicket.id == payload.ticket_id))
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        latest_email = db.scalar(
            select(EmailMessage)
            .where(EmailMessage.ticket_id == ticket.id)
            .order_by(EmailMessage.received_at.desc())
        )
        if latest_email:
            from datetime import datetime, timedelta, timezone
            started = datetime.now(timezone.utc)
            latest_email.processing_status = "processing"
            latest_email.processing_attempts = (latest_email.processing_attempts or 0) + 1
            latest_email.next_processing_attempt_at = None
            db.commit()
            try:
                outcome = LangChainSupportWorkflow(db).run_for_ticket(
                    ticket_id=payload.ticket_id,
                    force_regenerate=payload.force_regenerate,
                    custom_instructions=payload.custom_instructions,
                    email_id=latest_email.id,
                )
                latest_email.is_processed = True
                latest_email.processing_status = "sent" if outcome.status == "resolved" else "drafted"
                latest_email.processing_error = None
                latest_email.next_processing_attempt_at = None
                execution = record_workflow_execution(db, ticket, outcome=outcome)
                db.commit()
                return AgentRunResponse(
                    success=bool(outcome.generated_body.strip()),
                    generation_mode="langchain_grounded_email",
                    execution_id=execution.id,
                    ticket_id=ticket.id,
                    draft_id=outcome.draft_id,
                    category=ticket.category,
                    priority=ticket.priority,
                    escalation_recommended=outcome.requires_human_review,
                    confidence_score=outcome.confidence_score,
                    draft_body=outcome.generated_body,
                    citations=outcome.citations,
                    warnings=outcome.rejection_reasons,
                    execution_time_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                )
            except Exception as error:
                latest_email.is_processed = False
                latest_email.processing_status = "generation_failed"
                latest_email.processing_error = safe_workflow_error(error)
                retry_wait = timedelta(days=1) if "daily free-model request limit" in latest_email.processing_error else timedelta(minutes=1)
                latest_email.next_processing_attempt_at = datetime.now(timezone.utc) + retry_wait
                has_usable_draft = db.scalar(
                    select(Draft.id).where(
                        Draft.ticket_id == ticket.id,
                        Draft.status == "pending_review",
                        Draft.body.is_not(None),
                        func.length(func.trim(Draft.body)) > 0,
                    ).limit(1)
                )
                if not has_usable_draft:
                    ticket.status = "in_progress"
                record_workflow_execution(db, ticket, error=error)
                db.commit()
                raise HTTPException(status_code=500, detail=safe_workflow_error(error)) from error
        return AgentOrchestrator(db).run_for_ticket(
            ticket_id=payload.ticket_id,
            force_regenerate=payload.force_regenerate,
            custom_instructions=payload.custom_instructions,
        )
    except Exception as e:
        db.rollback()
        if isinstance(e, HTTPException):
            raise
        # Avoid returning provider exception text, which can contain request
        # metadata. The orchestrator records failed executions in PostgreSQL.
        raise HTTPException(status_code=500, detail=f"Agent execution failed ({type(e).__name__}).") from e


@router.get("/executions/{ticket_id}", response_model=list[AgentExecutionOut])
def get_agent_executions(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve full execution trace and logs for a ticket."""
    executions = db.scalars(
        select(AgentExecution)
        .options(selectinload(AgentExecution.tool_executions))
        .where(AgentExecution.ticket_id == ticket_id)
        .order_by(AgentExecution.created_at.desc())
    ).all()
    return executions
