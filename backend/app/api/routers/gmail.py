"""Gmail integration router: OAuth authorization, inbox synchronization, and live messaging."""
import uuid
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_user, get_db, require_admin, require_agent_or_above
from app.core.config import settings
from app.core.logging import logger
from app.models.audit import AuditLog, SystemSetting
from app.models.customer import Customer
from app.models.draft import Draft
from app.models.email import EmailMessage
from app.models.ticket import SupportTicket, TicketMessage
from app.models.user import User
from app.services.langchain_workflow.workflow import LangChainSupportWorkflow
from app.services.auto_send import process_auto_send_for_draft
from app.services.integrations.gmail import gmail_service
from app.services.workflow_audit import record_workflow_execution

router = APIRouter(prefix="/api/gmail", tags=["Gmail Integration"])


def _record_successful_authorization(db: Session, user_id: int | None = None) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == "gmail_last_authorized_at"))
    if row:
        row.value = timestamp
    else:
        db.add(SystemSetting(key="gmail_last_authorized_at", value=timestamp, description="Last verified Gmail OAuth authorization time"))
    db.add(AuditLog(user_id=user_id, action="gmail_authorized", entity_type="integration", entity_id="gmail", details="Gmail OAuth account profile and required scopes verified."))
    db.commit()


def _consume_oauth_state(db: Session, state: str) -> str | None:
    key = "gmail_oauth_state:" + hashlib.sha256(state.encode()).hexdigest()
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == key).with_for_update())
    if not row:
        return None
    try:
        state_data = json.loads(row.value)
        valid_age = 0 <= time.time() - float(state_data["issued_at"]) <= 600
        verifier = state_data.get("code_verifier")
    except (TypeError, ValueError):
        valid_age = False
    db.delete(row)
    db.commit()
    return verifier if valid_age and verifier else None


class GmailAuthUrlResponse(BaseModel):
    authorization_url: str
    state: str
    required_scopes: List[str]


class GmailAuthCallbackRequest(BaseModel):
    code: str
    state: str
    redirect_uri: Optional[str] = None


class GmailSyncResponse(BaseModel):
    status: str
    synced_emails_count: int
    tickets_created_or_updated: List[Dict[str, Any]]
    details: str
    synced_count: Optional[int] = None
    tickets_created: Optional[int] = None

    @model_validator(mode="after")
    def populate_aliases(self) -> "GmailSyncResponse":
        if self.synced_count is None:
            self.synced_count = self.synced_emails_count
        if self.tickets_created is None:
            self.tickets_created = len(self.tickets_created_or_updated)
        return self


@router.get("/status")
def get_gmail_status(current_user: User = Depends(get_current_user)):
    """Inspect safe Gmail OAuth and connectivity status without disclosing secrets."""
    return gmail_service.get_authorization_status()


@router.get("/worker/status")
def get_sync_worker_status(current_user: User = Depends(get_current_user)):
    """Return operational status of the background Gmail sync worker."""
    from app.services.sync_worker import get_worker_status
    return get_worker_status()


@router.post("/worker/sync-now")
def trigger_manual_sync(current_user: User = Depends(require_agent_or_above)):
    """Manually trigger one Gmail sync cycle immediately (agent or above)."""
    from app.services.sync_worker import run_sync_once
    result = run_sync_once()
    return result


