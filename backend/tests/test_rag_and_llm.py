"""Tests for RAG ingestion, chunking, pgvector search, and Groq integration."""

import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.db.session import SessionLocal
from app.models.product import Product
from app.services.rag.chunking import chunk_text, validate_file
from app.services.rag.embeddings import embedding_service
from app.integrations.llm.llm_service import groq_model_status


class RAGAndLLMTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

        # Login as Manager to run seed/upload tests.
        res = self.client.post(
            "/api/auth/login",
            json={
                "email": "manager@smarttech.local",
                "password": "ManagerPass123!",
            },
        )

        self.assertEqual(
            res.status_code,
            200,
            f"Manager login failed: {res.text}",
        )

        self.manager_token = res.cookies.get('st_session')
        self.headers = {
            "Authorization": f"Bearer {self.manager_token}"
        }

    def test_text_chunking_and_metadata(self):
        sample_doc = (
            "Section 1: Setup\n"
            "This is the initial setup instruction for the smart hardware device.\n\n"
            "Section 2: Bluetooth Reset\n"
            "To reset the bluetooth connectivity, hold power and volume up for 10 seconds.\n"
            "The LED will flash blue twice to confirm the factory reset."
        )

        chunks = chunk_text(
            sample_doc,
            chunk_size=120,
            chunk_overlap=30,
        )

        self.assertGreaterEqual(len(chunks), 1)
        self.assertEqual(chunks[0].chunk_index, 0)
        self.assertIn("Section", chunks[0].content)

        # Chunk indices should always be sequential and zero-based.
        self.assertEqual(
            [chunk.chunk_index for chunk in chunks],
            list(range(len(chunks))),
        )

    def test_long_troubleshooting_procedure_is_preserved_with_its_heading(self):
        """Generic chunking keeps a section heading and every step searchable."""

        manual_path = (
            Path(__file__).resolve().parents[2]
            / "data"
            / "sample_knowledge_base"
            / "SoundPulse_Pro_Manual.txt"
        )

        self.assertTrue(
            manual_path.is_file(),
            f"SoundPulse Pro manual not found: {manual_path}",
        )

        manual_text = manual_path.read_text(encoding="utf-8")
        chunks = chunk_text(manual_text)

        procedure_chunks = [
            chunk
            for chunk in chunks
            if "Firmware Update Stalls and Rapid Red LED"
            in chunk.content
        ]

        self.assertGreaterEqual(len(procedure_chunks), 1)

        firmware_content = "\n".join(chunk.content for chunk in procedure_chunks).lower()

        required_instructions = [
            "disconnect the headphones from any charging cable or audio wire",
            "power button and the anc button simultaneously for 12 seconds",
            "led flashes purple twice",
            "forget this device",
            "retry the update in the smarttech companion app",
        ]

        for instruction in required_instructions:
            with self.subTest(instruction=instruction):
                self.assertIn(
                    instruction,
                    firmware_content,
                    f"Firmware chunk is missing instruction: {instruction}",
                )

        # Verify that the numbered procedure is complete and ordered.
        step_positions = [
            firmware_content.find("1. disconnect the headphones"),
            firmware_content.find("2. press and hold down both"),
            firmware_content.find("3. release both buttons"),
            firmware_content.find("4. on your smartphone/tablet"),
            firmware_content.find("5. power on the headphones"),
        ]

        self.assertTrue(
            all(position >= 0 for position in step_positions),
            "One or more numbered firmware steps are missing.",
        )

        self.assertEqual(
            step_positions,
            sorted(step_positions),
            "The firmware recovery steps are not in the correct order.",
        )

        self.assertEqual([chunk.chunk_index for chunk in chunks], list(range(len(chunks))))

    def test_generic_issue_heading_and_reference_survive_chunking(self):
        """Issue identifiers stay with their procedures without product rules."""

        sample_doc = (
            "Section 7: Connectivity Troubleshooting\n"
            "Issue 7.1: Intermittent Link Failure NET-LINK-07\n"
            "1. Disconnect the network cable.\n"
            "2. Restart the gateway and reconnect the cable.\n"
            "3. Check the connection status in Network Settings.\n\n"
            "Section 8: Warranty Coverage\n"
            "The device has a limited warranty."
        )

        chunks = chunk_text(sample_doc, chunk_size=160)
        issue_chunks = [
            chunk
            for chunk in chunks
            if "NET-LINK-07" in chunk.content
        ]

        self.assertTrue(issue_chunks)
        issue_content = "\n".join(chunk.content for chunk in issue_chunks)
        self.assertIn("Issue 7.1: Intermittent Link Failure", issue_content)
        self.assertIn("Check the connection status", issue_content)
        self.assertTrue(any(chunk.metadata.get("reference") == "NET-LINK-07" for chunk in issue_chunks))
        self.assertNotIn("Section 8: Warranty Coverage", issue_content)

    def test_embedding_service_dimensions(self):
        vec = embedding_service.get_embedding(
            "Bluetooth connection failed on SoundPulse Pro"
        )

        self.assertEqual(len(vec), 384)

        # Vector should be L2 normalized (length approximately 1.0).
        norm = sum(x * x for x in vec) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=3)

    def test_seed_sample_documents_and_search(self):
        # 1. Trigger sample docs ingestion.
        seed_res = self.client.post(
            "/api/knowledge/seed-sample-docs",
            headers=self.headers,
        )

        self.assertEqual(seed_res.status_code, 200)

        # 2. Verify documents are listed and indexed.
        docs_res = self.client.get(
            "/api/knowledge/documents",
            headers=self.headers,
        )

        self.assertEqual(docs_res.status_code, 200)

        docs = docs_res.json()
        self.assertGreaterEqual(len(docs), 3)

        indexed_docs = [
            document
            for document in docs
            if document["is_indexed"]
        ]

        self.assertGreaterEqual(len(indexed_docs), 3)

        # 3. Search within the registered SoundPulse product. The retrieval
        # API intentionally does not search product manuals without identity.
        with SessionLocal() as db:
            soundpulse = db.scalar(
                select(Product).where(Product.sku == "ST-AUD-001")
            )
        self.assertIsNotNone(soundpulse)
        search_res = self.client.post(
            "/api/knowledge/search",
            headers=self.headers,
            json={
                "query": (
                    "SoundPulse Pro firmware update stalled LED flashing red "
                    "rapid packet timeout"
                ),
                "product_id": soundpulse.id,
                "top_k": 3,
            },
        )

        self.assertEqual(search_res.status_code, 200)

        results = search_res.json()
        self.assertGreaterEqual(len(results), 1)

        top_match = results[0]

        self.assertIn("SoundPulse", top_match["citation"])
        retrieved_evidence = "\n".join(result["content"] for result in results).lower()
        self.assertIn("reset", retrieved_evidence)

    def test_groq_model_availability_is_safe(self):
        from unittest.mock import patch
        from app.integrations.llm import llm_service

        response = type("Response", (), {
            "status_code": 200,
            "json": lambda _self: {"data": [{"id": "openai/gpt-oss-20b"}]},
        })()
        with patch.object(llm_service.settings, "groq_api_key", "gsk_test_secret_value"), \
             patch.object(llm_service.settings, "groq_model", "openai/gpt-oss-20b"), \
             patch.object(llm_service.httpx, "get", return_value=response) as get:
            status = groq_model_status(force_refresh=True)

        self.assertEqual(status["status"], "available")
        self.assertEqual(status["model"], "openai/gpt-oss-20b")
        self.assertNotIn("gsk_test_secret_value", str(status))
        self.assertTrue(get.call_args.args[0].startswith("https://api.groq.com/openai/v1/models"))

    def test_upload_file_validation(self):
        # Unsupported file type raises an error.
        with self.assertRaises(ValueError):
            validate_file("dangerous_script.exe", 1024)

        # Files exceeding 10 MB raise an error.
        with self.assertRaises(ValueError):
            validate_file(
                "huge_manual.pdf",
                15 * 1024 * 1024,
            )


if __name__ == "__main__":
    unittest.main()

