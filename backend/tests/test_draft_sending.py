
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.audit import AuditLog
from app.models.audit import SystemSetting
from app.models.draft import Draft
from app.services.langchain_workflow.quality_checks import (
    QualityChecker,
    SafetyCheckResult,
    SentimentCheckResult,
)


class DraftSendingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.gmail_profile_patch = patch(
            "app.api.routers.drafts.gmail_service.get_authorization_status",
            return_value={
                "status": "connected", "connection_state": "connected",
                "is_authorized": True, "has_send_permission": True,
                "has_modify_permission": True, "permissions_sufficient": True,
                "connected_email": "support@smarttech.local",
            },
        )
        self.gmail_profile_patch.start()
        self.client = TestClient(app)
        self.created_draft_ids = []
        self.langfuse_patcher = patch("app.services.langfuse_events.record_approval_event")
        self.langfuse_patcher.start()

        response = self.client.post(
            "/api/auth/login",
            json={
                "email": "manager@smarttech.local",
                "password": "ManagerPass123!",
            },
        )

        self.assertEqual(
            response.status_code,
            200,
            f"Manager login failed: {response.text}",
        )

        token = response.cookies.get('st_session')
        self.headers = {"Authorization": f"Bearer {token}"}

        admin_response = self.client.post(
            "/api/auth/login",
            json={"email": "admin@smarttech.local", "password": "AdminPass123!"},
        )
        self.admin_headers = {"Authorization": f"Bearer {admin_response.cookies.get('st_session')}"}

    def tearDown(self):
        self.gmail_profile_patch.stop()
        self.langfuse_patcher.stop()
        with SessionLocal() as db:
            for draft_id in self.created_draft_ids:
                for audit in db.scalars(select(AuditLog).where(
                    AuditLog.entity_type == "draft", AuditLog.entity_id == str(draft_id)
                )).all():
                    db.delete(audit)
                draft = db.get(Draft, draft_id)
                if draft:
                    db.delete(draft)
            db.commit()

    def create_approved_draft(self):
        response = self.client.post(
            "/api/drafts",
            headers=self.headers,
            json={
                "recipient": "test-recipient@example.com",
                "subject": "Automated safety test",
                "body": "This message must not reach a real recipient.",
            },
        )

        self.assertEqual(
            response.status_code,
            201,
            f"Draft creation failed: {response.text}",
        )

        draft_id = response.json()["id"]
        self.created_draft_ids.append(draft_id)

        approval = self.client.post(
            f"/api/drafts/{draft_id}/approve",
            headers=self.headers,
        )

        self.assertEqual(
            approval.status_code,
            200,
            f"Draft approval failed: {approval.text}",
        )

        return draft_id

    def get_draft(self, draft_id):
        response = self.client.get(
            "/api/drafts",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)

        draft = next(
            (
                item
                for item in response.json()
                if item["id"] == draft_id
            ),
            None,
        )

        self.assertIsNotNone(draft, "Draft was not found")
        return draft

    def test_unapproved_draft_stays_pending(self):
        response = self.client.post(
            "/api/drafts",
            headers=self.headers,
            json={"recipient": "test-recipient@example.com", "subject": "Pending draft", "body": "Review required."},
        )
        self.assertEqual(response.status_code, 201)
        self.created_draft_ids.append(response.json()["id"])
        self.assertEqual(response.json()["status"], "pending_review")

    def test_auto_send_activation_requires_verified_gmail_and_persists(self):
        with SessionLocal() as db:
            old_setting = db.scalar(select(SystemSetting).where(SystemSetting.key == "auto_send_enabled"))
            old_value = old_setting.value if old_setting else None
        old_config_value = settings.auto_send_enabled
        verified_profile = {
            "status": "connected", "connection_state": "connected",
            "is_authorized": True, "gmail_api_available": True,
            "has_send_permission": True, "permissions_sufficient": True,
            "connected_email": "support@smarttech.local",
        }
        try:
            with patch("app.services.integrations.gmail.gmail_service.get_authorization_status", return_value=verified_profile):
                response = self.client.patch(
                    "/api/settings/auto_send_enabled",
                    headers=self.admin_headers,
                    json={"value": "true"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                status = self.client.get("/api/settings/status", headers=self.admin_headers)
                self.assertEqual(status.status_code, 200)
                self.assertTrue(status.json()["auto_send_enabled"])
                self.assertTrue(status.json()["gmail_account_verified"])
        finally:
            with SessionLocal() as db:
                setting = db.scalar(select(SystemSetting).where(SystemSetting.key == "auto_send_enabled"))
                if old_value is None:
                    if setting:
                        db.delete(setting)
                elif setting:
                    setting.value = old_value
                else:
                    db.add(SystemSetting(key="auto_send_enabled", value=old_value, description="Restored by isolated test"))
                db.commit()
            settings.auto_send_enabled = old_config_value

    def test_auto_send_activation_is_blocked_when_gmail_authorization_expired(self):
        with SessionLocal() as db:
            setting = db.scalar(select(SystemSetting).where(SystemSetting.key == "auto_send_enabled"))
            old_value = setting.value if setting else None
        old_config_value = settings.auto_send_enabled
        expired_profile = {
            "status": "authorization_expired", "connection_state": "authorization_expired",
            "is_authorized": False, "gmail_api_available": None,
            "has_send_permission": False, "permissions_sufficient": False,
            "connected_email": None,
        }
        try:
            with patch("app.services.integrations.gmail.gmail_service.get_authorization_status", return_value=expired_profile):
                response = self.client.patch(
                    "/api/settings/auto_send_enabled",
                    headers=self.admin_headers,
                    json={"value": "true"},
                )
                self.assertEqual(response.status_code, 409)
                self.assertIn("Reconnect Gmail", response.json()["detail"])
        finally:
            with SessionLocal() as db:
                setting = db.scalar(select(SystemSetting).where(SystemSetting.key == "auto_send_enabled"))
                if old_value is None:
                    if setting:
                        db.delete(setting)
                elif setting:
                    setting.value = old_value
                else:
                    db.add(SystemSetting(key="auto_send_enabled", value=old_value, description="Restored by isolated test"))
                db.commit()
            settings.auto_send_enabled = old_config_value

    def test_workflow_uses_persisted_auto_send_setting_over_environment_fallback(self):
        checker = object.__new__(QualityChecker)
        checker.evaluate_sentiment = lambda *_args: SentimentCheckResult(
            customer_sentiment="neutral", is_empathetic_and_appropriate=True,
            tone_score=1.0, tone_feedback="ok",
        )
        checker.evaluate_safety = lambda *_args: SafetyCheckResult(
            is_relevant=True, is_grounded=True, policy_compliant=True,
            confidence_score=0.95, requires_human_review=False,
        )
        with patch.object(settings, "auto_send_enabled", False):
            persisted_enabled = checker.run_all_checks(
                subject="Product FAQ", email_body="How do I pair this?",
                retrieved_knowledge="Verified manual content", response_body="Use the pairing button.",
                auto_send_enabled=True,
            )
            persisted_disabled = checker.run_all_checks(
                subject="Product FAQ", email_body="How do I pair this?",
                retrieved_knowledge="Verified manual content", response_body="Use the pairing button.",
                auto_send_enabled=False,
            )
        self.assertFalse(persisted_enabled.requires_human_review)
        self.assertFalse(persisted_disabled.requires_human_review)

    def test_customer_approval_detector_uses_fraud_indicators_not_support_keywords(self):
        from app.services.auto_send import customer_requires_human_approval

        suspicious = [
            "Your account will be suspended. Reply with your password and OTP at this link immediately.",
            "We changed our bank account. Redirect today's invoice wire to this new account.",
            "I am the CFO. Urgently transfer funds to my personal crypto wallet and keep this confidential.",
        ]
        normal = [
            "Headphones not charging",
            "Bluetooth troubleshooting",
            "Product FAQ",
            "Where can I find the product manual?",
            "I am unhappy with this product",
            "What is the delivery status?",
            "My account page is not loading",
            "What is your refund policy?",
            "I want a refund for my order.",
            "Is this email a scam? It says my order refund was processed.",
            "The bank transfer payment status is still pending. Can you check it?",
            "I want to speak with a human about my warranty.",
        ]
        for text in suspicious:
            with self.subTest(text=text):
                self.assertTrue(customer_requires_human_approval("", text))
        for text in normal:
            with self.subTest(text=text):
                self.assertFalse(customer_requires_human_approval("", text))

    def test_approved_manual_send_is_available_when_auto_send_is_disabled(self):
        with patch.object(
            settings,
            "auto_send_enabled",
            False,
        ), patch(
            "app.api.routers.drafts.gmail_service.send_reply",
            return_value={"id": "mock-message-id", "thread_id": "mock-thread-id"},
        ) as send_reply, patch(
            "app.api.routers.drafts.gmail_service.is_authorized", return_value=True
        ):
            draft_id = self.create_approved_draft()

            response = self.client.post(
                f"/api/drafts/{draft_id}/send",
                headers=self.headers,
            )

            self.assertEqual(response.status_code, 200)
            send_reply.assert_called_once()

            draft = self.get_draft(draft_id)
            self.assertEqual(draft["status"], "sent")
            self.assertIsNotNone(draft["sent_at"])

    def test_gmail_failure_leaves_draft_ambiguous(self):
        with patch.object(
            settings,
            "auto_send_enabled",
            True,
        ), patch(
            "app.api.routers.drafts.gmail_service.is_authorized",
            return_value=True,
        ), patch(
            "app.api.routers.drafts.gmail_service.send_reply",
            side_effect=RuntimeError("Simulated Gmail timeout"),
        ) as send_reply:
            draft_id = self.create_approved_draft()

            response = self.client.post(
                f"/api/drafts/{draft_id}/send",
                headers=self.headers,
            )

            self.assertEqual(response.status_code, 502)

            draft = self.get_draft(draft_id)
            self.assertEqual(draft["status"], "send_ambiguous")
            self.assertIsNone(draft["sent_at"])
            send_reply.assert_called_once()

            retry = self.client.post(
                f"/api/drafts/{draft_id}/send",
                headers=self.headers,
            )

            self.assertEqual(retry.status_code, 409)
            send_reply.assert_called_once()

    def test_successful_gmail_response_marks_draft_sent(self):
        gmail_result = {
            "id": "mock-message-id",
            "thread_id": "mock-thread-id",
            "to": "test-recipient@example.com",
        }

        with patch.object(
            settings,
            "auto_send_enabled",
            True,
        ), patch(
            "app.api.routers.drafts.gmail_service.is_authorized",
            return_value=True,
        ), patch(
            "app.api.routers.drafts.gmail_service.send_reply",
            return_value=gmail_result,
        ) as send_reply:
            draft_id = self.create_approved_draft()

            response = self.client.post(
                f"/api/drafts/{draft_id}/send",
                headers=self.headers,
            )

            self.assertEqual(
                response.status_code,
                200,
                f"Send failed: {response.text}",
            )

            result = response.json()
            self.assertEqual(result["status"], "sent")
            self.assertEqual(result["mode"], "live_gmail")

            draft = self.get_draft(draft_id)
            self.assertEqual(draft["status"], "sent")
            self.assertIsNotNone(draft["sent_at"])
            send_reply.assert_called_once()

            duplicate = self.client.post(
                f"/api/drafts/{draft_id}/send",
                headers=self.headers,
            )

            self.assertEqual(duplicate.status_code, 409)
            send_reply.assert_called_once()


if __name__ == "__main__":
    unittest.main()