@router.get("/auth-url", response_model=GmailAuthUrlResponse)
def get_auth_url(
    redirect_uri: str = Query(settings.gmail_redirect_uri),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Generate OAuth2 authorization URL for Gmail permissions (Admin only)."""
    if redirect_uri != settings.gmail_redirect_uri:
        raise HTTPException(status_code=400, detail="Redirect URI must match GMAIL_REDIRECT_URI.")
    if not gmail_service.has_credentials_file():
        raise HTTPException(
            status_code=400,
            detail="Gmail OAuth client file is not configured. Add it at the configured credentials path.",
        )
    try:
        url, state, code_verifier = gmail_service.get_authorization_url(redirect_uri=redirect_uri)
        db.execute(delete(SystemSetting).where(
            SystemSetting.key.like("gmail_oauth_state:%"),
            SystemSetting.updated_at < datetime.now(timezone.utc) - timedelta(days=1),
        ))
        key = "gmail_oauth_state:" + hashlib.sha256(state.encode()).hexdigest()
        db.add(SystemSetting(
            key=key,
            value=json.dumps({"issued_at": time.time(), "code_verifier": code_verifier}),
            description="Short-lived Gmail OAuth state and PKCE verifier",
            is_secret=True,
        ))
        db.commit()
        return GmailAuthUrlResponse(
            authorization_url=url,
            state=state,
            required_scopes=gmail_service.scopes,
        )
    except ValueError as e:
        logger.warning("Gmail OAuth client preflight failed (%s)", type(e).__name__)
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error("Failed to generate Gmail authorization URL (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Could not start Gmail authorization. Check the OAuth client configuration and sanitized server status.")


@router.get("/oauth/callback", include_in_schema=False)
def gmail_oauth_redirect(code: str | None = None, state: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    """Google redirect endpoint. The single-use state is the admin-initiated authorization proof."""
    if error or not code or not state:
        return RedirectResponse("/app/?gmail_connection=failed", status_code=303)
    code_verifier = _consume_oauth_state(db, state)
    if not code_verifier:
        return RedirectResponse("/app/?gmail_connection=failed", status_code=303)
    redirect_uri = settings.gmail_redirect_uri
    try:
        gmail_service.complete_oauth_flow(code=code, state=state, redirect_uri=redirect_uri, code_verifier=code_verifier)
        status = gmail_service.get_authorization_status()
        if not status.get("is_authorized") or not status.get("permissions_sufficient") or not status.get("connected_email"):
            return RedirectResponse("/app/?gmail_connection=verification_failed", status_code=303)
        _record_successful_authorization(db)
        from app.services.sync_worker import wake_worker
        wake_worker()
        return RedirectResponse("/app/?gmail_connection=connected", status_code=303)
    except Exception as exc:
        logger.warning("Gmail OAuth callback failed (%s)", gmail_service._classify_oauth_error(exc))
        return RedirectResponse("/app/?gmail_connection=failed", status_code=303)


@router.post("/oauth/callback")
def complete_oauth(
    payload: GmailAuthCallbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Complete OAuth code exchange and save credentials (Admin only)."""
    redirect_uri = payload.redirect_uri or settings.gmail_redirect_uri
    if redirect_uri != settings.gmail_redirect_uri:
        raise HTTPException(status_code=400, detail="OAuth redirect URI does not match GMAIL_REDIRECT_URI.")
    try:
        code_verifier = _consume_oauth_state(db, payload.state)
        if not code_verifier:
            raise ValueError("OAuth state is invalid or has expired.")
        creds = gmail_service.complete_oauth_flow(code=payload.code, redirect_uri=redirect_uri, state=payload.state, code_verifier=code_verifier)
        status = gmail_service.get_authorization_status()

        # Persisted tokens are not enough to call OAuth complete: verify the
        # granted scopes and the account identity through Gmail's live profile.
        if (
            not status.get("is_authorized")
            or not status.get("permissions_sufficient")
            or not status.get("connected_email")
            or status.get("connection_state") != "connected"
        ):
            raise HTTPException(
                status_code=409,
                detail="Gmail authorization was received, but the account profile or required permissions could not be verified.",
            )

        _record_successful_authorization(db, current_user.id)
        from app.services.sync_worker import wake_worker
        wake_worker()

        return {
            "status": "connected",
            "connected_email": status.get("connected_email"),
            "scopes": list(creds.scopes) if creds.scopes else [],
            "permissions_sufficient": status.get("permissions_sufficient"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to complete Gmail OAuth flow (%s)", gmail_service._classify_oauth_error(e))
        raise HTTPException(status_code=400, detail="Gmail authorization failed. Restart the connection flow and check the configured redirect URI and OAuth client.")


@router.post("/sync", response_model=GmailSyncResponse)
@router.post("/sync-emails", response_model=GmailSyncResponse)
def sync_incoming_emails(
    query: str = Query("is:unread", description="Gmail search query"),
    max_results: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """Run one cursor-based worker poll; manual sync uses the same idempotency rules."""
    if not gmail_service.is_authorized():
        raise HTTPException(
            status_code=400,
            detail="Gmail integration is not authorized. Please complete Gmail authorization first.",
        )

    from app.services.sync_worker import run_sync_once
    result = run_sync_once()
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail="Gmail synchronization failed; check the sanitized worker status.")
    ingested = int(result.get("ingested", 0))
    worker_status = result.get("status", "unknown")
    api_status = "success" if worker_status == "ok" else worker_status
    return GmailSyncResponse(
        status=api_status,
        synced_emails_count=ingested,
        tickets_created_or_updated=result.get("tickets_processed", []),
        details=result.get("reason") or f"Processed {ingested} new Gmail message(s); {int(result.get('already_seen', 0))} were already recorded.",
    )
