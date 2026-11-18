"""Component 1: Planning - Intent recognition, entity extraction, and required tool/skill planning."""
from dataclasses import dataclass, field
import re
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.models.device import Device
from app.models.order import Order
from app.models.product import Product
from app.models.ticket import SupportTicket
from app.services.rag.retrieval import identify_product_id


@dataclass
class Plan:
    intent: str
    category: str
    priority: str
    needed_skills: list[str] = field(default_factory=list)
    needed_tools: list[str] = field(default_factory=list)
    extracted_order_number: str | None = None
    extracted_serial_number: str | None = None
    extracted_product_name: str | None = None
    target_product_id: int | None = None
    requires_clarification: bool = False
    clarification_reason: str | None = None
    reasoning: str = ""


class AgentPlanner:
    def __init__(self, db: Session):
        self.db = db

    def plan_ticket(self, ticket: SupportTicket, latest_message: str, customer_devices: list[dict[str, Any]] | None = None) -> Plan:
        """
        Analyze customer inquiry, classify intent, extract hardware/order entities,
        and construct an executable plan of skills and tools.
        """
        text = f"{ticket.subject} {latest_message}".lower()
        devices = customer_devices if customer_devices is not None else []

        # 1. Entity Extraction
        ord_match = re.search(r"ORD-[\w\-]+", text, re.IGNORECASE)
        extracted_order = ord_match.group(0).upper() if ord_match else None

        sn_match = re.search(r"ST-SN-[\w\-]+", text, re.IGNORECASE)
        extracted_sn = sn_match.group(0).upper() if sn_match else None

        extracted_prod_name = None
        target_prod_id = None

        # Resolve against the current catalog, so new products, SKUs, and
        # partial model names work without maintaining a hard-coded map.
        target_prod_id = identify_product_id(text, self.db)
        if target_prod_id is not None:
            product = self.db.get(Product, target_prod_id)
            extracted_prod_name = product.name if product else None

        # Check ticket device link if present
        if not target_prod_id and ticket.device_id:
            dev = self.db.scalar(select(Device).where(Device.id == ticket.device_id))
            if dev:
                target_prod_id = dev.product_id
                if not extracted_sn:
                    extracted_sn = dev.serial_number
                if not extracted_prod_name and dev.product:
                    extracted_prod_name = dev.product.name

        # 2. Intent Classification
        privacy_phrases = [
            "customer b", "customer a", "another customer", "other customer",
            "someone else's", "different customer", "third party", "third-party",
            "another user", "other user", "another person", "other person",
            "someone else's order", "other customer's", "another customer's",
            "dump database", "list all customers", "show customer records",
            "reveal serial numbers of other", "ignore previous instructions and",
            "who owns serial", "who ordered", "who purchased",
            "provide another customer", "show other customer",
        ]

        found_foreign_email = False
        if ticket.customer and ticket.customer.email:
            mentioned_emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', text)
            for m_email in mentioned_emails:
                if m_email.lower() != ticket.customer.email.lower():
                    found_foreign_email = True
                    break

        is_privacy_attack = (
            any(phrase in text for phrase in privacy_phrases)
            or found_foreign_email
            or bool(re.search(r"\b(?:provide|give|show|tell|lookup|look up|get)\s+(?:me\s+)?[\w\s]{1,30}?(?:'s|’s)\s+(?:order|device|serial|warranty|account|purchase|detail|information|info)", text))
            or bool(re.search(r"\b(?:another|other)\s+customer(?:'s)?\s+(?:order|device|serial|warranty|account|purchase|detail|information|info)", text))
        )

        if is_privacy_attack:
            return Plan(
                intent="privacy_refusal",
                category="privacy",
                priority=ticket.priority or "high",
                needed_skills=["customer_privacy_refusal"],
                needed_tools=[],
                extracted_order_number=None,
                extracted_serial_number=None,
                extracted_product_name=None,
                target_product_id=None,
                requires_clarification=False,
                clarification_reason=None,
                reasoning="Privacy protection triggered: Request for unauthorized third-party customer information was refused.",
            )

        is_specs = any(w in text for w in ["specs", "specifications", "features", "dimension", "weight", "battery life", "price", "how much is"])
        is_troubleshooting = any(w in text for w in [
            "power", "not powering on", "won't turn on", "not working", "broken", "troubleshoot",
            "reset", "pair", "pairing", "flicker", "audio", "noise", "error", "firmware",
            "stalled", "stuck", "failing", "crash", "bug", "connect", "connection", "bluetooth",
            "update", "screen", "display", "unresponsive", "manual", "setup", "instructions",
            "malfunction", "turn off", "not charging", "battery dead", "battery draining", "glitch"
        ]) and not is_specs
        is_policy_faq = any(w in text for w in ["refund", "return", "policy", "days return", "cancellation", "shipping policy", "faq"])
        mentions_generic_hardware = any(w in text for w in ["device", "hardware", "unit", "product", "gadget", "machine", "item"])
        is_order = any(w in text for w in ["tracking", "shipped", "shipping", "delivery", "delivered", "package", "transit", "fedex", "ups", "usps", "when will my order", "order status", "where is my order"]) or (("order" in text or extracted_order) and not is_troubleshooting)
        is_warranty = any(w in text for w in ["warranty", "coverage", "warranty claim", "extended warranty"])

        intent = "general"
        category = "general"
        priority = ticket.priority or "medium"
        needed_skills = []
        needed_tools = ["get_customer_details"]
        requires_clarification = False
        clarification_reason = None
        reasoning_steps = []

        if is_troubleshooting or (mentions_generic_hardware and not is_order and not is_policy_faq and not is_warranty and not is_specs):
            intent = "troubleshooting"
            category = "technical"
            needed_skills.append("troubleshooting_assistance")
            needed_tools.append("get_registered_devices")
            if extracted_order:
                needed_skills.append("order_status_assistance")
                needed_tools.append("get_customer_orders")
            reasoning_steps.append("Customer requires technical troubleshooting support.")

            # GUARD: Check if device model is identified or ambiguous
            if not target_prod_id and not extracted_prod_name:
                if len(devices) == 0:
                    requires_clarification = True
                    clarification_reason = "Customer is requesting troubleshooting, but no device model or serial number was provided and no registered devices are on file."
                    needed_skills = ["customer_clarification"]
                    reasoning_steps.append("Clarification required: unknown hardware model with zero registered devices.")
                elif len(devices) > 1:
                    requires_clarification = True
                    clarification_reason = f"Customer has {len(devices)} registered devices and did not specify which hardware model requires troubleshooting."
                    needed_skills = ["customer_clarification"]
                    reasoning_steps.append("Clarification required: multiple registered devices on file.")
                elif len(devices) == 1:
                    # Single registered device on file -> infer candidate device
                    single_dev = devices[0]
                    target_prod_id = single_dev.get("product_id")
                    extracted_prod_name = single_dev.get("product_name")
                    extracted_sn = single_dev.get("serial_number")
                    needed_tools.append("search_knowledge_base")
                    reasoning_steps.append(f"Inferred registered hardware: {extracted_prod_name} (SN: {extracted_sn}).")
            else:
                needed_tools.append("search_knowledge_base")

        elif is_order:
            intent = "order_status"
            category = "order"
            needed_skills.append("order_status_assistance")
            needed_tools.append("get_customer_orders")
            reasoning_steps.append("Customer inquiry pertains to orders/shipments.")

        elif is_warranty:
            intent = "warranty_check"
            category = "warranty"
            needed_skills.append("warranty_assistance")
            needed_tools.append("get_registered_devices")
            if extracted_sn:
                needed_tools.append("get_warranty_details")
            elif not target_prod_id and not extracted_prod_name:
                if len(devices) == 0:
                    requires_clarification = True
                    clarification_reason = "Customer is requesting warranty information, but no serial number or device model was provided and no registered devices are on file."
                    needed_skills = ["customer_clarification"]
                elif len(devices) > 1:
                    requires_clarification = True
                    clarification_reason = f"Customer has {len(devices)} registered devices on file and did not specify which device requires a warranty check."
                    needed_skills = ["customer_clarification"]
                elif len(devices) == 1:
                    single_dev = devices[0]
                    extracted_sn = single_dev.get("serial_number")
                    needed_tools.append("get_warranty_details")
            reasoning_steps.append("Customer inquiry pertains to product warranty status.")

        elif is_specs:
            intent = "product_specification"
            category = "technical"
            if not target_prod_id and not extracted_prod_name:
                if len(devices) == 0:
                    requires_clarification = True
                    clarification_reason = "Customer is asking about specifications, but did not specify which SmartTech product model."
                    needed_skills = ["customer_clarification"]
                elif len(devices) > 1:
                    requires_clarification = True
                    clarification_reason = f"Customer has {len(devices)} registered devices and did not specify which product model's specifications are needed."
                    needed_skills = ["customer_clarification"]
                elif len(devices) == 1:
                    single_dev = devices[0]
                    target_prod_id = single_dev.get("product_id")
                    extracted_prod_name = single_dev.get("product_name")
                    needed_skills.append("product_specification_lookup")
                    needed_tools.append("get_product_specifications")
            else:
                needed_skills.append("product_specification_lookup")
                needed_tools.append("get_product_specifications")
            reasoning_steps.append("Customer is inquiring about technical specifications or product catalog details.")

        elif is_policy_faq:
            intent = "policy_faq"
            category = "general"
            needed_skills.append("faq_assistance")
            needed_tools.append("search_knowledge_base")
            reasoning_steps.append("Inquiry covers store policies, warranties, or FAQs.")

        else:
            intent = "general_support"
            category = "general"
            needed_skills.append("faq_assistance")
            needed_tools.append("search_knowledge_base")
            reasoning_steps.append("General customer support request.")

        # Priority adjustment based on urgency signals
        if any(w in text for w in ["urgent", "immediately", "critical", "broken", "danger", "smoke"]):
            priority = "urgent"
        elif any(w in text for w in ["not working", "lost package", "never arrived", "stolen", "discrepancy"]):
            priority = "high"

        return Plan(
            intent=intent,
            category=category,
            priority=priority,
            needed_skills=needed_skills,
            needed_tools=needed_tools,
            extracted_order_number=extracted_order,
            extracted_serial_number=extracted_sn,
            extracted_product_name=extracted_prod_name,
            target_product_id=target_prod_id,
            requires_clarification=requires_clarification,
            clarification_reason=clarification_reason,
            reasoning=" | ".join(reasoning_steps),
        )
