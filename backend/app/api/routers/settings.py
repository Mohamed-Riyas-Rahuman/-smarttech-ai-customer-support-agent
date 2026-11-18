"""Settings and Audit router."""
import os
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_user, get_db, require_admin, require_manager_or_admin
from app.core.config import settings
from app.db.session import engine
from app.models.audit import AuditLog, SystemSetting
from app.models.user import User
from app.schemas.dashboard import AuditLogOut, SettingOut, SettingUpdate, SystemStatusOut

router = APIRouter(prefix="/api/settings", tags=["Settings & Audit"])


@router.get("/status", response_model=SystemStatusOut)
def get_system_status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Retrieve safe system operational and integration health status without disclosing secrets."""
    db_status = "ok"
    pgvector_status = "unknown"
    rag_status = "operational"
    agent_status = "operational"
    chunk_cnt = 0
    embedded_chunk_cnt = 0
    exec_cnt = 0

    # 1. PostgreSQL & pgvector check
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            ext = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
            pgvector_status = f"v{ext}" if ext else "not_installed"
            
            # Check knowledge chunks count for RAG health
            chunk_cnt = conn.execute(text("SELECT count(*) FROM knowledge_chunks")).scalar() or 0
            embedded_chunk_cnt = conn.execute(text(
                "SELECT count(*) FROM knowledge_chunks WHERE embedding IS NOT NULL"
            )).scalar() or 0
            indexed_doc_cnt = conn.execute(text(
                "SELECT count(*) FROM knowledge_documents WHERE is_indexed IS TRUE"
            )).scalar() or 0
            rag_status = (
                f"operational ({indexed_doc_cnt} documents; {embedded_chunk_cnt}/{chunk_cnt} embeddings)"
                if chunk_cnt > 0 and embedded_chunk_cnt == chunk_cnt and indexed_doc_cnt > 0
                else f"degraded ({indexed_doc_cnt} documents; {embedded_chunk_cnt}/{chunk_cnt} embeddings)"
            )

            # Check agent executions for pipeline health
            exec_cnt = conn.execute(text("SELECT count(*) FROM agent_executions")).scalar() or 0
            agent_status = f"operational ({exec_cnt} traces recorded)"
    except Exception as e:
        db_status = f"error: {type(e).__name__}"
        rag_status = f"degraded: DB connection failed"
        agent_status = f"degraded: DB connection failed"

    # 2. Groq authentication and configured-model availability (safe, no key disclosure)
    from app.integrations.llm.llm_service import groq_model_status
    groq_status_data = groq_model_status()
    groq_status = groq_status_data["status"]
    if db_status != "ok":
        agent_status = "degraded: database unavailable"
    elif groq_status != "available":
        agent_status = f"degraded: Groq {groq_status}"
    elif exec_cnt:
        agent_status = f"operational ({exec_cnt} executions recorded; Groq verified)"
    else:
        agent_status = "operational (Groq verified; awaiting first support email)"

    # 3. Gmail status
    from app.services.integrations.gmail import gmail_service
    gmail_details = gmail_service.get_authorization_status()
    gmail_status = gmail_details.get("status", "authorization_required")
    gmail_authorized_at = db.scalar(select(SystemSetting.value).where(SystemSetting.key == "gmail_last_authorized_at"))

    # 4. Email sending configuration status
    from app.services.auto_send import is_auto_send_enabled
    from app.services.auto_send import get_confidence_threshold, ENV_AUTO_SEND_ENABLED
    auto_send = is_auto_send_enabled(db)
    email_sending_status = "enabled (auto-dispatch)" if auto_send else "disabled (safe human-approval mode)"
    db_auto_send_value = db.scalar(select(SystemSetting.value).where(SystemSetting.key == "auto_send_enabled"))
    environment_auto_send = ENV_AUTO_SEND_ENABLED
    db_auto_send = (str(db_auto_send_value).strip().lower() in ("true", "1", "yes")) if db_auto_send_value is not None else None
    auto_send_source = "database" if db_auto_send_value is not None else "environment"
    auto_send_conflict = db_auto_send is not None and db_auto_send != environment_auto_send

    deps = {
        "database": db_status,
        "pgvector": pgvector_status,
        "groq": groq_status,
        "gmail": gmail_status,
        "rag": rag_status,
        "agent": agent_status,
        "email_sending": email_sending_status,
        "gmail_oauth": "authorized" if gmail_details.get("is_authorized") else gmail_status,
        "gmail_account": "verified" if gmail_details.get("connected_email") else "not verified",
        "gmail_api": (
            "available" if gmail_details.get("gmail_api_available") is True
            else "unavailable" if gmail_details.get("gmail_api_available") is False
            else "not checked"
        ),
        "gmail_send_permission": "available" if gmail_details.get("has_send_permission") else "missing",
        "auto_send": "enabled" if auto_send else "disabled",
        "auto_send_database": "unset" if db_auto_send is None else ("enabled" if db_auto_send else "disabled"),
        "auto_send_environment": "enabled" if environment_auto_send else "disabled",
        "auto_send_effective_source": auto_send_source,
        "auto_send_configuration_conflict": "yes" if auto_send_conflict else "no",
    }

    return SystemStatusOut(
        app_version="1.0.0",
        environment=settings.app_env,
        database_status=db_status,
        pgvector_status=pgvector_status,
        groq_status=groq_status,
        groq_model=settings.groq_model,
        groq_error_message=groq_status_data.get("message") if groq_status != "available" else None,
        embedding_model=settings.embedding_model,
        gmail_status=gmail_status,
        auto_send_enabled=auto_send,
        confidence_threshold=get_confidence_threshold(db),
        dependencies=deps,
        rag_status=rag_status,
        agent_status=agent_status,
        email_sending_status=email_sending_status,
        gmail_connected_email=gmail_details.get("connected_email"),
        gmail_authorization_error=gmail_details.get("authorization_error"),
        gmail_authorization_message=gmail_details.get("authorization_error_message"),
        gmail_permissions_sufficient=gmail_details.get("permissions_sufficient", False),
        gmail_required_scopes=gmail_details.get("required_scopes", []),
        gmail_last_authorized_at=gmail_authorized_at,
        gmail_oauth_authorized=bool(gmail_details.get("is_authorized")),
        gmail_account_verified=bool(gmail_details.get("connected_email")),
        gmail_api_available=gmail_details.get("gmail_api_available"),
        gmail_send_permission=bool(gmail_details.get("has_send_permission")),
        human_approval_policy=(
            "Required for high-risk, ungrounded, low-confidence, escalated, or otherwise flagged responses."
        ),
        gmail_redirect_uri=settings.gmail_redirect_uri,
    )


@router.get("", response_model=list[SettingOut])
def list_settings(db: Session = Depends(get_db), current_user: User = Depends(require_manager_or_admin)):
    """List system settings (Manager or Admin only)."""
    rows = db.scalars(select(SystemSetting).order_by(SystemSetting.key)).all()
    return [
        {
            "key": row.key,
            "value": "[REDACTED]" if row.is_secret else row.value,
            "description": row.description,
            "is_secret": row.is_secret,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


@router.patch("/{key}", response_model=SettingOut)
def update_setting(
    key: str,
    payload: SettingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Update a system configuration setting (Admin only)."""
    if key == "auto_send_enabled":
        from app.services.auto_send import set_auto_send_enabled
        value = payload.value.strip().lower()
        if value not in ("true", "1", "yes", "false", "0", "no"):
            raise HTTPException(status_code=422, detail="Auto-Send value must be true or false.")
        enabled = value in ("true", "1", "yes")
        if enabled:
            from app.services.integrations.gmail import gmail_service
            try:
                gmail_status = gmail_service.get_authorization_status() or {}
            except Exception:
                raise HTTPException(
                    status_code=409,
                    detail="The Gmail account could not be verified. Reconnect Gmail or retry the status check before enabling Auto-Send.",
                )
            if (
                not gmail_status.get("is_authorized")
                or not gmail_status.get("connected_email")
                or not gmail_status.get("has_send_permission")
                or gmail_status.get("gmail_api_available") is not True
                or gmail_status.get("connection_state") != "connected"
            ):
                state = gmail_status.get("connection_state") or "authorization_required"
                messages = {
                    "authorization_expired": "Gmail authorization expired. Reconnect Gmail before enabling Auto-Send.",
                    "refresh_token_revoked_or_expired": "Gmail authorization expired or was revoked. Reconnect Gmail before enabling Auto-Send.",
                    "reauthorization_required": "Reconnect Gmail before enabling Auto-Send.",
                    "permission_missing": "Reconnect Gmail and grant the required Gmail send permission before enabling Auto-Send.",
                    "required_permissions_missing": "Reconnect Gmail and grant the required Gmail send permission before enabling Auto-Send.",
                    "gmail_api_unavailable": "The Gmail API is unavailable. Auto-Send remains disabled until the Gmail account can be verified.",
                    "oauth_network_error": "The Gmail profile could not be checked because the authorization service is unavailable. Try again later.",
                    "profile_verification_failed": "The connected Gmail account could not be verified. Reconnect Gmail before enabling Auto-Send.",
                    "token_missing": "Connect Gmail before enabling Auto-Send.",
                    "not_connected": "Connect Gmail before enabling Auto-Send.",
                }
                raise HTTPException(status_code=409, detail=messages.get(
                    state,
                    "Gmail authorization, account identity, API availability, and send permission must all be verified before enabling Auto-Send.",
                ))
        setting = set_auto_send_enabled(db, enabled, current_user.id)
        return setting

    if key == "ai_confidence_threshold":
        try:
            threshold = float(payload.value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Confidence threshold must be a number from 0 to 1.")
        if not 0.0 <= threshold <= 1.0:
            raise HTTPException(status_code=422, detail="Confidence threshold must be between 0 and 1.")

    setting = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")

    setting.value = str(threshold) if key == "ai_confidence_threshold" else payload.value
    setting.updated_by_user_id = current_user.id
    db.commit()
    db.refresh(setting)

    db.add(AuditLog(
        user_id=current_user.id,
        action="update_setting",
        entity_type="setting",
        entity_id=key,
        details=f"Setting '{key}' updated to '{payload.value}'",
    ))
    db.commit()

    return setting


@router.get("/audit/logs", response_model=list[AuditLogOut])
def list_audit_logs(
    action: str | None = Query(None),
    entity_type: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager_or_admin),
):
    """Query audit logs with optional filters (Manager or Admin only)."""
    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action == action)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)

    logs = db.scalars(query.order_by(AuditLog.created_at.desc()).offset(skip).limit(limit)).all()
    results = []
    for log in logs:
        out = AuditLogOut.model_validate(log)
        if log.user:
            out.user_email = log.user.email
        results.append(out)
    return results
