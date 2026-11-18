"""
Automated tests for Background Gmail Inbox Monitoring, Auto-Send Decision Engine,
and Safety Guardrails.
"""

import unittest
from unittest.mock import MagicMock, patch
import uuid
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.db.session import SessionLocal
from app.models.agent import AgentExecution, ToolExecution
from app.models.audit import AuditLog, SystemSetting
from app.models.customer import Customer
from app.models.draft import Draft
from app.models.email import EmailMessage
from app.models.ticket import SupportTicket, TicketMessage
from app.services.auto_send import (
    evaluate_auto_send_eligibility,
    is_auto_send_enabled,
    process_auto_send_for_draft,
    set_auto_send_enabled,
)
from app.services.sync_worker import get_worker_status, run_sync_once


class AutoSendAndInboxMonitorTests(unittest.TestCase):
    def setUp(self):
        self.gmail_profile_patch = patch(
            "app.services.integrations.gmail.gmail_service.get_authorization_status",
            return_value={
                "status": "connected", "connection_state": "connected",
                "is_authorized": True, "has_send_permission": True,
                "has_modify_permission": True, "permissions_sufficient": True,
                "connected_email": "support@smarttech.local",
            },
        )
        self.gmail_profile_patch.start()
        self.client = TestClient(app)
        self.db = SessionLocal()
        self.test_prefix = f"autotest_{uuid.uuid4().hex[:8]}"

        # Login as Agent
        agent_res = self.client.post(
            "/api/auth/login",
            json={"email": "agent@smarttech.local", "password": "AgentPass123!"},
        )
        self.assertEqual(agent_res.status_code, 200)
        self.agent_headers = {"Authorization": f"Bearer {agent_res.cookies.get('st_session')}"}

        # Login as Admin
        admin_res = self.client.post(
            "/api/auth/login",
            json={"email": "admin@smarttech.local", "password": "AdminPass123!"},
        )
        self.assertEqual(admin_res.status_code, 200)
        self.admin_headers = {"Authorization": f"Bearer {admin_res.cookies.get('st_session')}"}

        self.created_customer_emails = []
        self.created_ticket_ids = []
        self.created_email_msg_ids = []

    def tearDown(self):
        try:
            for msg_id in self.created_email_msg_ids:
                msg = self.db.scalar(select(EmailMessage).where(EmailMessage.provider_message_id == msg_id))
                if msg:
                    self.db.delete(msg)

            for ticket_id in self.created_ticket_ids:
                ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
                if ticket:
                    drafts = self.db.scalars(select(Draft).where(Draft.ticket_id == ticket_id)).all()
                    for d in drafts:
                        self.db.delete(d)
                    execs = self.db.scalars(select(AgentExecution).where(AgentExecution.ticket_id == ticket_id)).all()
                    for ex in execs:
                        tool_execs = self.db.scalars(select(ToolExecution).where(ToolExecution.agent_execution_id == ex.id)).all()
                        for te in tool_execs:
                            self.db.delete(te)
                        self.db.delete(ex)
                    msgs = self.db.scalars(select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)).all()
                    for m in msgs:
                        self.db.delete(m)
                    self.db.delete(ticket)

            for email in self.created_customer_emails:
                cust = self.db.scalar(select(Customer).where(Customer.email == email.lower()))
                if cust:
                    self.db.delete(cust)

            self.db.commit()
        except Exception:
            self.db.rollback()
        finally:
            self.db.close()
            self.gmail_profile_patch.stop()

    def _track(self, email=None, ticket_id=None, msg_id=None):
        if email:
            self.created_customer_emails.append(email)
        if ticket_id:
            self.created_ticket_ids.append(ticket_id)
        if msg_id:
            self.created_email_msg_ids.append(msg_id)

    def _seed_sample_knowledge(self):
        response = self.client.post(
            "/api/knowledge/seed-sample-docs", headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _create_agent_execution_fixture(
        self, ticket_id: int, confidence_score: float | None = 0.95, escalation: bool = False
    ) -> AgentExecution:
        execution = AgentExecution(
            ticket_id=ticket_id,
            status="completed",
            confidence_score=confidence_score,
            escalation_recommended=escalation,
            user_intent="routine_support",
            issue_category="general",
        )
        self.db.add(execution)
        self.db.commit()
        return execution

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.get_client")
    def test_background_monitor_detects_and_ingests_without_manual_sync(
        self, mock_get_client, mock_auth
    ):
        """
        TEST A Requirement:
        A newly received mock Gmail Inbox message is detected and ingested automatically
        by run_sync_once without calling the manual /api/gmail/sync endpoint.
        """
        test_msg_id = f"msg_{self.test_prefix}_inbox"
        test_thread_id = f"thread_{self.test_prefix}"
        sender_email = f"{self.test_prefix}@example.org"
        self._track(email=sender_email, msg_id=test_msg_id)

        mock_service = MagicMock()
        mock_get_client.return_value = mock_service

        # The worker polls Gmail history, not the unread message list.
        mock_service.users().history().list().execute.return_value = {
            "historyId": "9999",
            "history": [{"messagesAdded": [{"message": {
                "id": test_msg_id,
                "threadId": test_thread_id,
                "labelIds": ["INBOX", "UNREAD"],
            }}]}],
        }
        mock_service.users().getProfile().execute.return_value = {
            "emailAddress": "smarttechsupports@gmail.com",
            "historyId": "9999",
        }
        mock_service.users().messages().get().execute.return_value = {
            "id": test_msg_id,
            "threadId": test_thread_id,
            "labelIds": ["INBOX", "UNREAD"],
            "snippet": "How do I pair my SoundPulse Pro via Bluetooth?",
            "payload": {
                "headers": [
                    {"name": "From", "value": f"Test User <{sender_email}>"},
                    {"name": "To", "value": "support@smarttech.local"},
                    {"name": "Subject", "value": "SoundPulse Pro Bluetooth Setup"},
                    {"name": "Message-ID", "value": f"<{test_msg_id}@mail.example.org>"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {
                            "data": "SG93IGRvIEkgcGFpciBteSBTb3VuZFB1bHNlIFBybyB2aWEgQmx1ZXRvb3RoPw=="
                        },
                    }
                ],
            },
        }

        # Keep the test independent of local embedding-model startup and
        # persistent sync cursors while exercising the real ingestion path.
        with patch(
            "app.services.rag.retrieval.embedding_service.get_embedding",
            return_value=[0.0] * 384,
        ), patch("app.services.sync_worker._get_setting", return_value="9998"), patch(
            "app.services.sync_worker._set_setting"
        ), patch(
            "app.services.auto_send.is_auto_send_enabled", return_value=False
        ):
            summary = run_sync_once()

        self.assertEqual(summary.get("status"), "ok")
        self.assertGreaterEqual(summary.get("ingested", 0), 1)

        # Verify ingested in database
        email_rec = self.db.scalar(
            select(EmailMessage).where(EmailMessage.provider_message_id == test_msg_id)
        )
        self.assertIsNotNone(email_rec)
        self.assertEqual(email_rec.sender, sender_email)
        self.assertIsNotNone(email_rec.ticket_id)
        self._track(ticket_id=email_rec.ticket_id)

        # Verify ticket and draft created
        ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == email_rec.ticket_id))
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.sender_email, sender_email)

        draft = self.db.scalar(select(Draft).where(Draft.ticket_id == ticket.id))
        self.assertIsNotNone(draft)
        self.assertEqual(draft.status, "pending_review")  # auto_send_enabled is False in test environment

        # Verify stats reflect new email
        stats_res = self.client.get("/api/dashboard/stats", headers=self.agent_headers)
        self.assertEqual(stats_res.status_code, 200)
        stats = stats_res.json()
        self.assertGreaterEqual(stats["incoming_emails_count"], 1)

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_routine_faq_auto_send_when_enabled(
        self, mock_auto_send_flag, mock_send_reply, mock_send_perm, mock_auth
    ):
        """
        TEST B Requirement:
        With auto-send enabled inside isolated test configuration, a safe routine FAQ
        is processed end-to-end and invokes the mocked Gmail send service without human approval.
        """
        self._seed_sample_knowledge()
        sender_email = f"{self.test_prefix}_routine@example.org"
        self._track(email=sender_email)

        customer = Customer(email=sender_email, full_name="Routine Tester")
        self.db.add(customer)
        self.db.flush()

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="SoundPulse Pro firmware update procedure",
            status="open",
            priority="low",
            category="general",
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        self.db.add(
            TicketMessage(
                ticket_id=ticket.id,
                sender_type="customer",
                sender_email=sender_email,
                sender_name="Routine Tester",
                message_body="Can you please explain the official steps to update firmware on my SoundPulse Pro?",
            )
        )
        self.db.commit()

        mock_send_reply.return_value = {
            "id": f"sent_mock_{self.test_prefix}",
            "thread_id": f"thread_{self.test_prefix}",
        }

        # Create routine draft
        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender_email,
            subject=f"Re: {ticket.subject}",
            body="To update firmware on your SoundPulse Pro, connect via the SmartTech Audio app and follow on-screen prompts.",
            status="pending_review",
            citations='[{"document": "SoundPulse Pro ANC Headphones User & Troubleshooting Guide", "citation": "Section 4", "relevance": 0.95}]',
        )
        self.db.add(draft)
        self.db.commit()

        # Explicit test fixture for AI confidence validation
        self._create_agent_execution_fixture(ticket.id, confidence_score=0.95)

        # Run auto-send dispatcher
        result = process_auto_send_for_draft(
            db=self.db,
            ticket=ticket,
            draft=draft,
            thread_id=f"thread_{self.test_prefix}",
            message_id_header=f"<msg_{self.test_prefix}@example.org>",
        )

        self.assertTrue(result["auto_sent"])
        self.assertTrue(result["eligible"])
        self.assertEqual(result["draft_status"], "sent")
        self.assertEqual(result["ticket_status"], "resolved")

        # Verify mocked send_reply was invoked with exact recipient
        mock_send_reply.assert_called_once()
        call_kwargs = mock_send_reply.call_args[1]
        self.assertEqual(call_kwargs["to_email"], sender_email)

        # Verify draft marked sent in DB
        self.db.refresh(draft)
        self.assertEqual(draft.status, "sent")
        self.assertIsNotNone(draft.sent_at)

        # Verify ticket marked resolved in DB
        self.db.refresh(ticket)
        self.assertEqual(ticket.status, "resolved")
        self.assertIsNotNone(ticket.resolved_at)

        # Verify audit log recorded
        audit = self.db.scalar(
            select(AuditLog).where(
                AuditLog.action == "auto_send_email",
                AuditLog.entity_id == str(draft.id),
            )
        )
        self.assertIsNotNone(audit)

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_fraudulent_payment_redirection_requires_human_review_even_when_auto_send_enabled(
        self, mock_auto_send_flag, mock_send_reply, mock_send_perm, mock_auth
    ):
        """
        A request to redirect an invoice payment to new bank details remains
        in pending_review even when automatic sending is enabled.
        """
        sender_email = f"{self.test_prefix}_payment_redirect@example.org"
        self._track(email=sender_email)

        customer = Customer(email=sender_email, full_name="Refund Demander")
        self.db.add(customer)
        self.db.flush()

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="Updated bank details for invoice payment",
            status="open",
            priority="high",
            category="billing",
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        self.db.add(
            TicketMessage(
                ticket_id=ticket.id,
                sender_type="customer",
                sender_email=sender_email,
                sender_name="Refund Demander",
                message_body="We changed our bank account. Redirect today's invoice wire to this new account and transfer the funds today.",
            )
        )
        self.db.commit()

        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender_email,
            subject=f"Re: {ticket.subject}",
            body="We received your payment instruction and a support specialist will review it.",
            status="pending_review",
        )
        self.db.add(draft)
        self.db.commit()

        # Evaluate eligibility
        eligible, reason = evaluate_auto_send_eligibility(self.db, ticket, draft)
        self.assertFalse(eligible)
        self.assertIn("bank or payment destination change", reason.lower())

        # Process auto send
        res = process_auto_send_for_draft(self.db, ticket, draft)
        self.assertFalse(res["auto_sent"])
        self.assertFalse(res["eligible"])

        # Must NOT call send_reply
        mock_send_reply.assert_not_called()

        # Draft must stay pending_review
        self.db.refresh(draft)
        self.assertEqual(draft.status, "pending_review")

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=False)
    def test_auto_send_disabled_safeguard(self, mock_flag, mock_send_reply, mock_auth):
        """
        When auto_send_enabled=False (the strict default), no outbound email
        is dispatched under any circumstances.
        """
        sender_email = f"{self.test_prefix}_disabled@example.org"
        self._track(email=sender_email)

        customer = Customer(email=sender_email, full_name="Safe Mode User")
        self.db.add(customer)
        self.db.flush()

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="General FAQ inquiry",
            status="open",
            priority="low",
            category="general",
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender_email,
            subject=f"Re: {ticket.subject}",
            body="Standard setup instructions.",
            status="pending_review",
        )
        self.db.add(draft)
        self.db.commit()

        # Exercise disabled mode without changing the persistent application setting.
        self.assertFalse(mock_flag())

        res = process_auto_send_for_draft(self.db, ticket, draft)
        self.assertFalse(res["auto_sent"])
        mock_send_reply.assert_not_called()

        self.db.refresh(draft)
        self.assertEqual(draft.status, "pending_review")

    def test_identity_mapping_no_display_name_guessing(self):
        """
        Sender-to-customer matching must use exact email address.
        Display names like 'Loan Predictor' cannot match another customer's record.
        """
        email_real = f"{self.test_prefix}_real@example.org"
        email_impostor = f"{self.test_prefix}_impostor@example.org"
        self._track(email=email_real)
        self._track(email=email_impostor)

        # Create target customer with name "Loan Predictor"
        cust_target = Customer(email=email_real, full_name="Loan Predictor")
        self.db.add(cust_target)
        self.db.commit()

        # Create ticket for real customer
        ticket_target = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=cust_target.id,
            subject="Screen issue",
            status="open",
        )
        self.db.add(ticket_target)
        self.db.commit()
        self._track(ticket_id=ticket_target.id)

        # Impostor sends message with From: "Loan Predictor" <email_impostor>
        from app.services.sync_worker import _ingest_message

        parsed_impostor = {
            "id": f"msg_imp_{self.test_prefix}",
            "thread_id": f"thread_imp_{self.test_prefix}",
            "sender_name": "Loan Predictor",  # matching display name!
            "sender_email": email_impostor,   # different email!
            "subject": "Screen issue",
            "body_text": "My screen is flickering.",
        }
        self._track(msg_id=parsed_impostor["id"])

        _ingest_message(self.db, parsed_impostor)

        # Find the ticket created for impostor
        imp_email_rec = self.db.scalar(
            select(EmailMessage).where(EmailMessage.provider_message_id == parsed_impostor["id"])
        )
        self.assertIsNotNone(imp_email_rec)
        self._track(ticket_id=imp_email_rec.ticket_id)

        imp_ticket = self.db.scalar(
            select(SupportTicket).where(SupportTicket.id == imp_email_rec.ticket_id)
        )

        # MUST NOT be attached to cust_target!
        self.assertNotEqual(imp_ticket.customer_id, cust_target.id)
        self.assertEqual(imp_ticket.customer.email, email_impostor)

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_response_with_another_customer_name_is_blocked(
        self, mock_flag, mock_send, mock_perm, mock_auth
    ):
        sender = f"{self.test_prefix}_identity_check@example.org"
        other_email = f"{self.test_prefix}_other_identity@example.org"
        self._track(email=sender, ticket_id=None)
        self._track(email=other_email)
        verified = Customer(email=sender, full_name="Verified Sender")
        other = Customer(email=other_email, full_name="Different Customer")
        self.db.add_all([verified, other])
        self.db.flush()
        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=verified.id,
            subject="VisionBeam setup",
            status="open",
            category="general",
            sender_email=sender,
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)
        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender,
            subject="Re: VisionBeam setup",
            body="Hello Different Customer, here are the setup instructions.",
            status="pending_review",
            citations="[]",
        )
        self.db.add(draft)
        self.db.commit()
        self._create_agent_execution_fixture(ticket.id, confidence_score=0.99)

        eligible, reason = evaluate_auto_send_eligibility(self.db, ticket, draft)
        self.assertFalse(eligible)
        self.assertIn("another customer's name", reason.lower())
        result = process_auto_send_for_draft(self.db, ticket, draft)
        self.assertFalse(result["auto_sent"])
        mock_send.assert_not_called()



    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.get_client")
    def test_deduplication_repeated_polling(self, mock_get_client, mock_auth):
        """
        Calling run_sync_once repeatedly with the same messages must not duplicate
        records or create duplicate tickets.
        """
        test_msg_id = f"msg_dedup_{self.test_prefix}"
        sender_email = f"{self.test_prefix}_dedup@example.org"
        self._track(email=sender_email, msg_id=test_msg_id)

        mock_service = MagicMock()
        mock_get_client.return_value = mock_service
        mock_service.users().history().list().execute.return_value = {
            "historyId": "1001",
            "history": [{"messagesAdded": [{"message": {
                "id": test_msg_id, "threadId": "t1", "labelIds": ["INBOX"]
            }}]}],
        }
        mock_service.users().getProfile().execute.return_value = {
            "emailAddress": "support@smarttech.local",
            "historyId": "1001",
        }
        mock_service.users().messages().get().execute.return_value = {
            "id": test_msg_id,
            "threadId": "t1",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": [
                    {"name": "From", "value": f"Dedup User <{sender_email}>"},
                    {"name": "Subject", "value": "Dedup Test"},
                ],
                "parts": [{"mimeType": "text/plain", "body": {"data": "VGVzdA=="}}],
            },
        }

        # Keep duplicate-polling coverage independent of local embedding-model
        # startup; sync itself and its deduplication path are still exercised.
        with patch(
            "app.services.rag.retrieval.embedding_service.get_embedding",
            return_value=[0.0] * 384,
        ), patch("app.services.sync_worker._get_setting", return_value="1000"), patch(
            "app.services.sync_worker._set_setting"
        ):
            # First sync
            res1 = run_sync_once()
        self.assertEqual(res1.get("ingested"), 1)

        email_rec = self.db.scalar(
            select(EmailMessage).where(EmailMessage.provider_message_id == test_msg_id)
        )
        if email_rec and email_rec.ticket_id:
            self._track(ticket_id=email_rec.ticket_id)

        with patch(
            "app.services.rag.retrieval.embedding_service.get_embedding",
            return_value=[0.0] * 384,
        ), patch("app.services.sync_worker._get_setting", return_value="1000"), patch(
            "app.services.sync_worker._set_setting"
        ):
            # Second sync with same message ID
            res2 = run_sync_once()
        self.assertEqual(res2.get("ingested"), 0)
        self.assertEqual(res2.get("already_seen"), 1)

        # Ensure only 1 record exists in DB
        count = len(
            self.db.scalars(
                select(EmailMessage).where(EmailMessage.provider_message_id == test_msg_id)
            ).all()
        )
        self.assertEqual(count, 1)

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.get_client")
    def test_resilience_temporary_gmail_error_and_recovery(self, mock_get_client, mock_auth):
        """
        Temporary Gmail error sets status='error' without killing the worker,
        and recovers cleanly on the next cycle.
        """
        mock_service = MagicMock()
        mock_get_client.return_value = mock_service
        mock_service.users().getProfile().execute.return_value = {
            "emailAddress": "support@smarttech.local",
            "historyId": "test-history",
        }

        history_list = mock_service.users().history().list
        # The worker now polls Gmail history rather than listing unread mail.
        history_list.side_effect = RuntimeError("Gmail API 503 Unavailable")
        from app.services import sync_worker
        original_get_setting = sync_worker._get_setting
        with patch(
            "app.services.sync_worker._get_setting",
            side_effect=lambda db, key: (
                "1001" if key == "gmail_sync_history_id"
                else original_get_setting(db, key)
            ),
        ):
            res_err = run_sync_once()
            self.assertEqual(res_err.get("status"), "error")
            self.assertIn("503", res_err.get("reason", ""))

            # Check worker status records the error.
            status_info = get_worker_status()
            self.assertIn("RuntimeError", status_info["worker_status"])

            # Second run recovers.
            history_list.side_effect = None
            history_list().execute.return_value = {
                "historyId": "1002", "history": [],
            }
            mock_service.users().getProfile().execute.return_value = {
                "emailAddress": "support@smarttech.local",
                "historyId": "1002",
            }

            res_rec = run_sync_once()
        self.assertEqual(res_rec.get("status"), "ok")
        self.assertEqual(res_rec.get("errors"), 0)

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.integrations.gmail.gmail_service.reconcile_sent_message")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_send_timeout_reconciliation_prevents_duplicate(
        self, mock_flag, mock_reconcile, mock_send, mock_perm, mock_auth
    ):
        """
        If sending encounters a socket timeout but Gmail actually delivered the message,
        reconciliation safely records it as sent to prevent duplicate re-sends.
        """
        self._seed_sample_knowledge()
        sender_email = f"{self.test_prefix}_reconcile@example.org"
        self._track(email=sender_email)

        customer = Customer(email=sender_email, full_name="Reconcile Tester")
        self.db.add(customer)
        self.db.flush()

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="Troubleshooting question",
            status="open",
            category="general",
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender_email,
            subject=f"Re: {ticket.subject}",
            body="Here are the troubleshooting instructions.",
            status="pending_review",
            citations='[{"document": "AeroBook Ultra 14 Display & Hardware Manual", "citation": "Section 4", "relevance": 0.95}]',
        )
        self.db.add(draft)
        self.db.commit()

        # Explicit test fixture for AI confidence validation
        self._create_agent_execution_fixture(ticket.id, confidence_score=0.95)

        # Send raises timeout
        mock_send.side_effect = TimeoutError("Connection reset after handshake")

        # But reconciliation discovers message in Gmail Sent folder
        mock_reconcile.return_value = {
            "id": "gmail_sent_reconciled_999",
            "thread_id": "thread_reconciled",
            "to": sender_email,
            "subject": draft.subject,
        }

        res = process_auto_send_for_draft(self.db, ticket, draft)

        self.assertTrue(res["auto_sent"])
        self.assertTrue(res.get("reconciled"))
        self.assertEqual(res["draft_status"], "sent")
        self.assertEqual(res["gmail_message_id"], "gmail_sent_reconciled_999")

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_unknown_customer_private_data_safeguard(
        self, mock_flag, mock_send, mock_perm, mock_auth
    ):
        """
        TEST D: An unknown sender with no verified customer record asking for order/warranty
        details cannot retrieve another customer's private data or get auto-sent.
        """
        unknown_sender = f"{self.test_prefix}_stranger@example.org"
        self._track(email=unknown_sender)

        # Ticket has no customer_id
        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=None,
            subject="Order status for ORD-9999",
            status="open",
            category="order",
            sender_email=unknown_sender,
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        draft = Draft(
            ticket_id=ticket.id,
            recipient=unknown_sender,
            subject=f"Re: {ticket.subject}",
            body="Please verify your registered account email and order details.",
            status="pending_review",
        )
        self.db.add(draft)
        self.db.commit()

        eligible, reason = evaluate_auto_send_eligibility(self.db, ticket, draft)
        self.assertFalse(eligible)
        self.assertIn("unverified", reason.lower())

        res = process_auto_send_for_draft(self.db, ticket, draft)
        self.assertFalse(res["auto_sent"])
        mock_send.assert_not_called()

        self.db.refresh(draft)
        self.assertEqual(draft.status, "pending_review")
        self.assertIsNotNone(draft.rejection_reason)

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_unsupported_technical_question_no_citations_blocked(
        self, mock_flag, mock_send, mock_perm, mock_auth
    ):
        """
        TEST E: Technical troubleshooting without approved KB citations cannot be
        auto-sent, even if auto-send is enabled.
        """
        sender_email = f"{self.test_prefix}_uncited@example.org"
        self._track(email=sender_email)

        customer = Customer(email=sender_email, full_name="Uncited User")
        self.db.add(customer)
        self.db.flush()

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="Troubleshoot custom overclocking firmware error",
            status="open",
            category="technical",
            sender_email=sender_email,
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        self.db.add(
            TicketMessage(
                ticket_id=ticket.id,
                sender_type="customer",
                sender_email=sender_email,
                sender_name="Uncited User",
                message_body="Can you help me troubleshoot custom overclocking firmware?",
            )
        )
        self.db.commit()

        # Draft has empty citations
        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender_email,
            subject=f"Re: {ticket.subject}",
            body="Generic unverified advice without manual grounding.",
            status="pending_review",
            citations="[]",
        )
        self.db.add(draft)
        self.db.commit()

        # Explicit test fixture with valid confidence to prove missing citations alone blocks auto-send
        self._create_agent_execution_fixture(ticket.id, confidence_score=0.95)

        eligible, reason = evaluate_auto_send_eligibility(self.db, ticket, draft)
        self.assertFalse(eligible)
        self.assertIn("citations", reason.lower())

        res = process_auto_send_for_draft(self.db, ticket, draft)
        self.assertFalse(res["auto_sent"])
        mock_send.assert_not_called()

        self.db.refresh(draft)
        self.assertEqual(draft.status, "pending_review")

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.integrations.gmail.gmail_service.reconcile_sent_message", return_value=None)
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_gmail_send_failure_records_error_and_does_not_mark_sent(
        self, mock_flag, mock_reconcile, mock_send, mock_perm, mock_auth
    ):
        """
        TEST F: When live Gmail send fails and reconciliation confirms message was not sent,
        status must be 'send_failed' and draft is NOT marked sent.
        """
        sender_email = f"{self.test_prefix}_sendfail@example.org"
        self._track(email=sender_email)

        customer = Customer(email=sender_email, full_name="SendFail User")
        self.db.add(customer)
        self.db.flush()

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="General setup inquiry",
            status="open",
            category="general",
            sender_email=sender_email,
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender_email,
            subject=f"Re: {ticket.subject}",
            body="Setup guide instructions.",
            status="pending_review",
            citations='[{"document": "Manual", "citation": "P. 1"}]',
        )
        self.db.add(draft)
        self.db.commit()

        # Explicit test fixture for AI confidence validation
        self._create_agent_execution_fixture(ticket.id, confidence_score=0.95)

        # Send raises an unrecoverable API error
        mock_send.side_effect = RuntimeError("Gmail API 500 Internal Error")

        res = process_auto_send_for_draft(self.db, ticket, draft)

        self.assertFalse(res["auto_sent"])
        self.assertEqual(res["draft_status"], "send_failed")

        self.db.refresh(draft)
        self.assertEqual(draft.status, "send_failed")
        self.assertIsNone(draft.sent_at)

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_multiple_routine_tickets_shared_global_workflow(
        self, mock_flag, mock_send, mock_perm, mock_auth
    ):
        """
        TEST H: Multiple distinct routine support inquiries from different senders
        all proceed through the same shared global workflow without hardcoded conditions.
        """
        mock_send.side_effect = lambda to_email, subject, body_text, thread_id, in_reply_to, references: {
            "id": f"gmail_{uuid.uuid4().hex[:6]}",
            "threadId": thread_id,
        }

        inquiries = [
            ("Alice", f"{self.test_prefix}_multi_a@example.org", "SoundPulse pairing", "How to pair SoundPulse?"),
            ("Bob", f"{self.test_prefix}_multi_b@example.org", "VisionBeam 4K input", "What ports are on VisionBeam?"),
            ("Charlie", f"{self.test_prefix}_multi_c@example.org", "AeroBook setup", "How to setup AeroBook USB-C?"),
        ]

        for name, email, subj, body in inquiries:
            self._track(email=email)
            customer = Customer(email=email, full_name=name)
            self.db.add(customer)
            self.db.flush()

            ticket = SupportTicket(
                ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
                customer_id=customer.id,
                subject=subj,
                status="open",
                category="general",
                sender_email=email,
            )
            self.db.add(ticket)
            self.db.flush()
            self._track(ticket_id=ticket.id)

            draft = Draft(
                ticket_id=ticket.id,
                recipient=email,
                subject=f"Re: {ticket.subject}",
                body=f"Hello {name}, here is the verified information about {subj.split()[0]}.",
                status="pending_review",
                citations='[{"document": "SoundPulse Pro ANC Headphones User & Troubleshooting Guide", "citation": "Page 1", "relevance": 0.95}]',
            )
            self.db.add(draft)
            self.db.commit()

            # Explicit test fixture for AI confidence validation
            self._create_agent_execution_fixture(ticket.id, confidence_score=0.95)

            res = process_auto_send_for_draft(self.db, ticket, draft)
            self.assertTrue(res["auto_sent"])
            self.assertEqual(res["draft_status"], "sent")

            self.db.refresh(draft)
            self.assertEqual(draft.status, "sent")
            self.db.refresh(ticket)
            self.assertEqual(ticket.status, "resolved")

        self.assertEqual(mock_send.call_count, 3)

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_sender_and_thread_correctness(
        self, mock_flag, mock_send, mock_perm, mock_auth
    ):
        """
        TEST I: Reply recipient must strictly match the actual incoming sender email,
        and thread_id/in_reply_to must match the incoming Gmail headers.
        """
        actual_sender = f"{self.test_prefix}_exact_sender@domain.org"
        self._track(email=actual_sender)

        customer = Customer(email=actual_sender, full_name="Exact Sender")
        self.db.add(customer)
        self.db.flush()

        incoming_thread_id = f"thread_{uuid.uuid4().hex[:8]}"
        incoming_msg_header = f"<incoming_{uuid.uuid4().hex[:8]}@domain.org>"

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="Product Inquiry",
            status="open",
            category="general",
            sender_email=actual_sender,
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        draft = Draft(
            ticket_id=ticket.id,
            recipient=actual_sender,
            subject=f"Re: {ticket.subject}",
            body="Here is the product information you requested.",
            status="pending_review",
            citations='[{"document": "Guide", "citation": "P. 2"}]',
        )
        self.db.add(draft)
        self.db.commit()

        # Explicit test fixture for AI confidence validation
        self._create_agent_execution_fixture(ticket.id, confidence_score=0.95)

        mock_send.return_value = {"id": "sent_exact_123", "threadId": incoming_thread_id}

        res = process_auto_send_for_draft(
            db=self.db,
            ticket=ticket,
            draft=draft,
            thread_id=incoming_thread_id,
            message_id_header=incoming_msg_header,
        )

        self.assertTrue(res["auto_sent"])
        mock_send.assert_called_once()
        k = mock_send.call_args[1]
        self.assertEqual(k["to_email"], actual_sender)
        self.assertEqual(k["thread_id"], incoming_thread_id)
        self.assertEqual(k["in_reply_to"], incoming_msg_header)

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_missing_ai_execution_record_blocks_auto_send(
        self, mock_flag, mock_send, mock_perm, mock_auth
    ):
        """
        Verify that in production mode, if a draft has no AI execution record or
        confidence score, automatic sending is strictly rejected and manual review
        is required (no fabricated confidence score is ever substituted).
        """
        sender_email = f"{self.test_prefix}_no_exec@example.org"
        self._track(email=sender_email)

        customer = Customer(email=sender_email, full_name="No Exec User")
        self.db.add(customer)
        self.db.flush()

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="General setup question",
            status="open",
            category="general",
            sender_email=sender_email,
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender_email,
            subject=f"Re: {ticket.subject}",
            body="Here is standard setup guidance.",
            status="pending_review",
            citations='[{"document": "Manual", "citation": "Page 1"}]',
        )
        self.db.add(draft)
        self.db.commit()

        # No AgentExecution fixture is created — proves missing execution record blocks auto-send
        eligible, reason = evaluate_auto_send_eligibility(self.db, ticket, draft)
        self.assertFalse(eligible)
        self.assertIn("confidence score is unavailable", reason)

        res = process_auto_send_for_draft(self.db, ticket, draft)
        self.assertFalse(res["auto_sent"])
        self.assertFalse(res["eligible"])
        self.assertIn("confidence score is unavailable", res["reason"])
        mock_send.assert_not_called()

        self.db.refresh(draft)
        self.assertEqual(draft.status, "pending_review")

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_ai_execution_missing_confidence_score_blocks_auto_send(
        self, mock_flag, mock_send, mock_perm, mock_auth
    ):
        """
        Verify that if an AI execution record exists but its confidence score is None,
        auto-send is strictly blocked with manual review required.
        """
        sender_email = f"{self.test_prefix}_none_conf@example.org"
        self._track(email=sender_email)

        customer = Customer(email=sender_email, full_name="None Conf User")
        self.db.add(customer)
        self.db.flush()

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="General inquiry",
            status="open",
            category="general",
            sender_email=sender_email,
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        # AgentExecution with confidence_score=None
        self._create_agent_execution_fixture(ticket.id, confidence_score=None)

        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender_email,
            subject=f"Re: {ticket.subject}",
            body="Here is standard setup guidance.",
            status="pending_review",
            citations='[{"document": "Manual", "citation": "Page 1"}]',
        )
        self.db.add(draft)
        self.db.commit()

        eligible, reason = evaluate_auto_send_eligibility(self.db, ticket, draft)
        self.assertFalse(eligible)
        self.assertIn("confidence score is unavailable", reason)

        res = process_auto_send_for_draft(self.db, ticket, draft)
        self.assertFalse(res["auto_sent"])
        mock_send.assert_not_called()

    @patch("app.services.integrations.gmail.gmail_service.is_authorized", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.has_send_permission", return_value=True)
    @patch("app.services.integrations.gmail.gmail_service.send_reply")
    @patch("app.services.auto_send.is_auto_send_enabled", return_value=True)
    def test_ai_execution_low_confidence_score_blocks_auto_send(
        self, mock_flag, mock_send, mock_perm, mock_auth
    ):
        """
        Verify that if an AI execution confidence score falls below the threshold,
        auto-send is strictly blocked with threshold error.
        """
        sender_email = f"{self.test_prefix}_low_conf@example.org"
        self._track(email=sender_email)

        customer = Customer(email=sender_email, full_name="Low Conf User")
        self.db.add(customer)
        self.db.flush()

        ticket = SupportTicket(
            ticket_number=f"TICK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer.id,
            subject="Routine query",
            status="open",
            category="general",
            sender_email=sender_email,
        )
        self.db.add(ticket)
        self.db.flush()
        self._track(ticket_id=ticket.id)

        # AgentExecution with confidence_score=0.60 (below threshold 0.70)
        self._create_agent_execution_fixture(ticket.id, confidence_score=0.60)

        draft = Draft(
            ticket_id=ticket.id,
            recipient=sender_email,
            subject=f"Re: {ticket.subject}",
            body="Here is standard setup guidance.",
            status="pending_review",
            citations='[{"document": "Manual", "citation": "Page 1"}]',
        )
        self.db.add(draft)
        self.db.commit()

        eligible, reason = evaluate_auto_send_eligibility(self.db, ticket, draft)
        self.assertFalse(eligible)
        self.assertIn("does not meet the approval threshold", reason)

        res = process_auto_send_for_draft(self.db, ticket, draft)
        self.assertFalse(res["auto_sent"])
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()

