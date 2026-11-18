"""Privacy routing and deterministic grounding checks (no live email sending)."""

import uuid
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.customer import Customer
from app.models.draft import Draft
from app.models.device import Device
from app.models.knowledge import KnowledgeDocument
from app.models.order import Order
from app.models.product import Product
from app.models.ticket import SupportTicket
from app.services.email_processing.email_service import CustomerEmailPayload
from app.services.langchain_workflow import workflow as workflow_module
from app.services.langchain_workflow.workflow import LangChainSupportWorkflow
from app.services.response_management.draft_service import DraftOutcome
from app.services.privacy import find_cross_customer_record_request
from app.services.rag.retrieval import RetrievedContext, identify_product_id, ingest_document, search_knowledge_base
from app.services.rag.embeddings import embedding_service
from app.services.response_validation import validate_generated_response


class TestCustomerResponsePolicy:
    def setup_method(self):
        self.db = SessionLocal()
        suffix = uuid.uuid4().hex[:10]
        self.own = Customer(email=f"own-{suffix}@example.test", full_name=f"Owner {suffix}")
        self.other = Customer(email=f"other-{suffix}@example.test", full_name=f"Target {suffix}")
        self.product = Product(sku=f"ST-NEW-{suffix}", name=f"NewModel {suffix}", category="Computing")
        self.db.add_all([self.own, self.other, self.product])
        self.db.flush()
        self.device = Device(
            serial_number=f"SERIAL-{suffix}", customer_id=self.other.id,
            product_id=self.product.id, is_registered=True,
        )
        self.order = Order(order_number=f"ORDER-{suffix}", customer_id=self.other.id)
        self.ticket = SupportTicket(
            ticket_number=f"TICK-{suffix}", customer_id=self.other.id,
            subject="Repair request", status="open",
        )
        self.db.add_all([self.device, self.order, self.ticket])
        self.db.commit()

    def teardown_method(self):
        try:
            for model, ident in (
                (SupportTicket, self.ticket.id), (Order, self.order.id),
                (Device, self.device.id), (Product, self.product.id),
                (Customer, self.other.id), (Customer, self.own.id),
            ):
                obj = self.db.get(model, ident)
                if obj:
                    self.db.delete(obj)
            self.db.commit()
        finally:
            self.db.close()

    def test_another_customer_status_uses_database_relationship(self):
        match = find_cross_customer_record_request(
            self.db, customer_id=self.own.id,
            subject="Repair update", body=f"What is the status of {self.other.full_name}'s laptop repair?",
        )
        assert match and match.record_type == "customer-linked product or service record"

    def test_foreign_serial_and_order_are_private_record_references(self):
        for identifier in (self.device.serial_number, self.order.order_number, self.ticket.ticket_number):
            match = find_cross_customer_record_request(
                self.db, customer_id=self.own.id, subject="Status update", body=f"Please send an update for {identifier}"
            )
            assert match is not None

    def test_another_product_model_general_specs_are_not_private(self):
        match = find_cross_customer_record_request(
            self.db, customer_id=self.own.id,
            subject="Specifications", body=f"What are the specifications of {self.product.name}?",
        )
        assert match is None

    def test_newly_added_catalog_product_is_identified_dynamically(self):
        assert identify_product_id(f"What are the specifications of {self.product.name}?", self.db) == self.product.id

    def test_new_product_manual_is_indexed_and_retrievable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new-product-manual.txt"
            path.write_text(
                "Section 1: Battery\nThe NewModel battery has a 60 Wh capacity. "
                "For overheating during charging, disconnect the power cable and let the device cool before reconnecting it.",
                encoding="utf-8",
            )
            doc = KnowledgeDocument(
                title=f"{self.product.name} Manual", filename=path.name,
                file_path=str(path), file_type="txt", file_size_bytes=path.stat().st_size,
                product_id=self.product.id, is_indexed=False,
            )
            self.db.add(doc)
            self.db.commit()
            vector = [1.0] + [0.0] * (embedding_service.dimension - 1)
            with patch.object(embedding_service, "get_embedding", return_value=vector):
                ingest_document(doc.id, self.db)
                results = search_knowledge_base(
                    f"{self.product.name} battery overheating troubleshooting", self.db,
                    product_id=self.product.id, top_k=4, min_similarity=0.0,
                )
                assert results and "disconnect the power cable" in results[0].content
                context = RetrievedContext(1, [results[0].citation], results[0].content, results)
                supported = "Hello, disconnect the power cable and let the device cool before reconnecting it."
                assert validate_generated_response(
                    body=supported, inquiry="My laptop overheats while charging.",
                    context=context, expected_product_id=self.product.id, db=self.db,
                ) is None
                partial = (
                    "Battery: The manual lists a 60 Wh capacity. "
                    "Warranty: the available product documentation does not specify the warranty period."
                )
                assert validate_generated_response(
                    body=partial, inquiry="What is the battery capacity and warranty period?",
                    context=context, expected_product_id=self.product.id, db=self.db,
                ) is None
            self.db.delete(self.db.get(KnowledgeDocument, doc.id))
            self.db.commit()

    def test_an_unambiguous_own_registered_product_can_scope_generic_device_question(self):
        self.device.customer_id = self.own.id
        self.db.commit()
        assert identify_product_id("My laptop overheats", self.db, customer_id=self.own.id) == self.product.id

    def test_undocumented_warranty_answer_can_be_honest_without_approval_trigger(self):
        context = RetrievedContext(total_chunks=0, citations=[], formatted_text="No relevant documentation found.", results=[])
        answer = "Hello, the available product documentation does not specify the warranty period. Technical-team confirmation is needed."
        assert validate_generated_response(
            body=answer, inquiry="What is the warranty period?", context=context,
            expected_product_id=None, db=self.db,
        ) is None

    def test_unretrieved_phone_email_and_fake_citation_are_rejected(self):
        context = RetrievedContext(total_chunks=0, citations=[], formatted_text="No relevant documentation found.", results=[])
        base = "The available product documentation does not specify that information."
        for answer, phrase in (
            (base + " Call us at +1 555-123-4567.", "phone number"),
            (base + " Email help@smarttech.example.", "email address"),
            (base + " Source: AeroBook manual, page 99.", "citation"),
        ):
            reason = validate_generated_response(
                body=answer, inquiry="Please help", context=context,
                expected_product_id=None, db=self.db,
            )
            assert reason and phrase in reason

    def test_unsupported_repair_steps_are_rejected_without_evidence(self):
        context = RetrievedContext(total_chunks=0, citations=[], formatted_text="No relevant documentation found.", results=[])
        reason = validate_generated_response(
            body="Press and hold the reset button for 20 seconds.",
            inquiry="My laptop has a repair issue", context=context,
            expected_product_id=None, db=self.db,
        )
        assert reason and "without retrieved documentation" in reason

    def test_missing_repair_procedure_gets_honest_nonprocedural_answer(self):
        context = RetrievedContext(total_chunks=0, citations=[], formatted_text="No relevant documentation found.", results=[])
        answer = "The available product documentation does not specify the repair procedure. Technical-team confirmation is needed."
        assert validate_generated_response(
            body=answer, inquiry="How do I repair the laptop?", context=context,
            expected_product_id=None, db=self.db,
        ) is None

    def test_missing_documentation_reply_is_not_forced_to_approval(self):
        ticket = SupportTicket(
            ticket_number=f"TICK-OWN-{uuid.uuid4().hex[:8]}", customer_id=self.own.id,
            subject=f"{self.product.name} warranty details", status="open", sender_email=self.own.email,
        )
        self.db.add(ticket)
        self.db.commit()
        payload = CustomerEmailPayload(
            email_id=0, ticket_id=ticket.id, customer_id=self.own.id,
            sender_email=self.own.email, sender_name=self.other.full_name,
            subject=f"{self.product.name} warranty details",
            body_text=f"What is the warranty period for my {self.product.name}?",
            thread_id="test-no-docs", provider_message_id="test-no-docs",
        )
        checked = SimpleNamespace(
            passed=True, confidence_score=0.95, requires_human_review=False,
            rejection_reasons=[], sentiment=SimpleNamespace(customer_sentiment="neutral"),
        )
        draft = DraftOutcome(1, "sent", True, self.own.email, "Re: Warranty details")
        workflow = LangChainSupportWorkflow(self.db)
        with patch.object(workflow_module, "retrieve_knowledge_context", return_value=RetrievedContext(0, [], "No relevant documentation found.", [])), \
             patch.object(workflow_module, "QualityChecker") as checker, \
             patch.object(workflow.draft_service, "process_delivery_or_draft", return_value=draft) as dispatch:
            checker.return_value.run_all_checks.return_value = checked
            outcome = workflow.execute_workflow_for_payload(payload)
        assert f"We identified your product as {self.product.name}" in outcome.generated_body
        assert "product documentation available for this request" in outcome.generated_body
        assert outcome.generated_body.startswith(f"Hello {self.own.full_name},")
        assert self.other.full_name not in outcome.generated_body
        assert not outcome.requires_human_review
        assert dispatch.call_args.kwargs["requires_human_review"] is False
        assert dispatch.call_args.kwargs["grounding_validated"] is True
        self.db.delete(ticket)
        self.db.commit()

    def test_honest_missing_documentation_response_passes_normal_auto_send_policy(self):
        from app.services.auto_send import evaluate_auto_send_eligibility
        ticket = SupportTicket(
            ticket_number=f"TICK-OWN-{uuid.uuid4().hex[:8]}", customer_id=self.own.id,
            subject="Warranty period", status="open", category="general",
            sender_email=self.own.email,
        )
        self.db.add(ticket)
        self.db.commit()
        draft = Draft(
            ticket_id=ticket.id, recipient=self.own.email, subject="Re: Warranty period",
            body=("Hello, the available product documentation does not specify the information "
                  "you asked about. I cannot confirm it without technical-team confirmation, and I do not want to guess."),
            status="pending_review", citations="[]",
        )
        profile = {
            "is_authorized": True, "has_send_permission": True,
            "permissions_sufficient": True, "gmail_api_available": True,
            "connection_state": "connected", "connected_email": "support@example.test",
        }
        with patch("app.services.auto_send.is_auto_send_enabled", return_value=True), \
             patch("app.services.auto_send.gmail_service.is_authorized", return_value=True), \
             patch("app.services.auto_send.gmail_service.has_send_permission", return_value=True), \
             patch("app.services.auto_send.gmail_service.get_authorization_status", return_value=profile):
            eligible, reason = evaluate_auto_send_eligibility(
                self.db, ticket, draft,
                SimpleNamespace(confidence_score=1.0, citations=[], grounding_validated=True),
            )
        assert eligible, reason
        self.db.delete(ticket)
        self.db.commit()

    def test_cross_customer_record_request_short_circuits_ai_and_saves_safe_draft(self):
        ticket = SupportTicket(
            ticket_number=f"TICK-OWN-{uuid.uuid4().hex[:8]}", customer_id=self.own.id,
            subject="Repair status", status="open", sender_email=self.own.email,
        )
        self.db.add(ticket)
        self.db.commit()
        payload = CustomerEmailPayload(
            email_id=0, ticket_id=ticket.id, customer_id=self.own.id,
            sender_email=self.own.email, sender_name=self.own.full_name,
            subject="Repair status", body_text=f"What is {self.other.full_name}'s repair status?",
            thread_id="test-privacy", provider_message_id="test-privacy",
        )
        draft = DraftOutcome(2, "pending_review", False, self.own.email, "Re: Repair status")
        workflow = LangChainSupportWorkflow(self.db)
        with patch.object(workflow_module, "retrieve_knowledge_context") as retrieve, \
             patch.object(workflow_module, "get_response_generation_chain") as generate, \
             patch.object(workflow.draft_service, "save_pending_draft", return_value=draft) as save:
            outcome = workflow.execute_workflow_for_payload(payload)
        retrieve.assert_not_called()
        generate.assert_not_called()
        assert outcome.requires_human_review
        assert outcome.status == "awaiting_approval"
        assert self.other.full_name not in outcome.generated_body
        assert self.device.serial_number not in outcome.generated_body
        assert save.call_args.kwargs["rejection_reason"]
        self.db.delete(ticket)
        self.db.commit()


def test_grounding_validator_rejects_invented_contact_details():
    db = SessionLocal()
    try:
        context = RetrievedContext(total_chunks=0, citations=[], formatted_text="No relevant documentation found.", results=[])
        reason = validate_generated_response(
            body="The available documentation does not specify that. Contact support at 555-111-2222.",
            inquiry="How long is the warranty?", context=context,
            expected_product_id=None, db=db,
        )
        assert reason == "Response contains an unsupported phone number."
    finally:
        db.close()
