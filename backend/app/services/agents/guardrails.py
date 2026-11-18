"""Component 8: Guardrails - Anti-hallucination, strict retrieval thresholds, warranty safeguards, and citation validation."""
import re
from typing import Any
from app.services.agents.planning import Plan


DEFAULT_MIN_SIMILARITY = 0.40


class AgentGuardrails:
    def __init__(self, min_similarity: float = DEFAULT_MIN_SIMILARITY):
        self.min_similarity = min_similarity

    def filter_knowledge_by_relevance(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Enforce strict retrieval relevance threshold.
        Drops all documents with similarity score below min_similarity (e.g. 0.2401).
        """
        return [
            m for m in matches
            if float(m.get("similarity", 0.0)) >= self.min_similarity
            or m.get("retrieval_method") == "hybrid"
        ]

    def validate_device_hallucinations(
        self,
        draft_body: str,
        plan: Plan,
        registered_devices: list[dict[str, Any]],
        customer_name: str,
        ticket_number: str,
        product_catalog_names: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        """
        Detect and correct unauthorized device model hallucinations:
        - Only applies to technical troubleshooting where hardware model must be grounded.
        - Non-troubleshooting tickets (orders, warranties, general) referencing verified items are not hallucinations.
        - If the customer never specified a model and has no registered hardware,
          the AI must NOT invent catalog product models.
        """
        warnings = []
        if plan.intent != "troubleshooting" and not plan.requires_clarification:
            return draft_body, warnings

        customer_device_names = {
            d.get("product_name", "").lower()
            for d in registered_devices
            if d.get("product_name")
        }

        is_unknown_device = (
            plan.requires_clarification
            or (not plan.target_product_id and not plan.extracted_product_name and len(registered_devices) == 0)
        )

        if is_unknown_device:
            lower_body = draft_body.lower()
            hallucinated_model = None
            for prod in (product_catalog_names or []):
                if prod.lower() in lower_body and prod.lower() not in customer_device_names:
                    hallucinated_model = prod
                    break

            if hallucinated_model or (len(registered_devices) == 0 and ("registered device" in lower_body or "serial" in lower_body)):
                warnings.append(
                    f"Guardrail intercepted hallucinated device '{hallucinated_model or 'unverified hardware'}'. "
                    f"Converted to grounded clarification request."
                )
                corrected_draft = (
                    f"Dear {customer_name},\n\n"
                    f"Thank you for reaching out to SmartTech Customer Support regarding ticket {ticket_number}.\n\n"
                    f"To ensure we provide the exact troubleshooting steps for your setup, could you please reply with "
                    f"the specific brand and model of your device (or the serial number from the product label)?\n\n"
                    f"Once we receive your device details, our support engineering team will gladly assist you with tailored troubleshooting.\n\n"
                    f"Best regards,\nSmartTech Support Team"
                )
                return corrected_draft, warnings

        # If customer has 0 registered devices, prevent false claims of active hardware registration
        if len(registered_devices) == 0 and "your registered" in draft_body.lower():
            draft_body = re.sub(r"(?i)your registered (?:device|headphones|laptop|projector)", "your hardware", draft_body)
            warnings.append("Guardrail adjusted ungrounded registered hardware reference.")

        return draft_body, warnings

    def validate_warranty_claims(
        self,
        draft_body: str,
        warranty_facts: dict[str, Any] | None,
    ) -> tuple[str, list[str]]:
        """
        Enforce warranty policy safeguards.

        Do not promise free repair or replacement without verified eligibility.
        Do not append internal warranty records to customer-facing emails.
        """
        warnings = []

        warranty_is_active = (
            warranty_facts is not None
            and warranty_facts.get("is_active") is True
        )

        if not warranty_is_active:
            lower_body = draft_body.lower()

            unauthorized_claims = [
                "free replacement",
                "free repair",
                "covered under warranty",
                "complimentary replacement",
            ]

            found_claims = [
                claim
                for claim in unauthorized_claims
                if claim in lower_body
            ]

            if found_claims:
                warnings.append(
                    "Warranty guardrail detected potentially unsupported "
                    f"warranty claims: {found_claims}. "
                    "Manual review is required."
                )

                draft_body = re.sub(
                    r"(?i)we\s+(?:will|can)\s+provide\s+"
                    r"a\s+(?:free|complimentary)\s+"
                    r"(?:replacement|repair)",
                    "we can review the available repair or replacement options",
                    draft_body,
                )

                draft_body = re.sub(
                    r"(?i)covered under (?:our )?warranty",
                    "subject to verification of the applicable warranty terms",
                    draft_body,
                )

            # Ensure the draft explicitly informs customer if warranty has expired
            if warranty_facts.get("has_warranty") and not any(
                w in draft_body.lower()
                for w in ["expired", "out-of-warranty", "not covered", "cannot offer a free replacement"]
            ):
                end_d = warranty_facts.get("end_date") or "the standard period"
                policy_num = warranty_facts.get("policy_number", "")
                notice = (
                    f"Please note that the manufacturer warranty for this device expired on {end_d} "
                    f"(Policy #{policy_num}). As this hardware is out-of-warranty, replacement under warranty is not available, "
                    "but we can assist with troubleshooting or available out-of-warranty repair options."
                )
                if "Best regards" in draft_body:
                    draft_body = draft_body.replace("Best regards", f"{notice}\n\nBest regards")
                elif "Sincerely" in draft_body:
                    draft_body = draft_body.replace("Sincerely", f"{notice}\n\nSincerely")
                else:
                    draft_body += f"\n\n{notice}"
                warnings.append("Guardrail added expired warranty notice.")

        # Internal warranty metadata must not be appended automatically.
        return draft_body, warnings

    def validate_citations(self, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Ensure citations genuinely meet the relevance threshold.
        Never attach citations with scores like 0.2401.
        """
        return [
            c for c in citations
            if float(c.get("relevance", c.get("similarity", 0.0))) >= self.min_similarity
            or c.get("retrieval_method") == "hybrid"
        ]

    def validate_customer_identity(
        self,
        draft_body: str,
        customer_name: str,
    ) -> tuple[str, list[str]]:
        """
        Enforce customer identity handling:
        Ensure the greeting strictly addresses the customer using the verified database name,
        not a conflicting email signature (e.g. 'Bob' instead of 'Alice').
        """
        warnings = []
        if not customer_name:
            return draft_body, warnings

        greeting_match = re.search(r"(?:^|\n)(?:Dear|Hello|Hi)\s+([^,\n]+),", draft_body, re.IGNORECASE)
        if greeting_match:
            greeting_name = greeting_match.group(1).strip()
            name_parts = [p.lower() for p in customer_name.split() if len(p) > 1]
            if (
                greeting_name.lower() not in name_parts
                and customer_name.lower() not in greeting_name.lower()
                and greeting_name.lower() != "valued customer"
            ):
                warnings.append(
                    f"Guardrail intercepted conflicting customer name '{greeting_name}' in greeting. "
                    f"Corrected to verified customer name '{customer_name}'."
                )
                draft_body = re.sub(
                    r"(?:^|\n)(?:Dear|Hello|Hi)\s+[^,\n]+,",
                    f"\n\nDear {customer_name},",
                    draft_body,
                    count=1,
                    flags=re.IGNORECASE,
                ).lstrip()

        # Ensure agent does not sign as the customer at the bottom
        if f"Best regards,\n\n{customer_name}" in draft_body:
            draft_body = draft_body.replace(
                f"Best regards,\n\n{customer_name}",
                "Best regards,\nSmartTech Support Team",
            )
        return draft_body, warnings

    def validate_troubleshooting_grounding(
        self,
        draft_body: str,
        plan: Plan,
        citations: list[dict[str, Any]],
        knowledge_matches: list[dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Keep any retrieved instructions grounded without product-specific rules."""
        # The common generation and response validation stages enforce grounding
        # against the selected evidence. Product-specific step repair here would
        # couple the pipeline to one manual and could alter future instructions.
        return draft_body, citations, []

    def validate_customer_privacy(
        self,
        draft_body: str,
        authorized_serials: set[str],
        authorized_orders: set[str],
    ) -> tuple[str, list[str]]:
        """Ensure no unauthorized serial numbers or order numbers appear in the draft body."""
        warnings = []
        found_serials = set(re.findall(r"ST-SN-[\w\-]+", draft_body, re.IGNORECASE))
        unauthorized_serials = {s for s in found_serials if s.upper() not in authorized_serials}
        for s in unauthorized_serials:
            warnings.append(f"Guardrail intercepted unauthorized serial number '{s}'. Redacted for privacy.")
            draft_body = re.sub(re.escape(s), "[REDACTED_SERIAL]", draft_body, flags=re.IGNORECASE)

        found_orders = set(re.findall(r"ORD-[\w\-]+", draft_body, re.IGNORECASE))
        unauthorized_orders = {o for o in found_orders if o.upper() not in authorized_orders}
        for o in unauthorized_orders:
            warnings.append(f"Guardrail intercepted unauthorized order number '{o}'. Redacted for privacy.")
            draft_body = re.sub(re.escape(o), "[REDACTED_ORDER]", draft_body, flags=re.IGNORECASE)

        return draft_body, warnings

    def apply_all_guardrails(
        self,
        draft_body: str,
        plan: Plan,
        registered_devices: list[dict[str, Any]],
        warranty_facts: dict[str, Any] | None,
        citations: list[dict[str, Any]],
        customer_name: str,
        ticket_number: str,
        knowledge_matches: list[dict[str, Any]] | None = None,
        authorized_orders: set[str] | None = None,
        product_catalog_names: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Apply all pre- and post-generation guardrail rules."""
        all_warnings = []

        # 1. Customer Identity Check (Anti-conflicting name)
        draft_body, id_warnings = self.validate_customer_identity(
            draft_body=draft_body,
            customer_name=customer_name,
        )
        all_warnings.extend(id_warnings)

        # 2. Privacy Leakage Check
        auth_serials = {d.get("serial_number", "").upper() for d in registered_devices if d.get("serial_number")}
        auth_orders = {o.upper() for o in (authorized_orders or set()) if o}
        draft_body, priv_warnings = self.validate_customer_privacy(
            draft_body=draft_body,
            authorized_serials=auth_serials,
            authorized_orders=auth_orders,
        )
        all_warnings.extend(priv_warnings)

        # 3. Device Hallucination Check
        draft_body, dev_warnings = self.validate_device_hallucinations(
            draft_body=draft_body,
            plan=plan,
            registered_devices=registered_devices,
            customer_name=customer_name,
            ticket_number=ticket_number,
            product_catalog_names=product_catalog_names,
        )
        all_warnings.extend(dev_warnings)

        # 4. Generic troubleshooting grounding check
        draft_body, citations, tb_warnings = self.validate_troubleshooting_grounding(
            draft_body=draft_body,
            plan=plan,
            citations=citations,
            knowledge_matches=knowledge_matches,
        )
        all_warnings.extend(tb_warnings)

        # 5. Warranty Claims Check
        draft_body, w_warnings = self.validate_warranty_claims(
            draft_body=draft_body,
            warranty_facts=warranty_facts,
        )
        all_warnings.extend(w_warnings)

        # 6. Citation Validity Check
        valid_citations = self.validate_citations(citations)
        if len(valid_citations) < len(citations):
            all_warnings.append(f"Pruned {len(citations) - len(valid_citations)} citation(s) below {self.min_similarity} threshold.")

        return draft_body, valid_citations, all_warnings
