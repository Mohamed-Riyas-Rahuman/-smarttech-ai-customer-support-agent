"""Tests for Phase 1: Customer Data Import (CSV/Excel), validation, duplicate handling, and transactional consistency."""
import io
import unittest
import uuid
import openpyxl
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.db.session import SessionLocal
from app.models.customer import Customer
from app.models.device import Device
from app.models.order import Order
from app.models.product import Product
from app.models.warranty import Warranty
from app.services.customer_import import generate_sample_csv_template


class CustomerImportTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.db = SessionLocal()
        self.test_prefix = f"import_{uuid.uuid4().hex[:8]}"

        # Login as Manager
        res = self.client.post("/api/auth/login", json={
            "email": "manager@smarttech.local",
            "password": "ManagerPass123!",
        })
        self.assertEqual(res.status_code, 200, f"Login failed: {res.text}")
        self.token = res.cookies.get('st_session')
        self.headers = {"Authorization": f"Bearer {self.token}"}

        self.cleanup_customer_emails = []

    def tearDown(self):
        try:
            for email in self.cleanup_customer_emails:
                cust = self.db.scalar(select(Customer).where(Customer.email == email))
                if cust:
                    # Cascade deletes devices, orders, warranties
                    self.db.delete(cust)
            self.db.commit()
        except Exception:
            self.db.rollback()
        finally:
            self.db.close()

    def test_download_import_template(self):
        """Verify downloading the standard CSV import template."""
        res = self.client.get("/api/customers/import/template", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers.get("content-type", ""))
        self.assertIn("customer_email", res.text)
        self.assertIn("order_number", res.text)
        self.assertIn("device_serial", res.text)
        self.assertIn("warranty_policy", res.text)

    def test_preview_csv_valid_and_duplicate_detection(self):
        """Test previewing a CSV with valid records, duplicate records, and invalid records."""
        email1 = f"{self.test_prefix}_user1@example.com"
        email2 = f"{self.test_prefix}_user2@example.com"
        self.cleanup_customer_emails.extend([email1, email2])

        # Pre-seed one customer in database to test existing duplicate detection
        existing_cust = Customer(
            email=email1,
            full_name="Existing Customer",
        )
        self.db.add(existing_cust)
        self.db.commit()

        csv_data = (
            "customer_name,customer_email,phone,product_sku,order_number,device_serial,warranty_policy\n"
            # Row 1: Valid new record
            f"Brand New User,{email2},+1-555-9988,ST-AUD-001,ORD-{self.test_prefix}-01,ST-SN-{self.test_prefix}-01,WAR-{self.test_prefix}-01\n"
            # Row 2: Duplicate customer email (already in DB)
            f"Duplicate User,{email1},+1-555-0000,ST-AUD-001,ORD-{self.test_prefix}-02,ST-SN-{self.test_prefix}-02,WAR-{self.test_prefix}-02\n"
            # Row 3: Invalid row (bad email and unknown SKU)
            f"Invalid User,not-an-email,,UNKNOWN-SKU,,,\n"
        )

        files = {"file": ("test_import.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
        res = self.client.post("/api/customers/import/preview", headers=self.headers, files=files)
        self.assertEqual(res.status_code, 200, f"Preview failed: {res.text}")
        data = res.json()

        self.assertEqual(data["total_rows"], 3)
        self.assertEqual(data["valid_count"], 1)
        self.assertEqual(data["duplicate_count"], 1)
        self.assertEqual(data["invalid_count"], 1)
        self.assertTrue(data["can_import"])
        self.assertEqual(len(data["valid_rows"]), 1)
        self.assertEqual(data["valid_rows"][0]["email"], email2)

    def test_preview_and_confirm_excel_xlsx_import(self):
        """Test uploading and confirming an Excel (.xlsx) file with full relational hierarchy."""
        email = f"{self.test_prefix}_xlsx@example.com"
        order_num = f"ORD-{self.test_prefix}-99"
        serial = f"ST-SN-{self.test_prefix}-99"
        policy = f"WAR-{self.test_prefix}-99"
        self.cleanup_customer_emails.append(email)

        # Create XLSX in-memory
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([
            "customer_name", "customer_email", "phone", "company", "address",
            "product_sku", "order_number", "order_status", "total_amount",
            "carrier", "tracking_number", "order_date", "item_quantity", "item_price",
            "device_serial", "device_nickname", "purchase_date",
            "warranty_policy", "coverage_type", "warranty_status", "warranty_start", "warranty_end", "warranty_terms"
        ])
        ws.append([
            "Excel Import User", email, "+1-555-4321", "Excel Corp", "123 Spreadsheet Ave",
            "ST-AUD-001", order_num, "shipped", 249.99,
            "FedEx", f"TRK-{self.test_prefix}", "2026-04-10", 1, 249.99,
            serial, "My SoundPulse", "2026-04-10",
            policy, "standard", "active", "2026-04-10", "2028-04-10", "Full 24-month warranty."
        ])

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        # 1. Preview
        files = {"file": ("test_import.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        res_prev = self.client.post("/api/customers/import/preview", headers=self.headers, files=files)
        self.assertEqual(res_prev.status_code, 200, f"Preview failed: {res_prev.text}")
        prev_data = res_prev.json()
        self.assertEqual(prev_data["valid_count"], 1)

        # 2. Confirm
        confirm_payload = {"rows": prev_data["valid_rows"]}
        res_conf = self.client.post("/api/customers/import/confirm", headers=self.headers, json=confirm_payload)
        self.assertEqual(res_conf.status_code, 200, f"Confirm failed: {res_conf.text}")
        conf_data = res_conf.json()
        self.assertEqual(conf_data["status"], "success")
        self.assertEqual(conf_data["imported_customers"], 1)
        self.assertEqual(conf_data["imported_orders"], 1)
        self.assertEqual(conf_data["imported_devices"], 1)
        self.assertEqual(conf_data["imported_warranties"], 1)

        # 3. Verify in Database
        self.db.expire_all()
        cust = self.db.scalar(select(Customer).where(Customer.email == email))
        self.assertIsNotNone(cust)
        self.assertEqual(cust.full_name, "Excel Import User")

        order = self.db.scalar(select(Order).where(Order.order_number == order_num.upper()))
        self.assertIsNotNone(order)
        self.assertEqual(order.customer_id, cust.id)
        self.assertEqual(order.status, "shipped")

        device = self.db.scalar(select(Device).where(Device.serial_number == serial.upper()))
        self.assertIsNotNone(device)
        self.assertEqual(device.customer_id, cust.id)
        self.assertEqual(device.order_id, order.id)

        warranty = self.db.scalar(select(Warranty).where(Warranty.policy_number == policy.upper()))
        self.assertIsNotNone(warranty)
        self.assertEqual(warranty.customer_id, cust.id)
        self.assertEqual(warranty.device_id, device.id)
        self.assertEqual(warranty.status, "active")

    def test_import_unsupported_file_format(self):
        """Reject files that are not CSV or XLSX."""
        files = {"file": ("script.py", io.BytesIO(b"print('hello')"), "text/x-python")}
        res = self.client.post("/api/customers/import/preview", headers=self.headers, files=files)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unsupported file format", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
