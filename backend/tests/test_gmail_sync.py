"""Tests for Gmail inbox synchronization, customer resolution, ticket deduplication, and AI processing."""
import unittest
import hashlib
import base64
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.db.session import SessionLocal
from app.models.agent import AgentExecution, ToolExecution
from app.models.audit import AuditLog, SystemSetting
from app.models.customer import Customer
from app.models.draft import Draft
from app.models.email import EmailMessage
from app.models.ticket import SupportTicket, TicketMessage


class GmailSyncTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.db = SessionLocal()
        self.test_prefix = f"test_{uuid.uuid4().hex[:8]}"

        # Login as Agent
        agent_res = self.client.post("/api/auth/login", json={
            "email": "agent@smarttech.local",
            "password": "AgentPass123!",
        })
        self.assertEqual(agent_res.status_code, 200)
        self.agent_token = agent_res.cookies.get('st_session')
        self.agent_headers = {"Authorization": f"Bearer {self.agent_token}"}

        # Login as Admin
        admin_res = self.client.post("/api/auth/login", json={
            "email": "admin@smarttech.local",
            "password": "AdminPass123!",
        })
        self.assertEqual(admin_res.status_code, 200)
        self.admin_token = admin_res.cookies.get('st_session')
        self.admin_headers = {"Authorization": f"Bearer {self.admin_token}"}

        self.created_customer_emails = []
        self.created_ticket_ids = []
        self.created_email_msg_ids = []

    @staticmethod
    def _request_with_result(result):
        request = MagicMock()
        request.execute.return_value = result
        return request

    @contextmanager
    def _mock_gmail_history(self, payload_batches):
        """Mock Gmail's profile, history, and message endpoints without OAuth/network."""
        from app.services import sync_worker

        service = MagicMock()
        users = service.users.return_value
        users.getProfile.return_value.execute.return_value = {
            "emailAddress": "support@smarttech.local",
            "historyId": "test-history-new",
        }
        history_requests = []
        raw_messages = {}
        for batch_index, batch in enumerate(payload_batches, start=1):
            additions = []
            for payload in batch:
                message_id = payload["id"]
                thread_id = payload.get("thread_id") or f"thread-{message_id}"
                sender = payload.get("sender_raw") or (
                    f"{payload.get('sender_name', 'Customer')} <{payload['sender_email']}>"
                )
                body = payload.get("body_text", "")
                encoded_body = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
                raw_messages[message_id] = {
                    "id": message_id,
                    "threadId": thread_id,
                    "labelIds": ["INBOX", "UNREAD"],
                    "snippet": payload.get("snippet", body[:120]),
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "From", "value": sender},
                            {"name": "To", "value": payload.get("recipient_email", "support@smarttech.local")},
                            {"name": "Subject", "value": payload.get("subject", "(No Subject)")},
                            {"name": "Message-ID", "value": f"<{message_id}@mail.example.test>"},
                        ],
                        "body": {"data": encoded_body},
                    },
                }
                additions.append({"message": {"id": message_id, "threadId": thread_id, "labelIds": ["INBOX"]}})
            history_requests.append(self._request_with_result({
                "historyId": f"test-history-{batch_index}",
                "history": [{"messagesAdded": additions}] if additions else [],
            }))
        users.history.return_value.list.side_effect = history_requests
        users.messages.return_value.get.side_effect = lambda **kwargs: self._request_with_result(
            raw_messages[kwargs["id"]]
        )

        original_get_setting = sync_worker._get_setting
        with patch("app.services.sync_worker.gmail_service.get_client", return_value=service), \
             patch(
                 "app.services.sync_worker._get_setting",
                 side_effect=lambda db, key: "test-history-start"
                 if key == "gmail_sync_history_id" else original_get_setting(db, key),
             ), \
             patch("app.services.sync_worker._set_setting"):
            yield service

    def _seed_sample_knowledge(self):
        response = self.client.post(
            "/api/knowledge/seed-sample-docs", headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200, response.text)

    def tearDown(self):
        try:
            # Clean up test-created records
            for msg_id in self.created_email_msg_ids:
                msg = self.db.scalar(select(EmailMessage).where(EmailMessage.provider_message_id == msg_id))
                if msg:
                    self.db.delete(msg)
            self.db.commit()

            for ticket_id in self.created_ticket_ids:
                ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
                if ticket:
                    # Clean up draft and executions if not cascaded
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
            self.db.commit()

            for email in self.created_customer_emails:
                cust = self.db.scalar(select(Customer).where(Customer.email == email.lower()))
                if cust:
                    self.db.delete(cust)
            self.db.commit()
        except Exception as e:
            self.db.rollback()
        finally:
            self.db.close()

    def test_gmail_status_endpoint(self):
        """GET /api/gmail/status should safely return status structure."""
        profile = {
            "credentials_file_exists": False, "token_file_exists": False,
            "is_authorized": False, "required_scopes": [],
            "connected_email": None, "status": "not_connected",
            "connection_state": "not_connected",
        }
        with patch("app.api.routers.gmail.gmail_service.get_authorization_status", return_value=profile):
            res = self.client.get("/api/gmail/status", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("credentials_file_exists", data)
        self.assertIn("token_file_exists", data)
        self.assertIn("is_authorized", data)
        self.assertIn("required_scopes", data)

    def test_admin_can_reconnect_gmail_and_callback_requires_verified_profile(self):
        from types import SimpleNamespace

        prior_auth_time = self.db.scalar(select(SystemSetting.value).where(SystemSetting.key == "gmail_last_authorized_at"))
        prior_audit_max = self.db.scalar(select(func.max(AuditLog.id))) or 0
        state = f"mock-oauth-state-{uuid.uuid4().hex}"
        with patch("app.api.routers.gmail.gmail_service.has_credentials_file", return_value=True), \
             patch("app.api.routers.gmail.gmail_service.get_authorization_url", return_value=(
                 "https://accounts.google.com/o/oauth2/mock", state, "mock-pkce-verifier"
             )):
            auth_response = self.client.get(
                "/api/gmail/auth-url",
                headers=self.admin_headers,
            )
        self.assertEqual(auth_response.status_code, 200, auth_response.text)
        self.assertEqual(auth_response.json()["state"], state)

        connected_profile = {
            "status": "connected", "connection_state": "connected",
            "is_authorized": True, "permissions_sufficient": True,
            "has_send_permission": True, "connected_email": "support@example.test",
        }
        try:
            with patch("app.api.routers.gmail.gmail_service.complete_oauth_flow", return_value=SimpleNamespace(scopes=[
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.send",
            ])) as complete, \
                 patch("app.api.routers.gmail.gmail_service.get_authorization_status", return_value=connected_profile), \
                 patch("app.services.sync_worker.wake_worker"):
                callback = self.client.post(
                    "/api/gmail/oauth/callback",
                    headers=self.admin_headers,
                    json={"code": "mock-one-time-code", "state": state},
                )
            self.assertEqual(callback.status_code, 200, callback.text)
            self.assertEqual(callback.json()["status"], "connected")
            self.assertEqual(callback.json()["connected_email"], "support@example.test")
            complete.assert_called_once()
            saved_at = self.db.scalar(select(SystemSetting.value).where(SystemSetting.key == "gmail_last_authorized_at"))
            self.assertIsNotNone(saved_at)
        finally:
            state_key = "gmail_oauth_state:" + hashlib.sha256(state.encode()).hexdigest()
            self.db.query(SystemSetting).filter(SystemSetting.key == state_key).delete(synchronize_session=False)
            auth_time_row = self.db.scalar(select(SystemSetting).where(SystemSetting.key == "gmail_last_authorized_at"))
            if prior_auth_time is None:
                if auth_time_row:
                    self.db.delete(auth_time_row)
            elif auth_time_row:
                auth_time_row.value = prior_auth_time
            else:
                self.db.add(SystemSetting(key="gmail_last_authorized_at", value=prior_auth_time, description="Restored by isolated test"))
            for audit in self.db.scalars(select(AuditLog).where(
                AuditLog.id > prior_audit_max, AuditLog.action == "gmail_authorized"
            )).all():
                self.db.delete(audit)
            self.db.commit()

    def test_sync_requires_authentication(self):
        """POST /api/gmail/sync without headers should be rejected."""
        self.client.cookies.clear()
        res = self.client.post("/api/gmail/sync")
        self.assertEqual(res.status_code, 401)

    def test_sync_fails_when_not_authorized(self):
        """POST /api/gmail/sync returns 400 if Gmail is unauthorized."""
        with patch("app.api.routers.gmail.gmail_service.is_authorized", return_value=False):
            res = self.client.post("/api/gmail/sync", headers=self.agent_headers)
            self.assertEqual(res.status_code, 400)
            self.assertIn("not authorized", res.json()["detail"].lower())

    def test_sync_unknown_sender_creates_customer_and_ticket_and_draft(self):
        """
        Verify synchronization of email from a previously unknown sender:
        - Customer is created using actual Customer model fields (email, full_name).
        - Support ticket is created and linked.
        - TicketMessage and EmailMessage are persisted.
        - AI Orchestrator generates a response draft with status 'pending_review'.
        - Automatic sending remains disabled (manual approval required).
        """
        test_email = f"{self.test_prefix}_unknown@example.com"
        test_name = "Jane Doe NewCustomer"
        test_msg_id = f"gmail_msg_{self.test_prefix}_001"
        test_thread_id = f"gmail_thread_{self.test_prefix}_001"

        self.created_customer_emails.append(test_email)
        self.created_email_msg_ids.append(test_msg_id)

        # Ensure sender does not already exist
        existing = self.db.scalar(select(Customer).where(Customer.email == test_email.lower()))
        self.assertIsNone(existing)

        mock_email_payload = [{
            "id": test_msg_id,
            "thread_id": test_thread_id,
            "sender_raw": f"{test_name} <{test_email}>",
            "sender_name": test_name,
            "sender_email": test_email,
            "recipient_email": "support@smarttech.local",
            "subject": "Help with SoundPulse ANC pairing issue",
            "body_text": "Hello, my SoundPulse headphones are not pairing with my laptop. How can I reset them?",
            "body_html": "<p>Hello, my SoundPulse headphones are not pairing with my laptop. How can I reset them?</p>",
            "snippet": "Hello, my SoundPulse headphones are not pairing...",
        }]

        with patch("app.api.routers.gmail.gmail_service.is_authorized", return_value=True), \
             self._mock_gmail_history([mock_email_payload]):
            res = self.client.post("/api/gmail/sync", headers=self.agent_headers)

        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["synced_emails_count"], 1)
        self.assertEqual(len(data["tickets_created_or_updated"]), 1)

        sync_item = data["tickets_created_or_updated"][0]
        ticket_id = sync_item["ticket_id"]
        self.created_ticket_ids.append(ticket_id)
        self.assertEqual(sync_item["customer_email"], test_email.lower())
        self.assertIsNotNone(sync_item["draft_id"])
        self.assertEqual(sync_item["draft_status"], "pending_review")

        # 1. Verify Customer created with correct model fields
        cust = self.db.scalar(select(Customer).where(Customer.email == test_email.lower()))
        self.assertIsNotNone(cust)
        self.assertEqual(cust.full_name, "Customer")
        self.assertEqual(cust.email, test_email.lower())

        # 2. Verify Ticket
        ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.customer_id, cust.id)
        self.assertEqual(ticket.subject, "Help with SoundPulse ANC pairing issue")
        self.assertEqual(ticket.status, "awaiting_approval")

        # 3. Verify TicketMessage
        msg = self.db.scalar(select(TicketMessage).where(TicketMessage.ticket_id == ticket_id))
        self.assertIsNotNone(msg)
        self.assertEqual(msg.sender_type, "customer")
        self.assertEqual(msg.sender_email, test_email.lower())
        self.assertEqual(msg.sender_name, "there")
        self.assertIn("SoundPulse", msg.message_body)

        # 4. Verify EmailMessage
        email_record = self.db.scalar(select(EmailMessage).where(EmailMessage.provider_message_id == test_msg_id))
        self.assertIsNotNone(email_record)
        self.assertEqual(email_record.thread_id, test_thread_id)
        self.assertEqual(email_record.ticket_id, ticket_id)
        self.assertTrue(email_record.is_processed)

        # 5. Verify Draft generated, linked, and NOT sent automatically
        draft = self.db.scalar(select(Draft).where(Draft.id == sync_item["draft_id"]))
        self.assertIsNotNone(draft)
        self.assertEqual(draft.ticket_id, ticket_id)
        self.assertEqual(draft.email_id, email_record.id)
        self.assertEqual(draft.status, "pending_review")
        self.assertEqual(draft.send_attempts, 0)
        self.assertIsNone(draft.sent_at)
        self.assertIn("SoundPulse", draft.body)

    def test_sync_llm_credit_failure_preserves_email_without_fabricating_draft(self):
        sender = f"{self.test_prefix}_credit@example.com"
        msg_id = f"gmail_msg_{self.test_prefix}_credit"
        self.created_customer_emails.append(sender)
        self.created_email_msg_ids.append(msg_id)
        payload = [{
            "id": msg_id, "thread_id": f"thread_{msg_id}",
            "sender_email": sender, "sender_name": "Credit Test",
            "recipient_email": "support@smarttech.local",
            "subject": "Question about my SoundPulse Pro device",
            "body_text": "I need help troubleshooting my SoundPulse Pro headphones.",
        }]
        with patch("app.api.routers.gmail.gmail_service.is_authorized", return_value=True), \
             self._mock_gmail_history([payload]), \
             patch(
                 "app.services.langchain_workflow.workflow.LangChainSupportWorkflow.run_for_ticket",
                 side_effect=RuntimeError("HTTP 402 credit limit"),
             ):
            response = self.client.post("/api/gmail/sync", headers=self.agent_headers)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "partial_success")
        item = result["tickets_created_or_updated"][0]
        self.created_ticket_ids.append(item["ticket_id"])
        self.assertIsNone(item["draft_id"])
        self.assertEqual(item["ai_status"], "generation_failed")
        self.assertNotIn("402", item["error"])

        email_row = self.db.scalar(select(EmailMessage).where(EmailMessage.provider_message_id == msg_id))
        self.assertIsNotNone(email_row)
        self.assertFalse(email_row.is_processed)
        self.assertIsNone(self.db.scalar(select(Draft).where(Draft.ticket_id == item["ticket_id"])))

    def test_sync_existing_customer_reuses_record(self):
        """
        Verify synchronization of email from an existing customer:
        - Customer record is reused, no duplicate customer created.
        - Ticket is linked to existing customer ID.
        """
        existing_email = "marcus.vance@example.com"
        orig_cust = self.db.scalar(select(Customer).where(Customer.email == existing_email))
        self.assertIsNotNone(orig_cust)
        orig_cust_id = orig_cust.id

        test_msg_id = f"gmail_msg_{self.test_prefix}_marcus_001"
        test_thread_id = f"gmail_thread_{self.test_prefix}_marcus_001"
        self.created_email_msg_ids.append(test_msg_id)

        mock_email_payload = [{
            "id": test_msg_id,
            "thread_id": test_thread_id,
            "sender_raw": f"Marcus Vance <{existing_email}>",
            "sender_name": "Marcus Vance",
            "sender_email": existing_email,
            "recipient_email": "support@smarttech.local",
            "subject": "Question about ANC firmware update steps",
            "body_text": "Could you clarify the firmware upgrade steps for my headphones?",
            "body_html": "<p>Could you clarify the firmware upgrade steps for my headphones?</p>",
            "snippet": "Could you clarify the firmware upgrade steps...",
        }]

        with patch("app.api.routers.gmail.gmail_service.is_authorized", return_value=True), \
             self._mock_gmail_history([mock_email_payload]):
            res = self.client.post("/api/gmail/sync", headers=self.agent_headers)

        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["synced_emails_count"], 1)

        ticket_id = data["tickets_created_or_updated"][0]["ticket_id"]
        self.created_ticket_ids.append(ticket_id)

        # Verify customer wasn't duplicated
        cust_matches = self.db.scalars(select(Customer).where(Customer.email == existing_email)).all()
        self.assertEqual(len(cust_matches), 1)

        # Verify ticket belongs to original customer
        ticket = self.db.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id))
        self.assertEqual(ticket.customer_id, orig_cust_id)

    def test_sync_thread_continuation_reuses_ticket(self):
        """
        Verify that a second email in the same thread:
        - Reuses the existing ticket instead of creating a duplicate ticket.
        - Adds a new TicketMessage and EmailMessage to the existing ticket.
        """
        test_email = f"{self.test_prefix}_threader@example.com"
        test_name = "Thread User"
        msg_id_1 = f"gmail_msg_{self.test_prefix}_t1"
        msg_id_2 = f"gmail_msg_{self.test_prefix}_t2"
        common_thread_id = f"gmail_thread_{self.test_prefix}_common"

        self.created_customer_emails.append(test_email)
        self.created_email_msg_ids.extend([msg_id_1, msg_id_2])

        payload_1 = [{
            "id": msg_id_1,
            "thread_id": common_thread_id,
            "sender_raw": f"{test_name} <{test_email}>",
            "sender_name": test_name,
            "sender_email": test_email,
            "recipient_email": "support@smarttech.local",
            "subject": "Initial Inquiry",
            "body_text": "I have a question about my order.",
            "body_html": "<p>I have a question about my order.</p>",
            "snippet": "I have a question...",
        }]

        payload_2 = [{
            "id": msg_id_2,
            "thread_id": common_thread_id,
            "sender_raw": f"{test_name} <{test_email}>",
            "sender_name": test_name,
            "sender_email": test_email,
            "recipient_email": "support@smarttech.local",
            "subject": "Re: Initial Inquiry",
            "body_text": "Here is additional information on the issue.",
            "body_html": "<p>Here is additional information on the issue.</p>",
            "snippet": "Here is additional...",
        }]

        with patch("app.api.routers.gmail.gmail_service.is_authorized", return_value=True), \
             self._mock_gmail_history([payload_1, payload_2]):
                res1 = self.client.post("/api/gmail/sync", headers=self.agent_headers)
                self.assertEqual(res1.status_code, 200)
                ticket_id_1 = res1.json()["tickets_created_or_updated"][0]["ticket_id"]
                self.created_ticket_ids.append(ticket_id_1)

                res2 = self.client.post("/api/gmail/sync", headers=self.agent_headers)
                self.assertEqual(res2.status_code, 200)
                ticket_id_2 = res2.json()["tickets_created_or_updated"][0]["ticket_id"]

        # Thread continuation must reuse the exact same ticket
        self.assertEqual(ticket_id_1, ticket_id_2)

        # Ticket must now have 2 customer messages
        messages = self.db.scalars(
            select(TicketMessage).where(TicketMessage.ticket_id == ticket_id_1).order_by(TicketMessage.created_at.asc())
        ).all()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].message_body, "I have a question about my order.")
        self.assertEqual(messages[1].message_body, "Here is additional information on the issue.")

    def test_sync_deduplication_skips_already_ingested_messages(self):
        """
        Verify that re-syncing the same provider_message_id is idempotent:
        - Message is skipped.
        - No duplicate ticket or messages are created.
        """
        test_email = f"{self.test_prefix}_dedup@example.com"
        test_name = "Dedup User"
        msg_id = f"gmail_msg_{self.test_prefix}_dedup_01"
        thread_id = f"gmail_thread_{self.test_prefix}_dedup_01"

        self.created_customer_emails.append(test_email)
        self.created_email_msg_ids.append(msg_id)

        payload = [{
            "id": msg_id,
            "thread_id": thread_id,
            "sender_raw": f"{test_name} <{test_email}>",
            "sender_name": test_name,
            "sender_email": test_email,
            "recipient_email": "support@smarttech.local",
            "subject": "Unique Dedup Subject",
            "body_text": "This should only be ingested once.",
            "body_html": "<p>This should only be ingested once.</p>",
            "snippet": "This should only be ingested once.",
        }]

        with patch("app.api.routers.gmail.gmail_service.is_authorized", return_value=True), \
             self._mock_gmail_history([payload, payload]):
            # First sync: ingest
            res1 = self.client.post("/api/gmail/sync", headers=self.agent_headers)
            self.assertEqual(res1.status_code, 200)
            self.assertEqual(res1.json()["synced_emails_count"], 1)
            ticket_id = res1.json()["tickets_created_or_updated"][0]["ticket_id"]
            self.created_ticket_ids.append(ticket_id)

            # Second sync: should be skipped
            res2 = self.client.post("/api/gmail/sync", headers=self.agent_headers)
            self.assertEqual(res2.status_code, 200)
            self.assertEqual(res2.json()["synced_emails_count"], 0)
            self.assertEqual(len(res2.json()["tickets_created_or_updated"]), 0)


if __name__ == "__main__":
    unittest.main()
