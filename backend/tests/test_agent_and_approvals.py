"""Tests for AI Agent workflow, tool execution tracing, and human approval lifecycle."""
import unittest
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.db.session import SessionLocal
from app.models.audit import AuditLog
from app.models.agent import AgentExecution, ToolExecution
from app.models.customer import Customer
from app.models.device import Device
from app.models.draft import Draft
from app.models.ticket import SupportTicket, TicketMessage
from sqlalchemy import select


class AgentAndApprovalTests(unittest.TestCase):
    def setUp(self):
        self.created_draft_ids = []
        self.created_ticket_ids = []
        self.db = SessionLocal()
        self.client = TestClient(app)

        # Login as Manager
        manager_res = self.client.post("/api/auth/login", json={
            "email": "manager@smarttech.local",
            "password": "ManagerPass123!",
        })
        self.manager_headers = {"Authorization": f"Bearer {manager_res.cookies.get('st_session')}"}

        # Login as Agent
        agent_res = self.client.post("/api/auth/login", json={
            "email": "agent@smarttech.local",
            "password": "AgentPass123!",
        })
        self.agent_headers = {"Authorization": f"Bearer {agent_res.cookies.get('st_session')}"}

    def tearDown(self):
        try:
            for draft_id in self.created_draft_ids:
                for audit in self.db.scalars(select(AuditLog).where(
                    AuditLog.entity_type == "draft", AuditLog.entity_id == str(draft_id)
                )).all():
                    self.db.delete(audit)
                draft = self.db.get(Draft, draft_id)
                if draft:
                    self.db.delete(draft)
            for ticket_id in self.created_ticket_ids:
                for execution in self.db.scalars(select(AgentExecution).where(AgentExecution.ticket_id == ticket_id)).all():
                    for tool in self.db.scalars(select(ToolExecution).where(ToolExecution.agent_execution_id == execution.id)).all():
                        self.db.delete(tool)
                    self.db.delete(execution)
                for draft in self.db.scalars(select(Draft).where(Draft.ticket_id == ticket_id)).all():
                    self.db.delete(draft)
                for message in self.db.scalars(select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)).all():
                    self.db.delete(message)
                ticket = self.db.get(SupportTicket, ticket_id)
                if ticket:
                    self.db.delete(ticket)
            self.db.commit()
        finally:
            self.db.close()

    def create_ticket(self, customer_email, subject, message_body, serial_number=None):
        customer = self.db.scalar(select(Customer).where(Customer.email == customer_email))
        if not customer:
            name = "Marcus Vance" if "marcus" in customer_email else ("Sarah Jenkins" if "sarah" in customer_email else "Test Customer")
            customer = Customer(email=customer_email, full_name=name)
            self.db.add(customer)
            self.db.commit()
            self.db.refresh(customer)
        self.assertIsNotNone(customer)
        device = None
        if serial_number:
            device = self.db.scalar(select(Device).where(
                Device.customer_id == customer.id, Device.serial_number == serial_number
            ))
            if not device:
                device = Device(
                    customer_id=customer.id,
                    product_id=1 if "AUD" in serial_number else (2 if "VIS" in serial_number else 3),
                    serial_number=serial_number,
                    is_registered=True,
                )
                self.db.add(device)
                self.db.commit()
                self.db.refresh(device)
            self.assertIsNotNone(device)
        ticket = SupportTicket(
            ticket_number=f"TEST-{uuid.uuid4().hex[:10].upper()}",
            customer_id=customer.id, device_id=device.id if device else None,
            subject=subject, status="open", priority="medium", category="general",
            sender_email=customer.email,
        )
        self.db.add(ticket)
        self.db.flush()
        self.db.add(TicketMessage(
            ticket_id=ticket.id, sender_type="customer", sender_email=customer.email,
            sender_name=customer.full_name, message_body=message_body,
        ))
        self.db.commit()
        self.created_ticket_ids.append(ticket.id)
        return ticket.id

    def test_agent_run_grounded_response_and_tools(self):
        # Provision the evidence this citation assertion depends on; test order
        # must not determine whether the knowledge base is populated.
        seeded = self.client.post(
            "/api/knowledge/seed-sample-docs", headers=self.manager_headers
        )
        self.assertEqual(seeded.status_code, 200, seeded.text)
        ticket_id = self.create_ticket(
            "marcus.vance@example.com", "SoundPulse Pro firmware update",
            "My SoundPulse Pro firmware update is failing. How can I recover it?",
            "ST-SN-AUD-88219",
        )

        run_res = self.client.post("/api/agent/run", headers=self.agent_headers, json={
            "ticket_id": ticket_id,
            "force_regenerate": True,
        })
        self.assertEqual(run_res.status_code, 200)
        data = run_res.json()
        self.assertTrue(data["success"])
        self.assertIn("SoundPulse", data["draft_body"])
        self.assertGreaterEqual(len(data["citations"]), 1)
        self.assertIn("ST-SN-AUD-88219", data["draft_body"])

        # Check tool execution traces
        exec_res = self.client.get(f"/api/agent/executions/{ticket_id}", headers=self.agent_headers)
        self.assertEqual(exec_res.status_code, 200)
        traces = exec_res.json()
        self.assertGreaterEqual(len(traces), 1)
        latest_trace = traces[0]
        tool_names = [t["tool_name"] for t in latest_trace["tool_executions"]]
        self.assertIn("get_customer_details", tool_names)
        self.assertIn("get_registered_devices", tool_names)
        self.assertIn("search_knowledge_base", tool_names)

    def test_react_agent_invokes_customer_scoped_device_tool(self):
        ticket_id = self.create_ticket(
            "marcus.vance@example.com", "Registered device lookup",
            "Please identify my registered SoundPulse device.",
            "ST-SN-AUD-88219",
        )
        ticket = self.db.get(SupportTicket, ticket_id)
        from app.services.agents.langchain_tools import ToolExecutionContext
        from app.services.agents.react_agent import AgentSupportDraft, run_react_agent_for_ticket

        captured = {}

        class MockAgentGraph:
            def invoke(self, _input, config=None):
                device_tool = next(tool for tool in captured["tools"] if tool.name == "get_registered_devices")
                devices = device_tool.invoke({})
                return {"structured_response": AgentSupportDraft(
                    draft_body=f"Your registered device serial is {devices[0]['serial_number']}.",
                    confidence_score=0.9,
                    reasoning_summary="Used the customer-scoped registered-device tool.",
                )}

        def mock_create_agent(*, tools, **_kwargs):
            captured["tools"] = tools
            return MockAgentGraph()

        context = ToolExecutionContext(db=self.db, ticket=ticket)
        with patch("app.services.agents.react_agent.get_groq_langchain_chat"), \
             patch("app.services.agents.react_agent.create_agent", side_effect=mock_create_agent):
            result = run_react_agent_for_ticket(
                context=context, subject=ticket.subject,
                latest_message="Please identify my registered SoundPulse device.",
            )

        self.assertIn("ST-SN-AUD-88219", result.draft_body)
        self.assertEqual(context.called_tools, ["get_registered_devices"])

    def test_agent_escalation_detection_missing_package(self):
        ticket_id = self.create_ticket(
            "sarah.jenkins@example.com", "Lost delivery package",
            "My package was lost and the carrier says it was delivered. Please investigate.",
        )

        run_res = self.client.post("/api/agent/run", headers=self.agent_headers, json={
            "ticket_id": ticket_id,
            "force_regenerate": True,
        })
        self.assertEqual(run_res.status_code, 200)
        data = run_res.json()
        self.assertTrue(data["escalation_recommended"])
        self.assertTrue(any("carrier" in w.lower() or "shipping" in w.lower() or "delivery" in w.lower() for w in data["warnings"]))

    def test_full_approval_lifecycle_and_invalidation(self):
        # Create a new draft for testing lifecycle
        draft_res = self.client.post("/api/drafts", headers=self.agent_headers, json={
            "recipient": "marcus.vance@example.com",
            "subject": "Re: Firmware Issue Lifecycle Test",
            "body": "Hello Marcus, please reset your headphones by holding the ANC button for 12 seconds.",
        })
        self.assertEqual(draft_res.status_code, 201)
        draft_id = draft_res.json()["id"]
        self.created_draft_ids.append(draft_id)

        # 1. Unapproved draft CANNOT be sent
        send_res = self.client.post(f"/api/drafts/{draft_id}/send", headers=self.agent_headers)
        self.assertEqual(send_res.status_code, 400)
        self.assertIn("not approved", send_res.json()["detail"].lower())

        # 2. Agent role cannot approve
        agent_approve = self.client.post(f"/api/drafts/{draft_id}/approve", headers=self.agent_headers)
        self.assertEqual(agent_approve.status_code, 403)

        # 3. Manager approves
        mgr_approve = self.client.post(f"/api/drafts/{draft_id}/approve", headers=self.manager_headers)
        self.assertEqual(mgr_approve.status_code, 200)
        self.assertEqual(mgr_approve.json()["status"], "approved")
        self.assertIsNotNone(mgr_approve.json()["approved_body_hash"])

        # 4. Editing the body MUST invalidate approval!
        edit_res = self.client.patch(f"/api/drafts/{draft_id}", headers=self.agent_headers, json={
            "body": "TAMPERED: We are giving you a free $500 voucher unconditionally.",
        })
        self.assertEqual(edit_res.status_code, 200)
        edited_draft = edit_res.json()
        self.assertEqual(edited_draft["status"], "pending_review")
        self.assertIsNone(edited_draft["approved_body_hash"])

        # 5. Tampered/edited draft cannot be sent
        send_res2 = self.client.post(f"/api/drafts/{draft_id}/send", headers=self.agent_headers)
        self.assertEqual(send_res2.status_code, 409)

        # 6. Re-approve edited draft
        mgr_reapprove = self.client.post(f"/api/drafts/{draft_id}/approve", headers=self.manager_headers)
        self.assertEqual(mgr_reapprove.status_code, 200)

        # 7. Manual sending remains available after explicit approval, even
        # though automatic dispatch is disabled. Gmail is explicitly mocked.
        from app.core.config import settings

        # Manual approval and sending remain available while Auto-Send stays disabled.
        self.assertFalse(settings.auto_send_enabled)

        profile = {"is_authorized": True, "has_send_permission": True, "connected_email": "support@smarttech.local"}
        with patch("app.api.routers.drafts.gmail_service.get_authorization_status", return_value=profile), \
             patch("app.api.routers.drafts.gmail_service.send_reply", return_value={"id": "mock-approved-send", "thread_id": "mock-thread"}) as send_reply:
            send_res3 = self.client.post(f"/api/drafts/{draft_id}/send", headers=self.agent_headers)
        self.assertEqual(send_res3.status_code, 200, send_res3.text)
        send_reply.assert_called_once()

        # 8. Rejected delivery must leave the draft approved.
        drafts_res = self.client.get(
            "/api/drafts",
            headers=self.manager_headers,
        )
        self.assertEqual(drafts_res.status_code, 200)
        drafts = drafts_res.json()
        saved_draft = next(d for d in drafts if d["id"] == draft_id)
        self.assertEqual(saved_draft["status"], "sent")
        self.assertIsNotNone(saved_draft["sent_at"])

    def test_draft_rejection_requires_reason(self):
        draft_res = self.client.post("/api/drafts", headers=self.agent_headers, json={
            "recipient": "david.chen@example.com",
            "subject": "Re: Projector Noise",
            "body": "Here is an ungrounded draft.",
        })
        draft_id = draft_res.json()["id"]
        self.created_draft_ids.append(draft_id)

        # Reject without reason fails validation
        reject_fail = self.client.post(f"/api/drafts/{draft_id}/reject", headers=self.manager_headers, json={"reason": "no"})
        self.assertEqual(reject_fail.status_code, 422)

        # Reject with valid reason succeeds
        reject_ok = self.client.post(f"/api/drafts/{draft_id}/reject", headers=self.manager_headers, json={
            "reason": "Warranty is expired; do not promise free parts without supervisor waiver.",
        })
        self.assertEqual(reject_ok.status_code, 200)
        self.assertEqual(reject_ok.json()["status"], "rejected")
        self.assertIn("Warranty is expired", reject_ok.json()["rejection_reason"])


if __name__ == "__main__":
    unittest.main()
