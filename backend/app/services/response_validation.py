"""Deterministic checks for generated customer-facing email responses."""

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeDocument
from app.models.product import Product
from app.models.customer import Customer
from app.services.rag.retrieval import RetrievedContext


# ---------------------------------------------------------------------------
# REGEX PATTERNS
# ---------------------------------------------------------------------------

_EMAIL = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.I,
)

_PHONE = re.compile(
    r"(?<!\w)(?:\+?\d[\d ().-]{7,}\d)(?!\w)"
)

_NUMBER = re.compile(
    r"\b\d+(?:\.\d+)?\b"
)

# Matches numeric values with optional technical units.
# Examples:
# 5 V, 2A, 20-25°C, 500 GB, 120 Hz, 10 minutes.
_NUMERIC_UNIT = re.compile(
    r"(?<![\w.])"
    r"(?P<number>\d+(?:\.\d+)?)"
    r"\s*"
    r"(?P<unit>"
    r"degrees?\s*[CF]|"
    r"°\s*[CF]|"
    r"mAh|"
    r"kWh|"
    r"GHz|MHz|kHz|"
    r"GB|MB|TB|"
    r"mm|cm|km|"
    r"kg|mg|"
    r"ml|"
    r"Ah|Wh|"
    r"mA|"
    r"Hz|"
    r"dB|"
    r"seconds?|secs?|"
    r"minutes?|mins?|"
    r"hours?|hrs?|"
    r"days?|years?|"
    r"V|W|A|"
    r"g|m|l|"
    r"%"
    r")?"
    # Permit sentence punctuation after a factual value (for example,
    # ``60Hz.``), while still preventing partial matches inside identifiers
    # and decimals such as ``120`` or ``60.5``.
    r"(?!\w|\.\d)",
    re.I,
)

_RESOLUTION_PAIR = re.compile(
    r"(?<!\w)(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)(?!\w)",
    re.I,
)

_CITATION_PAGE = re.compile(
    r"\bpage\s+\d+\b",
    re.I,
)

_TECHNICAL_INSTRUCTION = re.compile(
    r"\b(?:press|hold|disconnect|reconnect|unplug|plug in|restart|reset|"
    r"install|update|open|click|select|turn on|turn off|replace|charge)\b",
    re.I,
)

_ALTERNATIVE_TECHNICAL_ADVICE = re.compile(
    r"\b(?:try|test|connect|use|swap|replace)\b.{0,90}\b"
    r"(?:different|another|alternate|older|certified|new)\b.{0,60}\b"
    r"(?:cable|port|source|device|adapter|input)\b",
    re.I | re.S,
)

_UNSUPPORTED_UI_OR_STATUS_ACTION = re.compile(
    r"\b(?:press|open|exit|save|wait)\b.{0,60}\b"
    r"(?:menu button|display settings|status led|menu)\b",
    re.I | re.S,
)

_UNSUPPORTED_SUPPORT_PROMISE = re.compile(
    r"\b(?:forward|escalate|arrange|schedule|book|initiate|look into)\b"
    r".{0,80}\b(?:repair|technical team|technician|diagnostic|service|replacement)\b",
    re.I | re.S,
)

_UNSUPPORTED_DIAGNOSTIC_ADVICE = re.compile(
    r"\b(?:check|inspect|test|troubleshoot|investigate|look into)\b"
    r".{0,80}\b(?:cable|firmware|port|source device|hardware|repair|diagnostic)\b",
    re.I | re.S,
)

_UNSUPPORTED_CAUSAL_CLAIM = re.compile(
    r"\b(?:this|these steps|that|it)\b.{0,90}\b"
    r"(?:can|will|should|may|forces?)\s+"
    r"(?:resolve|fix|prevent|force|ensure|cause|eliminate|guarantee)\b",
    re.I | re.S,
)

_UNSUPPORTED_OPERATIONAL_OUTCOME = re.compile(
    r"\b(?:projector|device|laptop|system|it|this)\b.{0,60}\b"
    r"(?:will|won't|cannot|can't|should|may)\b.{0,40}\b"
    r"(?:display|show|detect|connect|work|resolve|fix|fail|accept|reject)\b",
    re.I | re.S,
)

_WARRANTY_CLAIM = re.compile(
    r"\b(?:covered|eligible|warranty will|warranty covers|free replacement|"
    r"repair will|repair takes|replace(?:ment)? approved)\b",
    re.I,
)

_CITATION_INTRO = re.compile(
    r"\b(?:source|citation)\s*:",
    re.I,
)


# ---------------------------------------------------------------------------
# INSTRUCTION SYNONYMS
# ---------------------------------------------------------------------------

