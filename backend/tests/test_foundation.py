"""Tests for foundation endpoints, configuration, and security."""
import unittest
from fastapi.testclient import TestClient
from app.main import app
from app.core.security import hash_password, verify_password, create_access_token, decode_access_token
from app.db.session import SessionLocal
from app.services.auto_send import is_auto_send_enabled


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_root_endpoint(self):
        response = self.client.get('/', follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers['location'], '/app/')

    def test_health_endpoint(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        with SessionLocal() as db:
            effective_auto_send = is_auto_send_enabled(db)
        self.assertEqual(data["auto_send_enabled"], effective_auto_send)

    def test_ready_endpoint(self):
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("ready", data)
        self.assertIn("dependencies", data)

    def test_password_hashing(self):
        password = "SecurePassword123!"
        hashed = hash_password(password)
        self.assertNotEqual(password, hashed)
        self.assertTrue(verify_password(password, hashed))
        self.assertFalse(verify_password("WrongPassword", hashed))

    def test_jwt_token_roundtrip(self):
        payload = {"sub": "1", "email": "admin@smarttech.local", "role": "admin"}
        token = create_access_token(payload)
        self.assertIsInstance(token, str)
        decoded = decode_access_token(token)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["sub"], "1")
        self.assertEqual(decoded["role"], "admin")

    def test_jwt_invalid_token(self):
        decoded = decode_access_token("invalid.token.signature")
        self.assertIsNone(decoded)


if __name__ == "__main__":
    unittest.main()



