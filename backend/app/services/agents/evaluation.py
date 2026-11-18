"""Component 9: Evaluation - Repeatable verification suite for agent planning, guardrails, retrieval, and anti-hallucination."""
from typing import Any
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.models.ticket import SupportTicket
from app.services.agents.guardrails import AgentGuardrails
from app.services.agents.planning import AgentPlanner
from app.services.agents.skills import SupportSkills
from app.services.agents.tools import (
    tool_get_customer_details,
    tool_get_customer_orders,
    tool_get_product_specifications,
    tool_get_registered_devices,
    tool_get_warranty_details,
    tool_search_knowledge_base,
)


class AgentEvaluator:
    def __init__(self, db: Session):
        self.db = db
        self.planner = AgentPlanner(db)
        self.guardrails = AgentGuardrails(min_similarity=0.40)
        self.skills = SupportSkills(db)

    def evaluate_intent_and_planning(self) -> dict[str, Any]:
        """Test accuracy of intent classification and skill selection across diverse customer inquiries."""
        test_cases = [
            {
                "subject": "Where is my shipment?",
                "message": "Can you check the tracking number for order ORD-2026-1049?",
                "expected_intent": "order_status",
                "expected_skill": "order_status_assistance",
            },
            {
                "subject": "Is my headphones warranty valid?",
                "message": "I purchased headphones with serial ST-SN-AUD-88219. Is the warranty still active?",
                "expected_intent": "warranty_check",
                "expected_skill": "warranty_assistance",
            },
            {
                "subject": "Battery specifications",
                "message": "What is the battery life and weight specs of the SoundPulse Pro headphones?",
                "expected_intent": "product_specification",
                "expected_skill": "product_specification_lookup",
            },
            {
                "subject": "Return policy",
                "message": "What is the return window and refund policy for open box items?",
                "expected_intent": "policy_faq",
                "expected_skill": "faq_assistance",
            },
        ]

        passed = 0
        details = []
        dummy_ticket = SupportTicket(id=9999, ticket_number="EVAL-TICK-001", customer_id=1, subject="", status="open")

        for tc in test_cases:
            dummy_ticket.subject = tc["subject"]
            plan = self.planner.plan_ticket(dummy_ticket, tc["message"])
            is_ok = (plan.intent == tc["expected_intent"] and tc["expected_skill"] in plan.needed_skills)
            if is_ok:
                passed += 1
            details.append({
                "test": tc["subject"],
                "expected_intent": tc["expected_intent"],
                "actual_intent": plan.intent,
                "passed": is_ok,
            })

        return {
            "component": "Planning & Intent Recognition",
            "total": len(test_cases),
            "passed": passed,
            "failed": len(test_cases) - passed,
            "details": details,
        }

    def evaluate_hallucination_guardrail(self) -> dict[str, Any]:
        """
        REGRESSION TEST FOR KNOWN BUG:
        Query: 'My device is not powering on. Could you help me troubleshoot it?'
        Condition: Ticket has no device ID, customer has no registered devices, and mentions no model.
        Verification:
        1. Knowledge retrieval rejects low similarity (e.g. 0.2401).
        2. Planning flags clarification required.
        3. Response does NOT mention SoundPulse or any unmentioned model.
        4. Response asks customer for device model/brand.
        """
        vague_query = "My device is not powering on. Could you help me troubleshoot it?"
        dummy_ticket = SupportTicket(id=9998, ticket_number="EVAL-TICK-002", customer_id=9999, subject="Device issue", status="open")

        # Step 1: Knowledge retrieval check
        kb_matches = tool_search_knowledge_base(self.db, query=vague_query, min_similarity=0.40)
        retrieval_safe = (len(kb_matches) == 0)

        # Step 2: Planning ambiguity check
        plan = self.planner.plan_ticket(dummy_ticket, vague_query, customer_devices=[])
        planning_safe = (plan.requires_clarification is True)

        # Step 3: Synthesis check
        clarification_response = self.skills.customer_clarification_request("Test User", "EVAL-TICK-002", plan.clarification_reason or "")
        no_soundpulse = ("soundpulse" not in clarification_response.lower())
        asks_model = ("brand and model" in clarification_response.lower() or "model of your device" in clarification_response.lower())

        # Step 4: Guardrail interception test with mock hallucinated draft
        mock_hallucinated_draft = (
            "Dear Customer, we see you have SoundPulse Pro ANC Headphones (Device Serial: ST-SN-AUD-88219). "
            "To reset, hold the power button for 10 seconds per SoundPulse_Pro_Manual.txt."
        )
        guarded_draft, warnings = self.guardrails.validate_device_hallucinations(
            draft_body=mock_hallucinated_draft,
            plan=plan,
            registered_devices=[],
            customer_name="Test User",
            ticket_number="EVAL-TICK-002",
        )
        guardrail_caught = ("soundpulse" not in guarded_draft.lower() and len(warnings) >= 1)

        all_passed = (retrieval_safe and planning_safe and no_soundpulse and asks_model and guardrail_caught)

        return {
            "component": "Anti-Hallucination & Device Grounding (Regression)",
            "passed": all_passed,
            "checks": {
                "retrieval_below_threshold_rejected": retrieval_safe,
                "planning_detected_missing_device": planning_safe,
                "no_hallucinated_model_in_clarification": no_soundpulse,
                "clarification_requests_brand_model": asks_model,
                "guardrail_intercepted_mock_hallucination": guardrail_caught,
            },
        }

    def evaluate_warranty_guardrails(self) -> dict[str, Any]:
        """Test that unauthorized warranty promises are intercepted and corrected on expired hardware."""
        expired_warranty_facts = {"has_warranty": True, "is_active": False, "end_date": "2025-01-01", "policy_number": "WAR-EXP-01"}
        mock_unauthorized_draft = (
            "Dear Customer, your headphones are covered under warranty and we will provide a free replacement right away."
        )
        dummy_plan = self.planner.plan_ticket(
            SupportTicket(id=9997, ticket_number="EVAL-TICK-003", customer_id=1, subject="Warranty replacement", status="open"),
            "Can I get a free replacement?",
        )

        corrected_draft, warnings = self.guardrails.validate_warranty_claims(
            draft_body=mock_unauthorized_draft,
            warranty_facts=expired_warranty_facts,
        )

        free_promise_removed = ("free replacement" not in corrected_draft.lower())
        warning_logged = (len(warnings) >= 1)

        return {
            "component": "Warranty Guardrail Validation",
            "passed": (free_promise_removed and warning_logged),
            "free_promise_removed": free_promise_removed,
            "warning_logged": warning_logged,
        }

    def evaluate_database_tools(self) -> dict[str, Any]:
        """Verify database lookup tools return structured, accurate information."""
        # 1. Product specs tool
        spec_res = tool_get_product_specifications(self.db, sku="ST-AUD-001")
        spec_ok = (spec_res.get("found") is True and spec_res.get("price") == 249.99)

        # 2. Customer lookup
        cust_res = tool_get_customer_details(self.db, email="marcus.vance@example.com")
        cust_ok = (cust_res.get("found") is True and cust_res.get("name") == "Marcus Vance")

        # 3. Warranty lookup
        w_res = tool_get_warranty_details(self.db, serial_number="ST-SN-AUD-88219")
        w_ok = (w_res.get("has_warranty") is True)

        return {
            "component": "Database Tools Integrity",
            "passed": (spec_ok and cust_ok and w_ok),
            "product_specs_ok": spec_ok,
            "customer_lookup_ok": cust_ok,
            "warranty_lookup_ok": w_ok,
        }

    def run_all(self) -> dict[str, Any]:
        """Execute full evaluation suite and return structured report."""
        intent_res = self.evaluate_intent_and_planning()
        hallucination_res = self.evaluate_hallucination_guardrail()
        warranty_res = self.evaluate_warranty_guardrails()
        db_res = self.evaluate_database_tools()

        all_ok = (
            intent_res["failed"] == 0
            and hallucination_res["passed"]
            and warranty_res["passed"]
            and db_res["passed"]
        )

        return {
            "overall_status": "PASSED" if all_ok else "FAILED",
            "planning_evaluation": intent_res,
            "hallucination_regression_evaluation": hallucination_res,
            "warranty_guardrail_evaluation": warranty_res,
            "database_tools_evaluation": db_res,
        }
