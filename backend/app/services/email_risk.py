"""Deterministic, intent-based detection for inbound phishing and fraud risk."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EmailRiskAssessment:
    requires_human_review: bool
    reason: str | None = None
    signal: str | None = None


_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_SHORTENER_OR_DECEPTIVE_URL = re.compile(
    r"\b(?:bit\.ly|tinyurl\.com|t\.co|cutt\.ly|is\.gd|ow\.ly|"
    r"xn--[a-z0-9-]+)\b|https?://\d{1,3}(?:\.\d{1,3}){3}",
    re.I,
)
_CREDENTIALS = r"(?:passwords?|passcodes?|credentials?|one[- ]time codes?|otp|verification codes?|authentication tokens?|security codes?)"
_DIRECT_CREDENTIAL_REQUEST = re.compile(
    rf"\b(?:reply\s+with|send(?:\s+me)?|share|text|provide|give|disclose)\b"
    rf".{{0,70}}\b{_CREDENTIALS}\b",
    re.I | re.S,
)
_LINK_CREDENTIAL_HARVEST = re.compile(
    rf"\b(?:click|open|visit|follow|use)\b.{{0,70}}\b(?:link|url|website|page|portal)\b"
    rf".{{0,100}}\b(?:enter|submit|verify|confirm|provide)\b.{{0,50}}\b{_CREDENTIALS}\b|"
    rf"\b(?:verify|restore|unlock|secure|confirm)\b.{{0,55}}\b(?:account|mailbox|profile)\b"
    rf".{{0,70}}\b(?:link|url|website|page)\b.{{0,70}}\b{_CREDENTIALS}\b",
    re.I | re.S,
)
_ACCOUNT_THREAT_OR_URGENCY = re.compile(
    r"\b(?:urgent(?:ly)?|immediately|within\s+\d+\s*(?:minutes?|hours?|days?)|"
    r"account\s+(?:will\s+be|is\s+being)\s+(?:suspended|closed|locked)|"
    r"mailbox\s+(?:will\s+be|is\s+being)\s+(?:disabled|closed)|"
    r"avoid\s+(?:suspension|termination|closure))\b",
    re.I,
)
_BANK_CHANGE_REQUEST = re.compile(
    r"\b(?:our|the|vendor|supplier|company)\s+(?:bank|payment|wire)\s+"
    r"(?:account|details|instructions)\s+(?:has|have|was|were)\s+(?:changed|updated)|"
    r"\b(?:update|change|replace|redirect)\b.{0,70}\b"
    r"(?:our|the|vendor|supplier)?\s*(?:new|different|alternate)?\s*"
    r"(?:bank\s+account|bank\s+details|payment\s+instructions|beneficiary|iban|routing number)\b"
    r".{0,90}\b(?:invoice|payment|wire|transfer|funds|account)\b|"
    r"\b(?:we|i|our company|the vendor|the supplier)\s+(?:have\s+)?"
    r"(?:changed|updated|replaced)\s+(?:our\s+|the\s+)?bank\s+account\b"
    r".{0,120}\b(?:redirect|invoice|wire|transfer|funds|payment)\b",
    re.I | re.S,
)
_SUSPICIOUS_TRANSFER_CHANNEL = re.compile(
    r"\b(?:gift\s+cards?|prepaid\s+cards?|bitcoin|cryptocurrency|crypto\s+wallet|"
    r"personal\s+account|western\s+union|moneygram|cash\s*app)\b",
    re.I,
)
_MONEY_TRANSFER_ACTION = re.compile(
    r"\b(?:wire|transfer|send|pay|move)\b.{0,75}\b(?:money|funds|payment|cash|"
    r"gift\s+cards?|bitcoin|crypto|wallet|account)\b|"
    r"\b(?:money|funds|payment)\b.{0,55}\b(?:wire|transfer|send|pay)\b",
    re.I | re.S,
)
_IMPERSONATION = re.compile(
    r"\b(?:i am|this is|writing on behalf of|your)\s+(?:the\s+)?"
    r"(?:ceo|cfo|chief executive|chief financial|president|owner|"
    r"bank manager|supplier|vendor|payroll|it administrator|security team|support team)\b",
    re.I,
)
_MALICIOUS_ATTACHMENT_INSTRUCTION = re.compile(
    r"\b(?:open|run|install|extract|enable|execute|disable\s+(?:your\s+)?antivirus)\b"
    r".{0,90}\b(?:attachment|attached|\.exe|\.scr|\.js|\.iso|macro|zip\s+file)\b",
    re.I | re.S,
)


def assess_inbound_email_risk(subject: str = "", message: str = "") -> EmailRiskAssessment:
    """Flag concrete credential theft, payment diversion, or malicious payload behavior.

    Risk is tied to a requested harmful action and its target, not isolated
    vocabulary such as "payment", "bank", "password", "fraud", or "scam".
    """

    text = f"{subject or ''}\n{message or ''}"
    has_link = bool(_URL.search(text))
    urgent_or_threatening = bool(_ACCOUNT_THREAT_OR_URGENCY.search(text))
    impersonation = bool(_IMPERSONATION.search(text))

    if _DIRECT_CREDENTIAL_REQUEST.search(text):
        # A request to send/reply with a password, OTP, or token is itself a
        # strong credential-theft indicator; ordinary reset questions do not
        # match this action pattern.
        return EmailRiskAssessment(
            True,
            "The message asks the recipient to disclose an authentication secret.",
            "credential_disclosure_request",
        )

    if _LINK_CREDENTIAL_HARVEST.search(text) and (has_link or urgent_or_threatening or impersonation):
        return EmailRiskAssessment(
            True,
            "The message directs the recipient to a link to verify an account or enter credentials.",
            "credential_harvesting_link",
        )

    if _BANK_CHANGE_REQUEST.search(text) and (impersonation or urgent_or_threatening or _MONEY_TRANSFER_ACTION.search(text)):
        return EmailRiskAssessment(
            True,
            "The message requests a bank or payment destination change tied to an invoice or transfer.",
            "payment_destination_change",
        )

    if _MONEY_TRANSFER_ACTION.search(text) and _SUSPICIOUS_TRANSFER_CHANNEL.search(text):
        return EmailRiskAssessment(
            True,
            "The message directs funds through a high-risk or atypical transfer channel.",
            "suspicious_transfer_channel",
        )

    if _MALICIOUS_ATTACHMENT_INSTRUCTION.search(text) and (urgent_or_threatening or impersonation):
        return EmailRiskAssessment(
            True,
            "The message urges execution of an attachment or disabling endpoint protection.",
            "malicious_attachment_instruction",
        )

    if _SHORTENER_OR_DECEPTIVE_URL.search(text) and (urgent_or_threatening or impersonation):
        return EmailRiskAssessment(
            True,
            "The message combines a deceptive or shortened link with urgency or impersonation.",
            "deceptive_link_with_pressure",
        )

    # Mentioning a suspected scam, asking whether an email is legitimate, or
    # discussing ordinary account/payment issues is not itself suspicious.
    return EmailRiskAssessment(False)
