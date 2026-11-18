"""Automated Regression and Evaluation Suite for Agent Components:
Planning, Skills, Guardrails, Memory, and Anti-Hallucination verification.
"""
import unittest
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.db.session import SessionLocal
from app.models.agent import AgentExecution, ToolExecution
from app.models.customer import Customer
from app.models.device import Device
from app.models.draft import Draft
from app.models.product import Product
from app.models.ticket import SupportTicket, TicketMessage
from app.services.agents.evaluation import AgentEvaluator
from app.services.agents.orchestrator import AgentOrchestrator


class AgentEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.db = SessionLocal()
        self.evaluator = AgentEvaluator(self.db)
        self.orchestrator = AgentOrchestrator(self.db)
        self.test_prefix = f"eval_{uuid.uuid4().hex[:8]}"

        # Login as Agent
        agent_res = self.client.post("/api/auth/login", json={
            "email": "agent@smarttech.local",
            "password": "AgentPass123!",
        })
        self.agent_token = agent_res.cookies.get('st_session')
        self.agent_headers = {"Authorization": f"Bearer {self.agent_token}"}

        self.created_ticket_ids = []
        self.created_customer_ids = []
        self.created_device_ids = []

    def tearDown(self):
        try:
            for did in self.created_device_ids:
                d = self.db.scalar(select(Device).where(Device.id == did))
                if d:
                    self.db.delete(d)
            self.db.commit()

            for tid in self.created_ticket_ids:
                drafts = self.db.scalars(select(Draft).where(Draft.ticket_id == tid)).all()
                for d in drafts:
                    self.db.delete(d)
                execs = self.db.scalars(select(AgentExecution).where(AgentExecution.ticket_id == tid)).all()
                for ex in execs:
                    for te in self.db.scalars(select(ToolExecution).where(ToolExecution.agent_execution_id == ex.id)).all():
                        self.db.delete(te)
                    self.db.delete(ex)
                for m in self.db.scalars(select(TicketMessage).where(TicketMessage.ticket_id == tid)).all():
                    self.db.delete(m)
                t = self.db.scalar(select(SupportTicket).where(SupportTicket.id == tid))
                if t:
                    self.db.delete(t)
            self.db.commit()

            for cid in self.created_customer_ids:
                c = self.db.scalar(select(Customer).where(Customer.id == cid))
                if c:
                    self.db.delete(c)
            self.db.commit()
        except Exception:
            self.db.rollback()
        finally:
            self.db.close()

    def test_run_evaluator_suite(self):
        """Execute the complete repeatable evaluation suite and verify all sub-components pass."""
        report = self.evaluator.run_all()
        self.assertEqual(report["overall_status"], "PASSED", f"Evaluator failed: {report}")
        self.assertEqual(report["planning_evaluation"]["failed"], 0)
        self.assertTrue(report["hallucination_regression_evaluation"]["passed"])
        self.assertTrue(report["warranty_guardrail_evaluation"]["passed"])
        self.assertTrue(report["database_tools_evaluation"]["passed"])

    def test_hallucination_regression_unknown_device_asks_clarification(self):
        """
        REGRESSION TEST FOR KNOWN BUG:
        Customer asks: 'My device is not powering on. Could you help me troubleshoot it?'
        Ticket has no device ID, customer has no registered devices.
        Verification:
        - Must NOT invent 'SoundPulse Pro ANC Headphones' or any other model.
        - Must NOT cite SoundPulse_Pro_Manual.txt with 0.2401 or any low relevance score.
        - Must ask customer for the brand and model of their device.
        - Must keep automatic sending disabled (draft in pending_review).
        """
        # Create a new test customer with 0 registered devices
        cust = Customer(
            email=f"{self.test_prefix}_nodevice@example.com",
            full_name="Alex Mercer",
        )
        self.db.add(cust)
        self.db.flush()
        self.created_customer_ids.append(cust.id)

        ticket = SupportTicket(
            ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=cust.id,
            subject="Device issue",
            status="open",
            priority="medium",
            category="general",
        )
        self.db.add(ticket)
        self.db.flush()
        self.created_ticket_ids.append(ticket.id)

        msg = TicketMessage(
            ticket_id=ticket.id,
            sender_type="customer",
            sender_email=cust.email,
            sender_name=cust.full_name,
            message_body="My device is not powering on. Could you help me troubleshoot it?",
        )
        self.db.add(msg)
        self.db.commit()

        # Run orchestrator
        res = self.orchestrator.run_for_ticket(ticket_id=ticket.id, force_regenerate=True)

        self.assertTrue(res.success)
        lower_draft = res.draft_body.lower()

        # 1. Zero hallucinations of specific product models
        self.assertNotIn("soundpulse", lower_draft)
        self.assertNotIn("aerobook", lower_draft)
        self.assertNotIn("visionbeam", lower_draft)
        self.assertNotIn("omnihub", lower_draft)

        # 2. No claims of registered hardware
        self.assertNotIn("your registered", lower_draft)

        # 3. Asks customer for brand and model
        self.assertTrue(
            "brand and model" in lower_draft or "model of your device" in lower_draft or "specific model" in lower_draft
        )

        # 4. No low-relevance citations attached
        self.assertEqual(len(res.citations), 0)

        # 5. Draft stored with pending_review
        draft = self.db.scalar(select(Draft).where(Draft.id == res.draft_id))
        self.assertIsNotNone(draft)
        self.assertEqual(draft.status, "pending_review")
        self.assertEqual(draft.send_attempts, 0)

    def test_warranty_guardrail_prevents_unauthorized_free_claims(self):
        """
        Verify that when a device warranty is expired, the agent does NOT promise free repair/replacement.
        David Chen (Customer 3) has VisionBeam Projector with expired warranty.
        """
        david = self.db.scalar(select(Customer).where(Customer.email == "david.chen@example.com"))
        self.assertIsNotNone(david)

        ticket = SupportTicket(
            ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=david.id,
            subject="VisionBeam Projector bulb failure - warranty claim",
            status="open",
            priority="medium",
            category="warranty",
        )
        self.db.add(ticket)
        self.db.flush()
        self.created_ticket_ids.append(ticket.id)

        msg = TicketMessage(
            ticket_id=ticket.id,
            sender_type="customer",
            sender_email=david.email,
            sender_name=david.full_name,
            message_body="The bulb stopped working. Can I get a free warranty replacement unit shipped immediately?",
        )
        self.db.add(msg)
        self.db.commit()

        res = self.orchestrator.run_for_ticket(ticket_id=ticket.id, force_regenerate=True)
        self.assertTrue(res.success)
        lower_body = res.draft_body.lower()

        # Must not promise free replacement
        self.assertNotIn("we will provide a free replacement", lower_body)
        self.assertNotIn("we can offer a free replacement", lower_body)
        self.assertTrue("cannot offer a free replacement" in lower_body or "expired" in lower_body or "not covered" in lower_body or "out-of-warranty" in lower_body)

    def test_product_specifications_tool(self):
        """Verify product specification lookup tool retrieves accurate catalog details."""
        from app.services.agents.tools import tool_get_product_specifications

        headphones_spec = tool_get_product_specifications(self.db, sku="ST-AUD-001")
        self.assertTrue(headphones_spec["found"])
        self.assertEqual(headphones_spec["price"], 249.99)
        self.assertEqual(headphones_spec["warranty_months"], 24)

        laptop_spec = tool_get_product_specifications(self.db, product_name="AeroBook Ultra")
        self.assertTrue(laptop_spec["found"])
        self.assertEqual(laptop_spec["price"], 1199.00)

    def test_hallucination_regression_multiple_registered_devices_asks_clarification(self):
        """
        REGRESSION TEST FOR MULTIPLE REGISTERED DEVICES:
        Customer owns 2 registered devices (Headphones and Laptop).
        Customer asks: 'My device is having connection and screen issues, how can I fix it?'
        Verification:
        - Must NOT arbitrarily invent or assume 'SoundPulse' or 'AeroBook'.
        - Must recognize ambiguity across registered devices.
        - Must ask customer for clarification on which device requires assistance.
        """
        # Create customer with 2 devices
        cust = Customer(
            email=f"{self.test_prefix}_multidev@example.com",
            full_name="Morgan Chase",
        )
        self.db.add(cust)
        self.db.flush()
        self.created_customer_ids.append(cust.id)

        hp_prod = self.db.scalar(select(Product).where(Product.sku == "ST-AUD-001"))
        lap_prod = self.db.scalar(select(Product).where(Product.sku == "ST-CMP-003"))

        dev1 = Device(
            customer_id=cust.id,
            product_id=hp_prod.id,
            serial_number=f"ST-SN-HP-{uuid.uuid4().hex[:6].upper()}",
            nickname="Work Headphones",
        )
        dev2 = Device(
            customer_id=cust.id,
            product_id=lap_prod.id,
            serial_number=f"ST-SN-LAP-{uuid.uuid4().hex[:6].upper()}",
            nickname="Primary Laptop",
        )
        self.db.add_all([dev1, dev2])
        self.db.flush()
        self.created_device_ids.extend([dev1.id, dev2.id])

        ticket = SupportTicket(
            ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=cust.id,
            subject="Hardware connection and screen problem",
            status="open",
            priority="medium",
            category="general",
        )
        self.db.add(ticket)
        self.db.flush()
        self.created_ticket_ids.append(ticket.id)

        msg = TicketMessage(
            ticket_id=ticket.id,
            sender_type="customer",
            sender_email=cust.email,
            sender_name=cust.full_name,
            message_body="My device is having connection and screen issues, how can I fix it?",
        )
        self.db.add(msg)
        self.db.commit()

        res = self.orchestrator.run_for_ticket(ticket_id=ticket.id, force_regenerate=True)
        self.assertTrue(res.success)
        lower_draft = res.draft_body.lower()

        # Must ask clarification for model/device
        self.assertTrue(
            "which" in lower_draft or "brand and model" in lower_draft or "model of your device" in lower_draft or "specific" in lower_draft
        )

    def test_grounded_response_single_registered_device_infers_model_and_serial(self):
        """
        VERIFICATION TEST FOR SINGLE REGISTERED DEVICE:
        Customer owns exactly 1 registered device (e.g. SoundPulse Headphones).
        Customer asks: 'My device won't turn on, please help.'
        Verification:
        - Accurately infers customer's single registered hardware.
        - References the serial number of that device in the grounded response.
        - Does NOT invent other devices (e.g. VisionBeam or AeroBook).
        """
        cust = Customer(
            email=f"{self.test_prefix}_singledir@example.com",
            full_name="Taylor Swiftly",
        )
        self.db.add(cust)
        self.db.flush()
        self.created_customer_ids.append(cust.id)

        hp_prod = self.db.scalar(select(Product).where(Product.sku == "ST-AUD-001"))
        sn = f"ST-SN-HP-{uuid.uuid4().hex[:6].upper()}"
        dev = Device(
            customer_id=cust.id,
            product_id=hp_prod.id,
            serial_number=sn,
            nickname="Taylor's ANC",
        )
        self.db.add(dev)
        self.db.flush()
        self.created_device_ids.append(dev.id)

        ticket = SupportTicket(
            ticket_number=f"TCK-{uuid.uuid4().hex[:8].upper()}",
            customer_id=cust.id,
            subject="Device power failure",
            status="open",
            priority="medium",
            category="technical",
        )
        self.db.add(ticket)
        self.db.flush()
        self.created_ticket_ids.append(ticket.id)

        msg = TicketMessage(
            ticket_id=ticket.id,
            sender_type="customer",
            sender_email=cust.email,
            sender_name=cust.full_name,
            message_body="My device won't turn on, please help.",
        )
        self.db.add(msg)
        self.db.commit()

        res = self.orchestrator.run_for_ticket(ticket_id=ticket.id, force_regenerate=True)
        self.assertTrue(res.success)
        lower_draft = res.draft_body.lower()

        # Confirms reference to customer's single registered serial number
        self.assertIn(sn.lower(), lower_draft)
        # Does NOT hallucinate other products
        self.assertNotIn("visionbeam", lower_draft)
        self.assertNotIn("aerobook", lower_draft)


if __name__ == "__main__":
    unittest.main()
