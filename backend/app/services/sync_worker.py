"""
Gmail Sync Background Worker
============================
Implements incremental, idempotent Gmail polling with:
- Persistent history-ID checkpoint in the SystemSetting table
- Bounded exponential backoff on transient failures
- Single-instance lock to prevent concurrent sync jobs
- Configurable polling interval via GMAIL_POLL_INTERVAL_SECONDS env var
- Graceful startup / shutdown tied to FastAPI lifespan
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from app.core.config import settings
from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.audit import SystemSetting
from app.models.customer import Customer
from app.models.draft import Draft
from app.models.email import EmailMessage
from app.models.ticket import SupportTicket, TicketMessage
from app.services.auto_send import is_auto_send_enabled, process_auto_send_for_draft
from app.services.integrations.gmail import gmail_service

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
_SETTING_KEY_HISTORY_ID = "gmail_sync_history_id"
_SETTING_KEY_LAST_SYNC = "gmail_sync_last_success"
_SETTING_KEY_LAST_ERROR = "gmail_sync_last_error"
_SETTING_KEY_WORKER_STATUS = "gmail_worker_status"
_SETTING_KEY_LAST_POLL_METRICS = "gmail_sync_last_poll_metrics"

_LOCK = threading.Lock()
_STOP_EVENT = threading.Event()
_WAKE_EVENT = threading.Event()
_WORKER_THREAD: Optional[threading.Thread] = None
_NEXT_POLL_TIMESTAMP: Optional[float] = None

try:
    POLL_INTERVAL = int(
        os.environ.get(
            "GMAIL_POLL_INTERVAL_SECONDS",
            str(settings.gmail_poll_interval_seconds),
        )
    )
except (TypeError, ValueError):
    POLL_INTERVAL = 10

MAX_BACKOFF_SECONDS = 600  # 10 minutes cap


# -------------------------------------------------------------------
# Persistent checkpoint helpers
# -------------------------------------------------------------------

def _get_setting(db, key: str) -> Optional[str]:
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    return row.value if row else None


def _set_setting(db, key: str, value: str) -> None:
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=key, value=value, description=f"Auto-managed: {key}"))
    db.commit()


# -------------------------------------------------------------------
# Core sync logic (single run)
# -------------------------------------------------------------------

def run_sync_once() -> dict:
    """
    Pull new Gmail messages since the last saved history ID.
    Instruments detailed operational metrics for health and auditing:
    - Poll start and finish time
    - Gmail account being queried
    - Number of messages returned by Gmail
    - Number of previously unseen message IDs
    - Number of messages ingested successfully
    - Number skipped and the specific reason
    - Number failed and the actual error
    - Database persistence result
    """
    if not gmail_service.is_authorized():
        return {"status": "skipped", "reason": gmail_service.last_auth_error or "Authorization required"}

    poll_start_dt = datetime.now(timezone.utc)
    poll_start_iso = poll_start_dt.isoformat()
    t0 = time.time()

    metrics = {
        "poll_start": poll_start_iso,
        "poll_finish": None,
        "duration_ms": 0,
        "account": None,
        "messages_returned": 0,
        "unseen_message_ids": 0,
        "ingested_count": 0,
        "skipped": {
            "already_seen": 0,
            "spam_or_trash": 0,
            "outbound_self": 0,
            "empty_sender": 0,
        },
        "failed": [],
        "db_persistence_result": "pending",
        "status": "running",
    }

    db = SessionLocal()
    summary = {"fetched": 0, "ingested": 0, "already_seen": 0, "errors": 0, "tickets_processed": []}

    try:
        service = gmail_service.get_client()
        if not service:
            return {"status": "error", "reason": "Could not build Gmail client"}

        # Determine connected account identity safely
        own_email = ""
        new_history_id = None
        try:
            prof = service.users().getProfile(userId="me").execute()
            if isinstance(prof, dict):
                email_val = prof.get("emailAddress")
                if isinstance(email_val, str):
                    own_email = email_val.strip().lower()
                hist_val = prof.get("historyId")
                if hist_val is not None:
                    new_history_id = str(hist_val)
        except Exception as profile_error:
            logger.warning("Gmail profile verification failed (%s)", type(profile_error).__name__)
            _set_setting(db, _SETTING_KEY_WORKER_STATUS, "gmail_api_unavailable")
            _set_setting(db, _SETTING_KEY_LAST_ERROR, "Gmail account profile could not be verified")
            return {"status": "error", "reason": "Gmail account profile could not be verified"}
        if not own_email:
            _set_setting(db, _SETTING_KEY_WORKER_STATUS, "gmail_api_unavailable")
            _set_setting(db, _SETTING_KEY_LAST_ERROR, "Gmail account profile returned no account address")
            return {"status": "error", "reason": "Gmail account profile returned no account address"}
        if not new_history_id:
            _set_setting(db, _SETTING_KEY_WORKER_STATUS, "gmail_api_unavailable")
            _set_setting(db, _SETTING_KEY_LAST_ERROR, "Gmail account profile returned no history cursor")
            return {"status": "error", "reason": "Gmail account profile returned no history cursor"}
        metrics["account"] = str(own_email)

        # Gmail history is the source of newness. Searching the unread Inbox
        # replays old mail forever (and after an operational DB cleanup those
        # old provider IDs no longer have a deduplication row).
        history_cursor = _get_setting(db, _SETTING_KEY_HISTORY_ID)
        if not history_cursor:
            _set_setting(db, _SETTING_KEY_HISTORY_ID, str(new_history_id))
            logger.info("Gmail history baseline initialized; no historical messages imported.")
            return {"status": "skipped", "reason": "Gmail history baseline initialized"}
        try:
            messages_meta, observed_history_id = _list_new_message_metas(service, history_cursor)
            if observed_history_id:
                new_history_id = observed_history_id
        except Exception as history_error:
            # Gmail returns 404 when a history cursor has expired. Rebase and
            # skip this poll: importing the current inbox would replay backlog.
            if getattr(history_error, "status_code", None) == 404 or getattr(history_error, "resp", None) is not None and getattr(history_error.resp, "status", None) == 404:
                _set_setting(db, _SETTING_KEY_HISTORY_ID, str(new_history_id))
                logger.warning("Gmail history cursor expired; safely rebased without importing old mail.")
                return {"status": "skipped", "reason": "Gmail history cursor expired; baseline reset"}
            raise

        # Deduplicate metas by ID while preserving order
        seen_ids = set()
        deduped_metas = []
        for m in messages_meta:
            if isinstance(m, dict):
                mid = m.get("id")
                if mid and isinstance(mid, str) and mid not in seen_ids:
                    seen_ids.add(mid)
                    deduped_metas.append(m)
        messages_meta = deduped_metas

        summary["fetched"] = len(messages_meta)
        metrics["messages_returned"] = len(messages_meta)

        # Count previously unseen message IDs
        unseen_count = 0
        for meta in messages_meta:
            existing = db.scalar(
                select(EmailMessage.id).where(EmailMessage.provider_message_id == meta["id"])
            )
            if not existing:
                unseen_count += 1
        metrics["unseen_message_ids"] = unseen_count

        # ---- Process each message ----
        for meta in messages_meta:
            msg_id = meta["id"]
            try:
                existing = db.scalar(
                    select(EmailMessage).where(EmailMessage.provider_message_id == msg_id)
                )
                if existing:
                    if not existing.is_processed and existing.ticket_id:
                        from app.services.langchain_workflow.workflow import LangChainSupportWorkflow
                        from app.services.workflow_audit import record_workflow_execution
                        retry_ticket = db.get(SupportTicket, existing.ticket_id)
                        now = datetime.now(timezone.utc)
                        if existing.next_processing_attempt_at and existing.next_processing_attempt_at > now:
                            summary["already_seen"] += 1
                            metrics["skipped"]["already_seen"] += 1
                            continue
                        existing.processing_status = "processing"
                        existing.processing_attempts = (existing.processing_attempts or 0) + 1
                        existing.next_processing_attempt_at = None
                        db.commit()
                        try:
                            retry_outcome = LangChainSupportWorkflow(db).run_for_ticket(ticket_id=existing.ticket_id, email_id=existing.id)
                            existing.is_processed = True
                            existing.processing_status = "sent" if retry_outcome.status == "resolved" else "drafted"
                            existing.processing_error = None
                            existing.next_processing_attempt_at = None
                            if retry_ticket:
                                record_workflow_execution(db, retry_ticket, outcome=retry_outcome)
                        except Exception as retry_error:
                            from app.services.workflow_audit import safe_workflow_error
                            existing.is_processed = False
                            existing.processing_status = "generation_failed"
                            existing.processing_error = safe_workflow_error(retry_error)
                            delay_seconds = 86400 if "daily free-model request limit" in existing.processing_error else min(1800, 60 * (2 ** min(existing.processing_attempts - 1, 4)))
                            existing.next_processing_attempt_at = now + timedelta(seconds=delay_seconds)
                            if retry_ticket:
                                has_usable_draft = db.scalar(select(Draft.id).where(
                                    Draft.ticket_id == retry_ticket.id,
                                    Draft.status == "pending_review",
                                    Draft.body.is_not(None),
                                    func.length(func.trim(Draft.body)) > 0,
                                ).limit(1))
                                if not has_usable_draft:
                                    retry_ticket.status = "in_progress"
                                record_workflow_execution(db, retry_ticket, error=retry_error)
                        db.commit()
                    summary["already_seen"] += 1
                    metrics["skipped"]["already_seen"] += 1
                    logger.info("Gmail message skipped as already recorded (provider message ID %s).", msg_id)
                    continue

                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )
                label_ids = set(msg.get("labelIds", []))
                if "SPAM" in label_ids or "TRASH" in label_ids:
                    summary["already_seen"] += 1
                    metrics["skipped"]["spam_or_trash"] += 1
                    logger.info("Gmail message skipped because it is in spam or trash (provider message ID %s).", msg_id)
                    continue

                parsed = gmail_service._parse_message(msg)
                if not parsed:
                    metrics["skipped"]["empty_sender"] += 1
                    continue

                # Skip outbound messages sent by support account to prevent loops
                if own_email and (parsed.get("sender_email") or "").lower() == own_email:
                    summary["already_seen"] += 1
                    metrics["skipped"]["outbound_self"] += 1
                    logger.info("Gmail message skipped because it was sent by the support account (provider message ID %s).", msg_id)
                    continue

                ticket_result = _ingest_message(db, parsed)
                if ticket_result:
                    summary["ingested"] += 1
                    summary["tickets_processed"].append(ticket_result)
                    metrics["ingested_count"] += 1

            except Exception as msg_err:
                db.rollback()
                # A concurrent worker may have won the provider-ID insert.
                # The database uniqueness constraint is authoritative.
                if db.scalar(select(EmailMessage.id).where(EmailMessage.provider_message_id == msg_id)):
                    summary["already_seen"] += 1
                    metrics["skipped"]["already_seen"] += 1
                    continue
                logger.error("Sync worker: message processing failed (provider message ID %s, error type %s).", msg_id, type(msg_err).__name__)
                summary["errors"] += 1
                metrics["failed"].append({
                    "error_type": type(msg_err).__name__,
                })

        # Retry persisted generation failures even after Gmail marks a message
        # read. The retry timestamp bounds quota/network retries across restarts.
        from app.services.langchain_workflow.workflow import LangChainSupportWorkflow
        from app.services.workflow_audit import record_workflow_execution, safe_workflow_error

        retry_now = datetime.now(timezone.utc)
        retryable_emails = db.scalars(
            select(EmailMessage)
            .where(
                EmailMessage.is_processed.is_(False),
                EmailMessage.ticket_id.is_not(None),
                EmailMessage.processing_status.in_(("generation_failed", "received")),
                (EmailMessage.next_processing_attempt_at.is_(None)
                 | (EmailMessage.next_processing_attempt_at <= retry_now)),
            )
            .order_by(EmailMessage.received_at.asc())
            .limit(20)
        ).all()
        for retry_email in retryable_emails:
            retry_ticket = db.get(SupportTicket, retry_email.ticket_id)
            if not retry_ticket:
                continue
            retry_email.processing_status = "processing"
            retry_email.processing_attempts = (retry_email.processing_attempts or 0) + 1
            retry_email.next_processing_attempt_at = None
            db.commit()
            try:
                retry_outcome = LangChainSupportWorkflow(db).run_for_ticket(
                    ticket_id=retry_ticket.id,
                    email_id=retry_email.id,
                )
                retry_email.is_processed = True
                retry_email.processing_status = "sent" if retry_outcome.status == "resolved" else "drafted"
                retry_email.processing_error = None
                retry_email.next_processing_attempt_at = None
                record_workflow_execution(db, retry_ticket, outcome=retry_outcome)
                db.commit()
            except Exception as retry_error:
                safe_error = safe_workflow_error(retry_error)
                retry_email.is_processed = False
                retry_email.processing_status = "generation_failed"
                retry_email.processing_error = safe_error
                retry_wait = timedelta(days=1) if "daily free-model request limit" in safe_error else timedelta(minutes=30)
                retry_email.next_processing_attempt_at = datetime.now(timezone.utc) + retry_wait
                has_usable_draft = db.scalar(select(Draft.id).where(
                    Draft.ticket_id == retry_ticket.id,
                    Draft.status == "pending_review",
                    Draft.body.is_not(None),
                    func.length(func.trim(Draft.body)) > 0,
                ).limit(1))
                if not has_usable_draft:
                    retry_ticket.status = "in_progress"
                record_workflow_execution(db, retry_ticket, error=retry_error)
                db.commit()

        # ---- Persist checkpoint ----
        # Keep the old cursor if fetching/ingesting any message failed. A later
        # poll can safely replay those IDs and the unique provider ID constraint
        # will skip ones already committed.
        if new_history_id and not metrics["failed"]:
            _set_setting(db, _SETTING_KEY_HISTORY_ID, str(new_history_id))

        now_iso = datetime.now(timezone.utc).isoformat()
        _set_setting(db, _SETTING_KEY_LAST_SYNC, now_iso)
        _set_setting(db, _SETTING_KEY_WORKER_STATUS, f"ok:{now_iso}")
        _set_setting(db, _SETTING_KEY_LAST_ERROR, "")

        metrics["poll_finish"] = now_iso
        metrics["duration_ms"] = int((time.time() - t0) * 1000)
        metrics["db_persistence_result"] = "committed"
        metrics["status"] = "ok"

        _set_setting(db, _SETTING_KEY_LAST_POLL_METRICS, json.dumps(metrics, default=str))

        logger.info(
            "Gmail poll completed: account=%s returned=%d unseen=%d ingested=%d skipped=%s failed=%d persistence=%s duration=%dms",
            metrics["account"],
            metrics["messages_returned"],
            metrics["unseen_message_ids"],
            metrics["ingested_count"],
            metrics["skipped"],
            len(metrics["failed"]),
            metrics["db_persistence_result"],
            metrics["duration_ms"],
        )

        summary["status"] = (
            "partial_success"
            if any(item.get("ai_status") == "generation_failed" for item in summary["tickets_processed"])
            else "ok"
        )
        summary["metrics"] = metrics
        return summary

    except Exception as exc:
        logger.error("Gmail sync worker failed (error type %s).", type(exc).__name__)
        # Persist a useful, credential-safe operational reason. Never persist the
        # provider's full exception text, which can contain request details.
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if not status_code:
            import re
            status_match = re.search(r"\b(5\d\d)\b", str(exc))
            status_code = status_match.group(1) if status_match else None
        safe_reason = f"Gmail API unavailable ({status_code})" if status_code else type(exc).__name__
        try:
            _set_setting(db, _SETTING_KEY_WORKER_STATUS, f"error:{type(exc).__name__}")
            _set_setting(db, _SETTING_KEY_LAST_ERROR, safe_reason)
            metrics["poll_finish"] = datetime.now(timezone.utc).isoformat()
            metrics["duration_ms"] = int((time.time() - t0) * 1000)
            metrics["db_persistence_result"] = f"error: {type(exc).__name__}"
            metrics["status"] = "error"
            metrics["failed"].append({"error_type": type(exc).__name__})
            _set_setting(db, _SETTING_KEY_LAST_POLL_METRICS, json.dumps(metrics, default=str))
        except Exception as inner_exc:
            logger.error("Failed to record Gmail sync error metrics (%s).", type(inner_exc).__name__)
        summary["status"] = "error"
        summary["reason"] = safe_reason
        return summary
    finally:
        db.close()



def _list_new_message_metas(service, start_history_id: str) -> tuple[list, str | None]:
    """Return Gmail messages added to Inbox since a durable history cursor."""
    try:
        messages, next_cursor, page_token, seen_ids = [], None, None, set()
        while True:
            request = service.users().history().list(
                userId="me", startHistoryId=start_history_id,
                historyTypes=["messageAdded"], maxResults=500,
                pageToken=page_token,
            )
            result = request.execute()
            next_cursor = result.get("historyId", next_cursor)
            for item in result.get("history", []):
                for added in item.get("messagesAdded", []):
                    message = added.get("message", {})
                    labels = set(message.get("labelIds", []))
                    if (message.get("id") and message["id"] not in seen_ids
                            and "INBOX" in labels and not labels.intersection({"SPAM", "TRASH"})):
                        messages.append({"id": message["id"]})
                        seen_ids.add(message["id"])
            page_token = result.get("nextPageToken")
            if not page_token:
                return messages, next_cursor
    except Exception as e:
        logger.error("Gmail history lookup failed (%s).", type(e).__name__)
        raise


def _ingest_message(db, parsed: dict) -> dict | None:
    """Trace one Gmail ingestion and always finish its root observation."""
    from app.services.agents.tracing import start_langfuse_trace

    trace = start_langfuse_trace(
        name="smarttech_gmail_email_workflow",
        source="gmail_sync_worker",
        workflow="email_to_draft",
        tags=["gmail", "support-agent", "rag", "groq", "auto-send"],
        input_summary={
            "source": "gmail",
            "sender_present": bool(parsed.get("sender_email")),
            "subject_present": bool(parsed.get("subject")),
            "body_present": bool(parsed.get("body_text") or parsed.get("snippet")),
            "thread_present": bool(parsed.get("thread_id")),
        },
    )
    started = time.perf_counter()
    try:
        result = _ingest_message_core(db, parsed, langfuse_trace=trace)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if trace:
            trace.finish(
                status="completed",
                output={
                    "email_ingested": result is not None,
                    "draft_created": bool(result and result.get("draft_id")),
                    "workflow_status": (result or {}).get("ai_status", "skipped"),
                },
                duration_ms=duration_ms,
            )
        return result
    except Exception as exc:
        if trace:
            trace.finish(
                status="error",
                output={"error_type": type(exc).__name__},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        raise


def _ingest_message_core(
    db,
    parsed: dict,
    *,
    langfuse_trace=None,
) -> dict | None:
    """
    Ingest a single parsed Gmail message and trigger AI processing.
    Caller must verify the message is not already ingested.
    """
    from app.services.langchain_workflow.workflow import LangChainSupportWorkflow

    provider_msg_id = parsed["id"]
    thread_id = parsed["thread_id"]
    sender_email = (parsed.get("sender_email") or "").strip().lower()
    # The From display name is customer supplied and may disagree with the
    # email identity. Keep it out of customer identity and use a neutral name.
    sender_name = "there"

    subject = parsed.get("subject") or "(No Subject)"
    body_text = parsed.get("body_text") or parsed.get("snippet") or ""

    # Exact-match customer by sender email — never guess or default
    if not sender_email:
        logger.warning("Sync worker: empty sender email; skipping message %s", provider_msg_id)
        db.rollback()
        return None

    customer_span = None
    customer_span_started = time.perf_counter()
    if langfuse_trace:
        try:
            customer_span = langfuse_trace.start_observation(
                name="customer_sender_resolution",
                as_type="tool",
                input={"sender_present": True},
                metadata={"operation": "exact_sender_lookup"},
            )
        except Exception as trace_exc:
            logger.warning("Could not start sender tracing span (%s)", type(trace_exc).__name__)
    customer = db.scalar(
        select(Customer).where(func.lower(Customer.email) == sender_email)
    )
    customer_was_registered = customer is not None
    if not customer:
        customer = Customer(email=sender_email, full_name="Customer")
        db.add(customer)
        db.flush()
    customer_id = customer.id
    if customer_span:
        try:
            customer_span.update(
                output={"customer_found": customer_was_registered, "customer_created": not customer_was_registered},
                metadata={
                    "status": "completed",
                    "duration_ms": int((time.perf_counter() - customer_span_started) * 1000),
                },
            )
            customer_span.end()
        except Exception as trace_exc:
            logger.warning("Could not complete sender tracing span (%s)", type(trace_exc).__name__)


    ticket_span = None
    ticket_span_started = time.perf_counter()
    existing_thread_msg = None
    if langfuse_trace:
        try:
            ticket_span = langfuse_trace.start_observation(
                name="ticket_creation_or_thread_match",
                as_type="tool",
                input={"thread_present": bool(thread_id)},
                metadata={"operation": "resolve_or_create_ticket"},
            )
        except Exception as trace_exc:
            logger.warning("Could not start ticket tracing span (%s)", type(trace_exc).__name__)

    # Find or create ticket per Gmail thread (only reuse if ticket belongs to THIS customer / sender)
    ticket = None
    if thread_id:
        existing_thread_msg = db.scalar(
            select(EmailMessage).where(
                EmailMessage.thread_id == thread_id,
                EmailMessage.ticket_id.isnot(None),
            )
        )
        if existing_thread_msg and existing_thread_msg.ticket_id:
            cand = db.scalar(
                select(SupportTicket).where(SupportTicket.id == existing_thread_msg.ticket_id)
            )
            if cand:
                if cand.sender_email and cand.sender_email.lower() == sender_email:
                    ticket = cand
                elif cand.customer_id and customer_id and cand.customer_id == customer_id:
                    ticket = cand

    ticket_created = False
    if not ticket:
        ticket_number = f"TICK-{uuid.uuid4().hex[:8].upper()}"
        while db.scalar(
            select(SupportTicket).where(SupportTicket.ticket_number == ticket_number)
        ):
            ticket_number = f"TICK-{uuid.uuid4().hex[:8].upper()}"

        ticket = SupportTicket(
            ticket_number=ticket_number,
            customer_id=customer_id,
            subject=subject,
            status="open",
            priority="medium",
            category="general",
            sender_email=sender_email,
        )
        db.add(ticket)
        db.flush()
        ticket_created = True
    elif ticket.status in ("resolved", "closed"):
        ticket.status = "open"
        ticket.resolved_at = None

    if ticket_span:
        try:
            ticket_span.update(
                output={"ticket_created": ticket_created, "ticket_reused": not ticket_created},
                metadata={
                    "status": "completed",
                    "duration_ms": int((time.perf_counter() - ticket_span_started) * 1000),
                },
            )
            ticket_span.end()
        except Exception as trace_exc:
            logger.warning("Could not complete ticket tracing span (%s)", type(trace_exc).__name__)

    storage_span = None
    storage_span_started = time.perf_counter()
    if langfuse_trace:
        try:
            storage_span = langfuse_trace.start_observation(
                name="email_and_message_persistence",
                as_type="tool",
                input={"email_record_present": True},
                metadata={"operation": "persist_inbound_message"},
            )
        except Exception as trace_exc:
            logger.warning("Could not start email storage tracing span (%s)", type(trace_exc).__name__)

    db.add(
        TicketMessage(
            ticket_id=ticket.id,
            sender_type="customer",
            sender_email=sender_email,
            sender_name=sender_name,
            message_body=body_text,
        )
    )

    email_record = EmailMessage(
        provider_message_id=provider_msg_id,
        rfc_message_id=parsed.get("message_id_header"),
        references_header=parsed.get("references"),
        thread_id=thread_id,
        ticket_id=ticket.id,
        sender=sender_email,
        recipient=parsed.get("recipient_email") or "support@smarttech.local",
        subject=subject,
        body_text=body_text,
        body_html=parsed.get("body_html"),
        is_processed=False,
        processing_status="processing",
        processing_attempts=1,
    )
    db.add(email_record)
    db.flush()
    if storage_span:
        try:
            storage_span.update(
                output={"email_record_persisted": True, "message_record_persisted": True},
                metadata={
                    "status": "completed",
                    "duration_ms": int((time.perf_counter() - storage_span_started) * 1000),
                },
            )
            storage_span.end()
        except Exception as trace_exc:
            logger.warning("Could not complete email storage tracing span (%s)", type(trace_exc).__name__)

    workflow = LangChainSupportWorkflow(db, langfuse_trace=langfuse_trace)
    safe_generation_error = None
    draft_status = None
    try:
        outcome = workflow.run_for_ticket(ticket_id=ticket.id, email_id=email_record.id)
        email_record.is_processed = True
        email_record.processing_status = "sent" if outcome.status == "resolved" else "drafted"
        email_record.processing_error = None
        email_record.next_processing_attempt_at = None
        from app.services.workflow_audit import record_workflow_execution
        record_workflow_execution(db, ticket, outcome=outcome)
        if outcome.draft_id:
            draft = db.scalar(select(Draft).where(Draft.id == outcome.draft_id))
            if draft:
                draft.email_id = email_record.id
                if not draft.recipient or draft.recipient == "customer@example.com":
                    draft.recipient = sender_email
                draft_status = draft.status
                db.flush()
    except Exception as ai_err:
        from app.services.workflow_audit import safe_workflow_error
        safe_generation_error = safe_workflow_error(ai_err)
        email_record.is_processed = False
        email_record.processing_status = "generation_failed"
        email_record.processing_error = safe_generation_error
        retry_wait = timedelta(days=1) if "daily free-model request limit" in email_record.processing_error else timedelta(seconds=60)
        email_record.next_processing_attempt_at = datetime.now(timezone.utc) + retry_wait
        has_usable_draft = db.scalar(select(Draft.id).where(
            Draft.ticket_id == ticket.id,
            Draft.status == "pending_review",
            Draft.body.is_not(None),
            func.length(func.trim(Draft.body)) > 0,
        ).limit(1))
        if not has_usable_draft:
            ticket.status = "in_progress"
        from app.services.workflow_audit import record_workflow_execution
        record_workflow_execution(db, ticket, error=ai_err)
        logger.error(
            "Sync worker: response generation failed for ticket %s (%s).",
            ticket.ticket_number,
            type(ai_err).__name__,
        )

    db.commit()
    return {
        "ticket_id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "customer_email": sender_email,
        "subject": subject,
        "draft_id": outcome.draft_id if 'outcome' in locals() else None,
        "draft_status": draft_status,
        "ai_status": "completed" if 'outcome' in locals() else "generation_failed",
        "error": safe_generation_error,
    }



# -------------------------------------------------------------------
# Background thread loop
# -------------------------------------------------------------------

def _worker_loop() -> None:
    """Continuous polling loop running in a daemon thread."""
    global _NEXT_POLL_TIMESTAMP
    logger.info("Gmail sync worker started (interval=%ds)", POLL_INTERVAL)
    consecutive_failures = 0
    backoff = POLL_INTERVAL

    while not _STOP_EVENT.is_set():
        if not _LOCK.acquire(blocking=False):
            time.sleep(5)
            continue

        try:
            result = run_sync_once()
            if result.get("status") == "ok":
                consecutive_failures = 0
                backoff = POLL_INTERVAL
            elif result.get("status") == "skipped":
                # Not authorized yet — poll more slowly
                backoff = max(POLL_INTERVAL, 120)
            else:
                consecutive_failures += 1
                backoff = min(POLL_INTERVAL * (2 ** consecutive_failures), MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Sync failure #%d; next retry in %ds", consecutive_failures, backoff
                )
        except Exception as loop_err:
            consecutive_failures += 1
            backoff = min(POLL_INTERVAL * (2 ** consecutive_failures), MAX_BACKOFF_SECONDS)
            logger.error("Sync worker loop failed (%s); backoff=%ds", type(loop_err).__name__, backoff)
        finally:
            _LOCK.release()

        _NEXT_POLL_TIMESTAMP = time.time() + backoff
        # Wake immediately after OAuth completes rather than waiting through
        # the unauthorized-credentials backoff interval.
        _WAKE_EVENT.wait(backoff)
        _WAKE_EVENT.clear()

    _NEXT_POLL_TIMESTAMP = None
    logger.info("Gmail sync worker stopped.")


# -------------------------------------------------------------------
# Public lifecycle API
# -------------------------------------------------------------------

def start_worker() -> None:
    """Start the background sync thread (idempotent)."""
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        logger.info("Gmail sync worker already running.")
        return
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, daemon=True, name="gmail-sync-worker"
    )
    _WORKER_THREAD.start()
    logger.info("Gmail sync worker thread launched.")


def stop_worker() -> None:
    """Signal the background sync thread to stop gracefully."""
    global _WORKER_THREAD, _NEXT_POLL_TIMESTAMP
    _STOP_EVENT.set()
    _WAKE_EVENT.set()
    _NEXT_POLL_TIMESTAMP = None
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=15)
        logger.info("Gmail sync worker thread joined.")


def wake_worker() -> None:
    """Request an immediate poll after successful Gmail authorization."""
    _WAKE_EVENT.set()


def get_worker_status() -> dict:
    """Return a safe operational status summary."""
    db = SessionLocal()
    try:
        last_sync = _get_setting(db, _SETTING_KEY_LAST_SYNC)
        worker_status = _get_setting(db, _SETTING_KEY_WORKER_STATUS) or "not_started"
        last_error_raw = _get_setting(db, _SETTING_KEY_LAST_ERROR)
        last_error = "Synchronization failed; consult server logs." if last_error_raw else None
        history_id = _get_setting(db, _SETTING_KEY_HISTORY_ID)
        is_running = _WORKER_THREAD is not None and _WORKER_THREAD.is_alive()

        now = time.time()
        next_poll_in = (
            max(0, int(_NEXT_POLL_TIMESTAMP - now))
            if (_NEXT_POLL_TIMESTAMP and is_running)
            else None
        )
        next_poll_at = (
            datetime.fromtimestamp(_NEXT_POLL_TIMESTAMP, tz=timezone.utc).isoformat()
            if (_NEXT_POLL_TIMESTAMP and is_running)
            else None
        )

        auth_status = gmail_service.get_authorization_status()
        is_authorized = bool(auth_status.get("is_authorized") and auth_status.get("permissions_sufficient") and auth_status.get("connected_email"))
        connected_email = None
        if is_authorized:
            connected_email = auth_status.get("connected_email")

        last_metrics_raw = _get_setting(db, _SETTING_KEY_LAST_POLL_METRICS)
        last_poll_metrics = None
        if last_metrics_raw:
            try:
                last_poll_metrics = json.loads(last_metrics_raw)
                if isinstance(last_poll_metrics, dict):
                    last_poll_metrics["failed"] = [
                        {"error_type": item.get("error_type", "ProcessingError")}
                        for item in last_poll_metrics.get("failed", [])
                        if isinstance(item, dict)
                    ]
            except Exception:
                pass

        return {
            "worker_running": is_running,
            "worker_status": worker_status,
            "last_sync": last_sync,
            "last_error": last_error,
            "next_poll_in_seconds": next_poll_in,
            "next_poll_at": next_poll_at,
            "has_checkpoint": history_id is not None,
            "poll_interval_seconds": POLL_INTERVAL,
            "gmail_authorized": is_authorized,
            "connected_email": connected_email,
            "gmail_status": auth_status.get("status"),
            "authorization_error": auth_status.get("authorization_error"),
            "operational_status": (
                "connected_and_monitoring" if is_running and is_authorized
                else (auth_status.get("status") if not is_authorized else "connected_worker_paused")
            ),
            "monitoring_active": bool(is_running and is_authorized),
            "auto_send_enabled": is_auto_send_enabled(db),
            "last_poll_metrics": last_poll_metrics,
        }
    finally:
        db.close()
