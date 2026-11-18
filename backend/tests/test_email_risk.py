"""Intent-based scam detection tests; no provider or database required."""

import pytest

from app.services.auto_send import customer_requires_human_approval
from app.services.email_risk import assess_inbound_email_risk


@pytest.mark.parametrize(
    "subject,body",
    [
        ("VisionBeam HDMI no signal", "The projector displays No Signal after reconnecting the cable."),
        ("Warranty question", "What does the standard warranty cover?"),
        ("Refund policy", "Please explain your refund policy for a returned order."),
        ("Is this email a scam?", "A message says my order refund was processed. Is it legitimate?"),
        ("Payment status", "My bank transfer payment is pending. Can you check the order status?"),
        ("Human support", "I want to speak with a person about a warranty claim."),
        ("Password reset", "I cannot remember my password. How do I reset it safely?"),
        ("Invoice", "Our invoice amount is incorrect. Please explain the payment status."),
    ],
)
def test_ordinary_support_and_scam_analysis_questions_do_not_require_review(subject, body):
    assessment = assess_inbound_email_risk(subject, body)
    assert not assessment.requires_human_review
    assert not customer_requires_human_approval(subject, body)


@pytest.mark.parametrize(
    "subject,body,signal",
    [
        (
            "Immediate account verification",
            "Your mailbox will be suspended. Reply with your password and OTP at https://bit.ly/verify within 30 minutes.",
            "credential_disclosure_request",
        ),
        (
            "New invoice payment details",
            "We changed our bank account. Redirect today's invoice wire to this new account and transfer the funds today.",
            "payment_destination_change",
        ),
        (
            "Urgent executive request",
            "I am the CFO. Urgently transfer funds to my personal crypto wallet and keep this confidential.",
            "suspicious_transfer_channel",
        ),
        (
            "Security update",
            "Your account will be locked. Click this link and enter your password to verify your account: https://example.test/login",
            "credential_harvesting_link",
        ),
        (
            "Open attached invoice immediately",
            "I am the supplier. Open and run the attached invoice.exe immediately or payment will be cancelled.",
            "malicious_attachment_instruction",
        ),
    ],
)
def test_convincing_phishing_and_payment_diversion_require_review(subject, body, signal):
    assessment = assess_inbound_email_risk(subject, body)
    assert assessment.requires_human_review
    assert assessment.signal == signal
    assert assessment.reason
    assert customer_requires_human_approval(subject, body)


def test_fraud_detection_does_not_treat_isolated_sensitive_words_as_suspicious():
    for text in (
        "scam",
        "fraud",
        "payment",
        "bank",
        "password",
        "The bank shows my payment as pending; is that normal?",
    ):
        assert not assess_inbound_email_risk("Question", text).requires_human_review


def test_incoming_report_of_an_explicit_credential_theft_message_is_reviewed():
    assessment = assess_inbound_email_risk(
        "Is this email a scam?",
        "It tells me to reply with my password and OTP to restore my account.",
    )
    assert assessment.requires_human_review
    assert assessment.signal == "credential_disclosure_request"