INSTRUCTION_SYNONYMS = {
    "restart": {
        "restart", "reboot", "power cycle", "power-cycle",
        "power off", "power on", "reset", "power",
    },
    "reset": {
        "reset", "restore", "restart", "reboot",
        "re-initialize", "power cycle", "default",
    },
    "disconnect": {
        "disconnect", "unplug", "detach", "remove",
        "separate", "cable", "power",
    },
    "reconnect": {
        "reconnect", "plug in", "plug", "attach",
        "connect", "cable", "insert",
    },
    "unplug": {
        "unplug", "disconnect", "detach", "remove",
        "cable", "power",
    },
    "plug in": {
        "plug in", "connect", "reconnect",
        "attach", "insert", "cable",
    },
    "turn on": {
        "turn on", "power on", "switch on",
        "enable", "start", "boot", "on",
    },
    "turn off": {
        "turn off", "power off", "shut down",
        "shutdown", "switch off", "disable", "off",
    },
    "press": {
        "press", "hold", "push", "tap",
        "click", "depress", "button",
    },
    "hold": {
        "hold", "press and hold", "press",
        "keep pressed", "seconds",
    },
    "install": {
        "install", "setup", "set up",
        "download", "load", "update", "driver",
    },
    "update": {
        "update", "upgrade", "refresh",
        "install", "firmware", "patch", "version",
    },
    "open": {
        "open", "launch", "navigate",
        "access", "go to", "select", "menu", "settings",
    },
    "click": {
        "click", "tap", "press",
        "select", "choose", "button",
    },
    "select": {
        "select", "choose", "pick", "click",
        "set", "navigate", "menu", "option",
    },
    "replace": {
        "replace", "swap", "change",
        "substitute", "battery", "part",
    },
    "charge": {
        "charge", "charging", "battery",
        "power", "plug in", "adapter", "cable",
    },
}


# ---------------------------------------------------------------------------
# COMPANY EMAIL ADDRESSES
# ---------------------------------------------------------------------------

COMPANY_EMAILS = {
    "support@smarttech.com",
    "help@smarttech.com",
    "contact@smarttech.com",
    "service@smarttech.com",
    "info@smarttech.com",
}


# ---------------------------------------------------------------------------
# CITATION HELPERS
# ---------------------------------------------------------------------------

def _citation_to_str(c: str | dict) -> str:
    if isinstance(c, dict):
        return (
            c.get("citation")
            or c.get("document")
            or c.get("filename")
            or str(c)
        )

    return str(c)


# ---------------------------------------------------------------------------
# NUMERIC VALIDATION HELPERS
# ---------------------------------------------------------------------------

def _normalize_number(number: str) -> str:
    """
    Normalize numeric values without losing significant digits.

    Examples:
        20     -> 20
        20.0   -> 20
        5.00   -> 5
        120.50 -> 120.5

    Decimal is used instead of float to avoid incorrect normalization.
    """
    try:
        value = Decimal(number)

        if not value.is_finite():
            return number

        return format(value.normalize(), "f")

    except (InvalidOperation, ValueError):
        return number


def _normalize_unit(unit: str | None) -> str:
    """
    Normalize unit formatting.

    Examples:
        ' V'  -> 'v'
        '° C' -> '°c'
        'GB'  -> 'gb'
    """
    if not unit:
        return ""

    normalized = re.sub(r"\s+", "", unit)
    return normalized.casefold()


def _extract_factual_numbers(text: str) -> list[str]:
    """
    Extract factual numeric claims while ignoring list numbering.

    Examples:
        '1. Check the cable' -> ignored
        'Step 2: Restart' -> ignored
        '5 V' -> 5v
        '2 A' -> 2a
        '20-25°C' -> 20, 25°c
        '120 GB' -> 120gb

    Single-digit numbers are ignored only when they have no unit.
    """

    if not text:
        return []

    # Treat common resolution notation as two numeric facts so 3840x2160
    # is checked against both dimensions rather than ignored as one token.
    cleaned = _RESOLUTION_PAIR.sub(r"\1 \2", text)

    # Remove line-leading enumeration bullets.
    cleaned = re.sub(
        r"(?m)^\s*\d+[\.\)]\s+",
        " ",
        cleaned,
    )

    # Remove parenthesized list numbers such as (1), (2).
    cleaned = re.sub(
        r"\(\d+\)",
        " ",
        cleaned,
    )

    # Remove common step/item labels.
    cleaned = re.sub(
        r"(?i)\b(?:step|option|method|point|item|tip|section|phase|rule|"
        r"level|part|no\.?|#)\s*\d+\b",
        " ",
        cleaned,
    )

    factual = []

    for match in _NUMERIC_UNIT.finditer(cleaned):
        number = match.group("number")
        unit = match.group("unit")

        # Ignore single-digit numbers only when they are unitless.
        # This preserves specifications such as 5 V and 2 A.
        if len(number) == 1 and number.isdigit() and not unit:
            continue

        normalized_number = _normalize_number(number)
        normalized_unit = _normalize_unit(unit)

        factual.append(normalized_number + normalized_unit)

    return factual


