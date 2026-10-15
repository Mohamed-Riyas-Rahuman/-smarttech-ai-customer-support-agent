import unittest
from app.services.draft_policy import DraftState, PolicyError, approve, assert_can_send, edit


class PolicyTests(unittest.TestCase):
    def test_unapproved_cannot_send(self):
        with self.assertRaises(PolicyError):
            assert_can_send(DraftState(body="hi"))

    def test_agent_role_cannot_approve(self):
        with self.assertRaises(PolicyError):
            approve(DraftState(body="hi"), 1, "agent")

    def test_approved_can_send(self):
        assert_can_send(approve(DraftState(body="hi"), 1, "reviewer"))

    def test_edit_invalidates_approval(self):
        d = edit(approve(DraftState(body="hi"), 1, "admin"), "changed")
        self.assertEqual(d.status, "pending_review")
        with self.assertRaises(PolicyError):
            assert_can_send(d)

    def test_tampered_body_blocked(self):
        d = approve(DraftState(body="hi"), 1, "admin")
        with self.assertRaises(PolicyError):
            assert_can_send(DraftState(body="evil", status="approved", approved_by=1, approved_hash=d.approved_hash))

    def test_no_double_send_or_ambiguous_resend(self):
        for s in ("sent", "send_ambiguous"):
            with self.assertRaises(PolicyError):
                assert_can_send(DraftState(body="x", status=s, approved_by=1))


if __name__ == "__main__":
    unittest.main()
