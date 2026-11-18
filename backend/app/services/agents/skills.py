"""Component 2: Reusable Support Skills for Customer Support Agent."""

from typing import Any

from sqlalchemy.orm import Session

from app.services.agents.tools import (
    tool_get_customer_details,
    tool_get_customer_orders,
    tool_get_device_by_serial,
    tool_get_product_specifications,
    tool_get_registered_devices,
    tool_get_warranty_details,
    tool_search_knowledge_base,
)


class SupportSkills:
    def __init__(self, db: Session):
        self.db = db

    def order_status_assistance(
        self,
        customer_id: int,
        order_number: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve factual order status and shipment tracking for a customer."""
        orders = tool_get_customer_orders(
            self.db,
            customer_id=customer_id,
        )

        if not orders:
            return {
                "skill": "order_status_assistance",
                "has_orders": False,
                "summary": (
                    "Our database confirms there are currently no recorded "
                    "orders under your customer profile."
                ),
                "orders": [],
            }

        target_order = None

        if order_number:
            for order in orders:
                if (
                    order.get("order_number", "").upper()
                    == order_number.upper()
                ):
                    target_order = order
                    break

            if not target_order:
                return {
                    "skill": "order_status_assistance",
                    "has_orders": False,
                    "order_found": False,
                    "summary": f"No order with number '{order_number}' was found under your verified customer account.",
                    "orders": [],
                }

        selected = target_order or orders[0]

        items_summary = ", ".join(
            f"{item.get('product_name')} (x{item.get('qty', 1)})"
            for item in selected.get("items", [])
        )

        carrier = selected.get("carrier") or "Carrier pending"
        tracking = selected.get("tracking_number") or "Pending assignment"

        summary = (
            f"Order #{selected.get('order_number')} is currently "
            f"'{selected.get('status')}'. "
            f"Items: {items_summary}. "
            f"Shipping Carrier: {carrier} (Tracking: {tracking})."
        )

        return {
            "skill": "order_status_assistance",
            "has_orders": True,
            "selected_order": selected,
            "summary": summary,
            "all_orders": orders,
            "orders": orders,
        }

    def device_information_lookup(
        self,
        customer_id: int,
    ) -> dict[str, Any]:
        """Look up registered devices belonging to the specified customer."""
        devices = tool_get_registered_devices(
            self.db,
            customer_id=customer_id,
        )

        if not devices:
            return {
                "skill": "device_information_lookup",
                "has_devices": False,
                "summary": (
                    "No registered hardware devices found under "
                    "this customer account."
                ),
                "devices": [],
            }

        device_lines = [
            (
                f"- {device.get('product_name')} "
                f"(SN: {device.get('serial_number')}, "
                f"Warranty: {device.get('warranty_status')})"
            )
            for device in devices
        ]

        return {
            "skill": "device_information_lookup",
            "has_devices": True,
            "summary": (
                "Registered hardware on file:\n"
                + "\n".join(device_lines)
            ),
            "devices": devices,
        }

    def warranty_assistance(
        self,
        serial_number: str,
        customer_id: int,
    ) -> dict[str, Any]:
        """
        Check warranty information only for the specified customer's device.
        """
        if customer_id is None:
            return {
                "skill": "warranty_assistance",
                "has_warranty": False,
                "summary": "The customer account must be verified before checking warranty information.",
                "details": {
                    "has_warranty": False,
                    "message": "Customer verification required.",
                },
            }

        w_facts = tool_get_warranty_details(
            self.db,
            serial_number=serial_number,
            customer_id=customer_id,
        )

        if not w_facts.get("has_warranty"):
            return {
                "skill": "warranty_assistance",
                "has_warranty": False,
                "summary": (
                    "No warranty was found for this customer and device."
                ),
                "details": w_facts,
            }

        if w_facts.get("is_active"):
            summary = (
                "Your hardware has active warranty coverage under "
                f"Policy #{w_facts.get('policy_number')}, "
                f"valid through {w_facts.get('end_date')} "
                f"({w_facts.get('coverage_type')})."
            )
        else:
            summary = (
                "The manufacturer warranty for this device expired on "
                f"{w_facts.get('end_date')} "
                f"(Policy #{w_facts.get('policy_number')}). "
                "Out-of-warranty support options remain available."
            )

        return {
            "skill": "warranty_assistance",
            "has_warranty": True,
            "is_active": w_facts.get("is_active"),
            "summary": summary,
            "details": w_facts,
        }

    def product_specification_lookup(
        self,
        sku: str | None = None,
        product_name: str | None = None,
        product_id: int | None = None,
    ) -> dict[str, Any]:
        """Retrieve verified technical specifications from the catalog."""
        specs = tool_get_product_specifications(
            self.db,
            sku=sku,
            product_name=product_name,
            product_id=product_id,
        )

        if not specs.get("found"):
            return {
                "skill": "product_specification_lookup",
                "found": False,
                "summary": (
                    "Product specifications could not be located "
                    "in the catalog."
                ),
            }

        summary = (
            f"{specs.get('name')} (SKU: {specs.get('sku')}): "
            f"{specs.get('description')} "
            f"Category: {specs.get('category')}. "
            f"MSRP: ${specs.get('price'):.2f}. "
            f"Standard Warranty: {specs.get('warranty_months')} months. "
            f"Support Status: {specs.get('support_status')}."
        )

        return {
            "skill": "product_specification_lookup",
            "found": True,
            "summary": summary,
            "specs": specs,
        }

    def troubleshooting_assistance(
        self,
        query: str,
        product_id: int | None = None,
        min_similarity: float = 0.40,
    ) -> dict[str, Any]:
        """Retrieve grounded troubleshooting instructions from approved documentation."""
        matches = tool_search_knowledge_base(
            self.db,
            query=query,
            product_id=product_id,
            min_similarity=min_similarity,
            top_k=3,
        )

        if not matches:
            return {
                "skill": "troubleshooting_assistance",
                "found_docs": False,
                "summary": (
                    "No verified troubleshooting documentation met "
                    "the relevance criteria."
                ),
                "matches": [],
            }

        top = matches[0]
        content = top.get("content", "").strip()

        return {
            "skill": "troubleshooting_assistance",
            "found_docs": True,
            "summary": (
                f"Per {top['document']}:\n{content}"
            ),
            "matches": matches,
        }

    def faq_assistance(
        self,
        query: str,
        min_similarity: float = 0.40,
    ) -> dict[str, Any]:
        """Retrieve verified policy or FAQ documentation."""
        matches = tool_search_knowledge_base(
            self.db,
            query=query,
            product_id=None,
            min_similarity=min_similarity,
            top_k=3,
        )

        if not matches:
            return {
                "skill": "faq_assistance",
                "found_docs": False,
                "summary": "No matching store policy or FAQ article found.",
                "matches": [],
            }

        return {
            "skill": "faq_assistance",
            "found_docs": True,
            "summary": matches[0]["content"],
            "matches": matches,
        }

    def customer_clarification_request(
        self,
        customer_name: str,
        ticket_number: str,
        clarification_reason: str,
    ) -> str:
        """
        Construct a courteous clarification email requesting missing information.

        Does not invent device models or claim the customer's hardware is known.
        """
        greeting = (
            f"Dear {customer_name},\n\n"
            "Thank you for reaching out to SmartTech Customer Support "
            f"regarding ticket {ticket_number}.\n\n"
        )

        body = (
            "To ensure we provide the exact troubleshooting steps for your "
            "setup, could you please reply with the specific brand and model "
            "of your device (or the hardware serial number located on the "
            "device label)?\n\n"
            "Once we have your device model, our support team will provide "
            "tailored technical guidance right away.\n\n"
            "Best regards,\n"
            "SmartTech Support Team"
        )

        return greeting + body

    def customer_privacy_refusal(
        self,
        customer_name: str,
        ticket_number: str,
    ) -> str:
        """
        Construct a grounded, courteous privacy refusal email.
        Strictly enforces that third-party customer information cannot be disclosed.
        """
        greeting = (
            f"Dear {customer_name},\n\n"
            "Thank you for contacting SmartTech Customer Support "
            f"regarding ticket {ticket_number}.\n\n"
        )
        body = (
            "For security and privacy reasons, we can only provide information regarding "
            "accounts, orders, hardware devices, and warranty policies that you are "
            "authorized to access under your verified customer profile.\n\n"
            "We cannot disclose account details, order numbers, device serial numbers, "
            "purchase history, or warranty records belonging to other customers.\n\n"
            "If you have questions regarding your own SmartTech products, orders, or "
            "warranty coverage, please let us know and we will be glad to assist you.\n\n"
            "Best regards,\n"
            "SmartTech Support Team"
        )
        return greeting + body

    def unverified_account_assistance(
        self,
        sender_email: str,
        ticket_number: str,
        request_type: str = "order or warranty records",
    ) -> str:
        """
        Polite notice explaining that the sender's email is not associated with an existing account
        and requires verification before sensitive orders or warranty records can be accessed.
        """
        greeting = (
            "Dear Valued Customer,\n\n"
            "Thank you for reaching out to SmartTech Customer Support "
            f"regarding ticket {ticket_number}.\n\n"
        )
        body = (
            f"Our records do not show an existing customer profile or registered purchase history "
            f"associated with the email address '{sender_email}'.\n\n"
            f"To protect customer privacy and access {request_type}, please reply from "
            f"the email address used during your purchase, or provide your order confirmation number "
            f"and billing address for verification.\n\n"
            "If you are inquiring about general product specifications or troubleshooting guidance, "
            "we are glad to provide that information right away.\n\n"
            "Best regards,\n"
            "SmartTech Support Team"
        )
        return greeting + body