# ---------------------------------------------------------------------------
# CUSTOMER IDENTITY VALIDATION
# ---------------------------------------------------------------------------

def validate_draft_customer_identity(
    db: Session,
    ticket,
    recipient: str,
    body: str,
) -> str | None:
    """
    Require recipient and customer record to match the original sender.

    Reject another customer's email address or name in the response.
    """

    customer = ticket.customer if ticket else None

    if (
        re.match(r'(?s)^\s*\{\s*"subject"\s*:', body or "")
        or re.search(r"(?im)^\s*Sources\s*:\s*$", body or "")
    ):
        return "Draft body is not clean customer-facing email text."

    expected = (
        (ticket.sender_email or "").strip().casefold()
        if ticket
        else ""
    )

    recipient = (recipient or "").strip().casefold()

    if (
        not customer
        or not expected
        or customer.email.strip().casefold() != expected
        or recipient != expected
    ):
        return (
            "Recipient is not verified against the customer record "
            "for this incoming sender."
        )

    # Prevent disclosure of another customer's identity.
    for other in db.scalars(
        select(Customer).where(Customer.id != customer.id)
    ).all():

        other_email = (other.email or "").strip()

        if other_email and re.search(
            r"(?<![\w.+-])"
            + re.escape(other_email)
            + r"(?![\w.-])",
            body or "",
            re.I,
        ):
            return "Response contains another customer's email address."

        name = (other.full_name or "").strip()

        if name and name.casefold() != "customer" and len(name) >= 4:

            if re.search(
                r"(?<!\w)"
                + re.escape(name)
                + r"(?!\w)",
                body or "",
                re.I,
            ):
                return "Response contains another customer's name."

    return None


# ---------------------------------------------------------------------------
# GENERATED RESPONSE VALIDATION
# ---------------------------------------------------------------------------

