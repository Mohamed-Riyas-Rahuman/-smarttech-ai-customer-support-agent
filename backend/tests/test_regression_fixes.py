"""Regression checks for grounded generation and reply persistence safeguards."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.langchain_workflow.chains import _parse_json_or_fallback
from app.services.langchain_workflow.prompts import SYSTEM_SUPPORT_INSTRUCTIONS
from app.services.rag.chunking import extract_text_from_file, chunk_text
from app.services.rag.retrieval import _make_search_result, identify_product_id, retrieve_knowledge_context
from app.services.response_management.draft_service import _validate_response_body
from app.services.email_risk import assess_inbound_email_risk
from app.services.workflow_audit import safe_workflow_error
from app.schemas.draft import DraftCreate, DraftUpdate


class GroundingRegressionTests(unittest.TestCase):
    def test_uploaded_pdf_text_contains_battery_steps_and_warranty_boundary(self):
        pdf = Path(__file__).resolve().parents[2] / "data" / "uploads" / "AeroBook_Ultra_14_Test_Manual.pdf"
        text = extract_text_from_file(str(pdf))
        self.assertIn("Battery Draining Rapidly", text)
        self.assertIn("Battery Saver Mode", text)
        self.assertIn("never promise free repairs", text.lower())
        self.assertTrue(chunk_text(text))

    def test_citations_include_source_page_and_real_similarity(self):
        chunk = SimpleNamespace(
            id=3,
            document_id=7,
            content="Battery troubleshooting evidence",
            metadata_json='{"page": 2, "section": "Battery & Power"}',
            document=SimpleNamespace(id=7, title="AeroBook Manual", filename="AeroBook.pdf"),
        )
        result = _make_search_result(chunk, 0.18)
        self.assertIn("AeroBook.pdf", result.citation)
        self.assertIn("Page 2", result.citation)
        self.assertIn("Relevance 82%", result.citation)
        self.assertAlmostEqual(result.similarity, 0.82)

    def test_retrieval_citations_are_structured_and_duplicate_locations_removed(self):
        first = SimpleNamespace(
            chunk_id=1, document_id=7, document_title="VisionBeam Manual",
            filename="visionbeam.md", content="Grounded projector guidance",
            similarity=0.63, citation="Source: VisionBeam Manual (visionbeam.md) | Section 2 | Page 3 | Relevance 63%",
        )
        repeated = SimpleNamespace(
            chunk_id=2, document_id=7, document_title="VisionBeam Manual",
            filename="visionbeam.md", content="More grounded projector guidance",
            similarity=0.61, citation="Source: VisionBeam Manual (visionbeam.md) | Section 2 | Page 3 | Relevance 61%",
        )
        with patch("app.services.rag.retrieval.search_knowledge_base", return_value=[first, repeated]):
            context = retrieve_knowledge_context("projector", object())
        self.assertEqual(len(context.citations), 1)
        self.assertEqual(context.citations[0]["document"], "VisionBeam Manual")
        self.assertEqual(context.citations[0]["section"], "Section 2")
        self.assertEqual(context.citations[0]["page"], 3)
        self.assertEqual(context.citations[0]["similarity"], 0.63)

    def test_workflow_never_appends_source_debugging_to_email_body(self):
        workflow_path = Path(__file__).resolve().parents[1] / "app" / "services" / "langchain_workflow" / "workflow.py"
        source = workflow_path.read_text(encoding="utf-8")
        self.assertNotIn('resp_body += "\\n\\nSources:', source)
        self.assertIn("citations=retrieved_ctx.citations", source)

    def test_financial_policy_faq_is_not_confused_with_a_refund_request(self):
        self.assertFalse(assess_inbound_email_risk("Refund policy", "What is your refund policy?").requires_human_review)
        self.assertFalse(assess_inbound_email_risk("Refund", "Please process my refund.").requires_human_review)
        self.assertTrue(assess_inbound_email_risk(
            "Updated payment details",
            "We changed our bank account. Redirect today's invoice wire to this new account and transfer the funds today.",
        ).requires_human_review)

    def test_empty_and_provider_error_responses_are_rejected(self):
        for raw in ('{"subject":"Re: Help","body":" "}', '{"subject":"Re: Help","body":"API error: invalid model"}', ""):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                _parse_json_or_fallback(raw)

    def test_email_json_is_split_and_markdown_cleaned(self):
        parsed = _parse_json_or_fallback(
            '{"subject":"Re: VisionBeam","body":"Hello Ravi,\\n\\n**Try this:**\\n1. Open `Settings`.\\n- Select *Input*."}'
        )
        self.assertEqual(parsed["subject"], "Re: VisionBeam")
        self.assertEqual(parsed["body"], "Hello Ravi,\n\nTry this:\n1. Open Settings.\n- Select Input.")
        with self.assertRaises(ValueError):
            _parse_json_or_fallback('{"subject":"Re: help","body": [broken json')

    def test_json_wrapper_and_sources_are_rejected_as_stored_email_body(self):
        for body in ('{"subject":"Re: help","body":"hello"}', "Hello.\n\nSources:\n- manual.pdf"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                _validate_response_body(body)

    def test_citation_only_and_error_text_cannot_be_saved(self):
        for body in ("Source: manual.pdf | Page 2 | Relevance 82%", "Error: request failed", "  "):
            with self.subTest(body=body), self.assertRaises(ValueError):
                _validate_response_body(body)

    def test_prompt_does_not_invent_default_warranty_terms(self):
        self.assertNotIn("standard 1-year limited warranty", SYSTEM_SUPPORT_INSTRUCTIONS)
        self.assertIn("must confirm", SYSTEM_SUPPORT_INSTRUCTIONS)

    def test_rate_limit_error_is_actionable_without_provider_payload(self):
        diagnostic = safe_workflow_error(RuntimeError("429 free-models-per-day quota exceeded; add credits"))
        self.assertIn("daily free-model request limit", diagnostic)
        self.assertIn("HTTP 429", diagnostic)
        self.assertNotIn("add credits", diagnostic.lower())

    def test_product_resolution_uses_catalog_names_and_skus_without_hardcoding(self):
        class FakeRows:
            def all(self):
                return [
                    SimpleNamespace(id=7, name="VisionBeam 4K Portable Projector", sku="ST-VIS-002"),
                    SimpleNamespace(id=8, name="AeroBook Ultra 14 Laptop", sku="ST-AERO-014"),
                ]

        class FakeDb:
            def scalars(self, _query):
                return FakeRows()

        self.assertEqual(
            identify_product_id("Need help focusing VisionBeam 4K Portable Projector", FakeDb()),
            7,
        )
        self.assertEqual(identify_product_id("ST-AERO-014 battery issue", FakeDb()), 8)
        self.assertIsNone(identify_product_id("How do I contact support?", FakeDb()))

    def test_ticket_trace_ui_surfaces_failed_generation_diagnostic(self):
        app_script = Path(__file__).resolve().parents[2] / "frontend" / "js" / "app.js"
        script = app_script.read_text(encoding="utf-8")
        self.assertIn("lastExec.error_message", script)
        self.assertIn("generation failed before evaluation", script)
        self.assertIn("chunks_retrieved", script)

    def test_draft_schemas_reject_provider_errors_and_citation_only_text(self):
        for body in ("Error: request failed", "Source: manual.pdf | Page 2"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                DraftCreate(recipient="a@example.com", subject="Re: help", body=body)
            with self.subTest(update_body=body), self.assertRaises(ValueError):
                DraftUpdate(body=body)

    def test_login_page_masks_password_and_never_persists_it(self):
        login_script = Path(__file__).resolve().parents[2] / "frontend" / "js" / "login.js"
        html = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
        script_text = login_script.read_text(encoding="utf-8")
        html_text = html.read_text(encoding="utf-8")
        self.assertIn('type="password"', script_text)
        self.assertIn("togglePasswordVisibility", script_text)
        self.assertIn('src="js/login.js', html_text)
        self.assertNotIn("st_trusted_auth_", script_text)
        self.assertNotIn("localStorage.setItem(_REMEMBERED", script_text)


if __name__ == "__main__":
    unittest.main()
