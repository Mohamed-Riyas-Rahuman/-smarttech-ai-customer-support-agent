"""Tests for Core Support REST APIs: customers, products, orders, devices, warranties, tickets, dashboard, settings."""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app


class CoreAPITests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # Login as Agent
        agent_res = self.client.post("/api/auth/login", json={
            "email": "agent@smarttech.local",
            "password": "AgentPass123!",
        })
        self.agent_token = agent_res.cookies.get('st_session')
        self.agent_headers = {"Authorization": f"Bearer {self.agent_token}"}

        # Login as Analyst
        analyst_res = self.client.post("/api/auth/login", json={
            "email": "analyst@smarttech.local",
            "password": "AnalystPass123!",
        })
        self.analyst_token = analyst_res.cookies.get('st_session')
        self.analyst_headers = {"Authorization": f"Bearer {self.analyst_token}"}

    def test_list_customers(self):
        res = self.client.get("/api/customers", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertGreaterEqual(len(data), 5)
        emails = [c["email"] for c in data]
        self.assertTrue(all(isinstance(email, str) and "@" in email for email in emails))

    def test_get_customer_detail(self):
        # First find Marcus
        res = self.client.get("/api/customers?q=marcus", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        cust_id = res.json()[0]["id"]

        detail_res = self.client.get(f"/api/customers/{cust_id}", headers=self.agent_headers)
        self.assertEqual(detail_res.status_code, 200)
        cust = detail_res.json()
        self.assertEqual(cust["email"], "marcus.vance@example.com")
        self.assertGreaterEqual(cust["order_count"], 1)
        self.assertGreaterEqual(cust["device_count"], 1)

    def test_list_products(self):
        res = self.client.get("/api/products", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        products = res.json()
        self.assertGreaterEqual(len(products), 5)
        skus = [p["sku"] for p in products]
        self.assertIn("ST-AUD-001", skus)

    def test_list_orders(self):
        res = self.client.get("/api/orders", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        orders = res.json()
        self.assertGreaterEqual(len(orders), 4)

    def test_device_serial_number_masking(self):
        # 1. Agent sees unmasked serial number
        res_agent = self.client.get("/api/devices", headers=self.agent_headers)
        self.assertEqual(res_agent.status_code, 200)
        agent_sn = res_agent.json()[0]["serial_number"]
        self.assertNotIn("***", agent_sn)

        # 2. Analyst sees masked serial number
        res_analyst = self.client.get("/api/devices", headers=self.analyst_headers)
        self.assertEqual(res_analyst.status_code, 200)
        analyst_sn = res_analyst.json()[0]["serial_number"]
        self.assertIn("***", analyst_sn)

    def test_warranty_check_active(self):
        # Marcus's SoundPulse Pro: active warranty
        res = self.client.get("/api/warranties/check/ST-SN-AUD-88219", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["has_coverage"])
        self.assertEqual(data["status"], "active")
        self.assertIn("Active warranty verified", data["message"])

    def test_warranty_check_expired(self):
        # David's VisionBeam 4K: expired warranty
        res = self.client.get("/api/warranties/check/ST-SN-VIS-33910", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["has_coverage"])
        self.assertIn("expired", data["message"].lower())

    def test_warranty_check_nonexistent(self):
        res = self.client.get("/api/warranties/check/FAKE-SERIAL-000", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["has_coverage"])
        self.assertEqual(data["status"], "not_found")

    def test_list_tickets_and_create(self):
        # 1. List
        res = self.client.get("/api/tickets", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        tickets = res.json()
        self.assertGreaterEqual(len(tickets), 5)

        # 2. Create
        cust = self.client.get("/api/customers", headers=self.agent_headers).json()[0]
        create_res = self.client.post("/api/tickets", headers=self.agent_headers, json={
            "customer_id": cust["id"],
            "subject": "New Test Inquiry regarding USB-C Cable",
            "priority": "low",
            "category": "general",
            "initial_message": "Is the cable compatible with 60W chargers?",
        })
        self.assertEqual(create_res.status_code, 201)
        new_ticket = create_res.json()
        self.assertEqual(new_ticket["status"], "open")
        self.assertIn("TCK-2026-", new_ticket["ticket_number"])

    def test_dashboard_stats(self):
        res = self.client.get("/api/dashboard/stats", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertGreaterEqual(data["total_open_tickets"], 1)
        self.assertGreaterEqual(data["total_customers"], 5)
        self.assertGreaterEqual(data["total_devices"], 4)
        self.assertIsInstance(data["ticket_trend"], list)
        self.assertIsInstance(data["status_distribution"], list)

    def test_settings_status_no_leak(self):
        res = self.client.get("/api/settings/status", headers=self.agent_headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["database_status"], "ok")
        self.assertIn("v0.8.6", data["pgvector_status"])
        self.assertIn("groq_status", data)
        # Verify no secret leaked
        res_text = res.text
        self.assertNotIn("sk-or", res_text)
        self.assertNotIn("password", res_text)

    def test_settings_status_separates_gmail_gate_states(self):
        profile = {
            "status": "authorization_expired", "connection_state": "authorization_expired",
            "is_authorized": False, "connected_email": None,
            "gmail_api_available": None, "has_send_permission": False,
            "permissions_sufficient": False, "authorization_error": "refresh_token_revoked_or_expired",
            "required_scopes": ["gmail.modify", "gmail.send"],
        }
        with patch("app.services.integrations.gmail.gmail_service.get_authorization_status", return_value=profile):
            response = self.client.get("/api/settings/status", headers=self.agent_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["gmail_status"], "authorization_expired")
        self.assertFalse(data["gmail_oauth_authorized"])
        self.assertFalse(data["gmail_account_verified"])
        self.assertIsNone(data["gmail_api_available"])
        self.assertFalse(data["gmail_send_permission"])
        self.assertTrue(data["human_approval_policy"])


if __name__ == "__main__":
    unittest.main()
