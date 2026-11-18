"""Tests for Authentication, JWT tokens, and RBAC enforcement."""
import unittest
from fastapi.testclient import TestClient
from app.main import app


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_login_success_admin(self):
        res = self.client.post("/api/auth/login", json={
            "email": "admin@smarttech.local",
            "password": "AdminPass123!",
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertNotIn("access_token", data)
        self.assertIn("st_session", res.cookies)
        self.assertEqual(data["user"]["role"], "admin")
        self.assertEqual(data["user"]["email"], "admin@smarttech.local")

    def test_login_invalid_password(self):
        res = self.client.post("/api/auth/login", json={
            "email": "admin@smarttech.local",
            "password": "WrongPassword!",
        })
        self.assertEqual(res.status_code, 401)
        self.assertIn("Incorrect email or password", res.json()["detail"])

    def test_login_nonexistent_user(self):
        res = self.client.post("/api/auth/login", json={
            "email": "ghost@smarttech.local",
            "password": "SomePassword123!",
        })
        self.assertEqual(res.status_code, 401)

    def test_get_me_authenticated(self):
        # 1. Login
        login_res = self.client.post("/api/auth/login", json={
            "email": "agent@smarttech.local",
            "password": "AgentPass123!",
        })
        token = login_res.cookies.get('st_session')

        # 2. Get Me
        me_res = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me_res.status_code, 200)
        user = me_res.json()
        self.assertEqual(user["email"], "agent@smarttech.local")
        self.assertEqual(user["role"], "agent")

    def test_get_me_unauthenticated(self):
        res = self.client.get("/api/auth/me")
        self.assertEqual(res.status_code, 401)

    def test_admin_rbac_protection(self):
        # Agent tries to list users -> 403 Forbidden
        agent_login = self.client.post("/api/auth/login", json={
            "email": "agent@smarttech.local",
            "password": "AgentPass123!",
        })
        agent_token = agent_login.cookies.get('st_session')

        res = self.client.get("/api/auth/users", headers={"Authorization": f"Bearer {agent_token}"})
        self.assertEqual(res.status_code, 403)

        # Admin lists users -> 200 OK
        admin_login = self.client.post("/api/auth/login", json={
            "email": "admin@smarttech.local",
            "password": "AdminPass123!",
        })
        admin_token = admin_login.cookies.get('st_session')

        res = self.client.get("/api/auth/users", headers={"Authorization": f"Bearer {admin_token}"})
        self.assertEqual(res.status_code, 200)
        users = res.json()
        self.assertGreaterEqual(len(users), 4)

    def test_active_login_user_chooser_and_dashboard_session(self):
        users_res = self.client.get("/api/auth/login-users")
        self.assertEqual(users_res.status_code, 200)
        self.assertTrue(any(u["email"] == "agent@smarttech.local" for u in users_res.json()))
        self.assertNotIn("hashed_password", users_res.text)
        self.assertTrue(all(set(user) == {"email"} for user in users_res.json()))

        login_res = self.client.post("/api/auth/login", json={
            "email": "agent@smarttech.local", "password": "AgentPass123!",
        })
        self.assertEqual(login_res.status_code, 200)
        self.assertNotIn("access_token", login_res.json())
        self.assertEqual(self.client.get("/api/dashboard/stats").status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/dashboard/stats").status_code, 401)

    def test_dashboard_requires_authentication(self):
        self.assertEqual(self.client.get("/api/dashboard/stats").status_code, 401)


if __name__ == "__main__":
    unittest.main()