def validate_generated_response(
    *,
    body: str,
    inquiry: str,
    context: RetrievedContext,
    expected_product_id: int | None,
    db: Session,
) -> str | None:
    """
    Return a concrete rejection reason when generated text is unsafe to send.

    This function validates:
        1. Empty response
        2. Unsupported product names
        3. Unsupported citations and page numbers
        4. Unsupported emails and phone numbers
        5. Product-document association
        6. Warranty and repair claims
        7. Technical instructions
        8. Unsupported numerical claims
        9. Retrieved document references
    """

    if not isinstance(body, str) or not body.strip():
        return "Response body is empty."

    evidence = "\n".join(
        result.content for result in context.results
    )

    citation_strs = [
        _citation_to_str(c)
        for c in (context.citations or [])
    ]

    sources = "\n".join(citation_strs)

    allowed_text = f"{evidence}\n{sources}\n{inquiry}"

    # Reject common additions that look plausible but are not safe to infer
    # from a related product manual: alternate hardware tests, undocumented UI
    # or status-indicator procedures, and promises of service actions.
    for pattern, reason in (
        (_ALTERNATIVE_TECHNICAL_ADVICE, "an alternative hardware test"),
        (_UNSUPPORTED_UI_OR_STATUS_ACTION, "a UI or status-indicator action"),
        (_UNSUPPORTED_SUPPORT_PROMISE, "a support action"),
        (_UNSUPPORTED_DIAGNOSTIC_ADVICE, "an additional diagnostic step"),
        (_UNSUPPORTED_CAUSAL_CLAIM, "an unsupported technical outcome claim"),
        (_UNSUPPORTED_OPERATIONAL_OUTCOME, "an unsupported technical outcome claim"),
    ):
        for match in pattern.finditer(body):
            if match.group(0).casefold() not in allowed_text.casefold():
                return f"Response adds {reason} absent from the inquiry and retrieved evidence."

    # -----------------------------------------------------------------------
    # 1. PRODUCT NAME VALIDATION
    # -----------------------------------------------------------------------

    for product in db.scalars(select(Product)).all():

        for label in (product.name, product.sku):

            if label and re.search(
                r"(?<![a-z0-9])"
                + re.escape(label)
                + r"(?![a-z0-9])",
                body,
                re.I,
            ):

                if not re.search(
                    r"(?<![a-z0-9])"
                    + re.escape(label)
                    + r"(?![a-z0-9])",
                    allowed_text,
                    re.I,
                ):
                    return (
                        "Response identifies a catalog product that was "
                        "not requested or supported by retrieved documentation."
                    )

    # -----------------------------------------------------------------------
    # 2. CITATION VALIDATION
    # -----------------------------------------------------------------------

    # LLM-written citations are untrusted.
    # The application appends real citations.
    if (
        _CITATION_INTRO.search(body)
        and not any(
            cit.lower() in body.lower()
            for cit in citation_strs
        )
    ):
        return (
            "Response contains a citation that was not returned "
            "by knowledge retrieval."
        )

    for page in _CITATION_PAGE.findall(body):

        if (
            page.lower() not in sources.lower()
            and page.lower() not in evidence.lower()
        ):
            return (
                "Response cites a page number that is absent "
                "from the retrieved source metadata."
            )

    # -----------------------------------------------------------------------
    # 3. EMAIL AND PHONE VALIDATION
    # -----------------------------------------------------------------------

    for pattern, label in (
        (_EMAIL, "email address"),
        (_PHONE, "phone number"),
    ):

        for value in pattern.findall(body):

            val_lower = value.casefold()

            if (
                val_lower not in allowed_text.casefold()
                and val_lower not in COMPANY_EMAILS
            ):
                return f"Response contains an unsupported {label}."

    # -----------------------------------------------------------------------
    # 4. PRODUCT-DOCUMENT ASSOCIATION VALIDATION
    # -----------------------------------------------------------------------

    if expected_product_id is not None:

        product = db.get(Product, expected_product_id)

        if not product:
            return "Identified product no longer exists in the catalog."

        linked_doc_ids = set(
            db.scalars(
                select(KnowledgeDocument.id).where(
                    KnowledgeDocument.is_indexed.is_(True),
                    KnowledgeDocument.product_id == product.id,
                )
            ).all()
        )

        retrieved_doc_ids = {
            result.document_id
            for result in context.results
        }

        if (
            context.results
            and not (retrieved_doc_ids & linked_doc_ids)
        ):

            # Product-tagged results are mandatory when indexed manuals
            # are associated with the identified product.
            if linked_doc_ids:
                return (
                    "Retrieved documentation does not belong "
                    "to the identified product."
                )

    # -----------------------------------------------------------------------
    # 5. WARRANTY AND REPAIR CLAIM VALIDATION
    # -----------------------------------------------------------------------

    if (
        _WARRANTY_CLAIM.search(body)
        and not re.search(
            r"\b(?:warranty|covered|eligible|repair|replacement)\b",
            evidence,
            re.I,
        )
    ):
        return (
            "Response makes a warranty or repair claim "
            "without supporting documentation."
        )

    # -----------------------------------------------------------------------
    # 6. TECHNICAL INSTRUCTION VALIDATION
    # -----------------------------------------------------------------------

    if (
        _TECHNICAL_INSTRUCTION.search(body)
        and not context.results
    ):
        return (
            "Response gives technical instructions "
            "without retrieved documentation."
        )

    if context.results:

        evidence_lower = evidence.casefold()
        inquiry_lower = inquiry.casefold()

        # ---------------------------------------------------------------
        # 6A. NUMERIC CLAIM VALIDATION
        # ---------------------------------------------------------------

        # Build exact normalized numeric evidence.
        # This prevents incorrect substring matches:
        # 20 must NOT match 120.
        supported_numbers = set(
            _extract_factual_numbers(evidence)
            + _extract_factual_numbers(inquiry)
        )

        for number in _extract_factual_numbers(body):

            if number not in supported_numbers:
                return (
                    f"Response includes unsupported numeric detail "
                    f"'{number}'."
                )

        # ---------------------------------------------------------------
        # 6B. TECHNICAL INSTRUCTION VALIDATION
        # ---------------------------------------------------------------

        for verb in _TECHNICAL_INSTRUCTION.findall(body):

            v_lower = verb.casefold()

            if (
                v_lower not in evidence_lower
                and v_lower not in inquiry_lower
            ):

                synonyms = INSTRUCTION_SYNONYMS.get(
                    v_lower,
                    {v_lower},
                )

                if not any(
                    syn in evidence_lower or syn in inquiry_lower
                    for syn in synonyms
                ):
                    return (
                        f"Response includes an instruction "
                        f"('{verb}') absent from the retrieved documentation."
                    )

    # -----------------------------------------------------------------------
    # 7. DOCUMENT NAME VALIDATION
    # -----------------------------------------------------------------------

    known_sources = " ".join(
        citation_strs
    ).casefold()

    for doc in db.scalars(
        select(KnowledgeDocument).where(
            KnowledgeDocument.is_indexed.is_(True)
        )
    ).all():

        for label in (doc.title, doc.filename):

            if (
                label
                and label.casefold() in body.casefold()
                and label.casefold() not in known_sources
            ):
                return (
                    "Response names a document that was not retrieved "
                    "for this request."
                )

    # -----------------------------------------------------------------------
    # ALL CHECKS PASSED
    # -----------------------------------------------------------------------

    return None
