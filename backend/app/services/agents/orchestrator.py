"""Component 3: AI Support Agent Orchestrator.
Coordinates Planning, Skills, Memory, Tools, Knowledge Retrieval, LLM
Synthesis, Guardrails, and Tracing.
"""

import json
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.logging import logger
from app.models.customer import Customer
from app.models.draft import Draft
from app.models.product import Product
from app.models.ticket import SupportTicket, TicketMessage
from app.schemas.agent import AgentRunResponse
from app.services.agents.guardrails import AgentGuardrails
from app.services.agents.memory import ConversationMemory
from app.services.agents.planning import AgentPlanner, Plan
from app.services.agents.skills import SupportSkills
from app.services.agents.tools import (
    tool_get_customer_details,
    tool_get_customer_orders,
    tool_get_device_by_serial,
    tool_get_product_specifications,
    tool_get_registered_devices,
    tool_get_warranty_details,
    tool_search_knowledge_base,
)
from app.services.agents.tracing import AgentExecutionTracer
from app.integrations.llm.llm_service import is_llm_configured
from app.services.workflow_audit import safe_workflow_error
from app.services.rag.retrieval import assemble_context_block


class AgentOrchestrator:
    def __init__(self, db: Session):
        self.db = db
        self.planner = AgentPlanner(db)
        self.skills = SupportSkills(db)
        self.memory = ConversationMemory(db)
        self.tracer = AgentExecutionTracer(db)
        self.guardrails = AgentGuardrails(min_similarity=0.40)

    def run_for_ticket(
        self,
        ticket_id: int,
        force_regenerate: bool = False,
        custom_instructions: str | None = None,
    ) -> AgentRunResponse:
        start_time = time.time()

        # 1. Load Ticket & Relationships
        ticket = self.db.scalar(
            select(SupportTicket)
            .options(
                selectinload(SupportTicket.customer),
                selectinload(SupportTicket.messages),
                selectinload(SupportTicket.device),
                selectinload(SupportTicket.order),
            )
            .where(SupportTicket.id == ticket_id)
        )

        if not ticket:
            raise ValueError(f"SupportTicket {ticket_id} not found.")

        # 2. Start Observability Trace
        exec_id, correlation_id = self.tracer.start_trace(ticket.id)
        warnings: list[str] = []

        # 3. Memory Extraction
        t0 = time.time()
        conversation_history = self.memory.get_conversation_history(ticket.id)
        thread_context = self.memory.format_thread_context_for_prompt(ticket.id)
        d_ms = int((time.time() - t0) * 1000)

        self.tracer.record_step(
            exec_id,
            "conversation_memory",
            {"ticket_id": ticket.id},
            {"messages_count": len(conversation_history)},
            duration_ms=d_ms,
        )

        # Extract latest customer message
        customer_messages = [
            message
            for message in ticket.messages
            if message.sender_type == "customer"
        ]
        latest_message = (
            customer_messages[-1].message_body
            if customer_messages
            else ticket.subject
        )

        # 4. Customer Details Lookup
        t0 = time.time()
        cust_facts = tool_get_customer_details(
            self.db,
            customer_id=ticket.customer_id,
        )
        d_ms = int((time.time() - t0) * 1000)

        self.tracer.record_step(
            exec_id,
            "get_customer_details",
            {"customer_id": ticket.customer_id},
            cust_facts,
            duration_ms=d_ms,
        )

        customer_name = cust_facts.get("name", "Valued Customer")

        # 5. Registered Devices Lookup
        t0 = time.time()
        registered_devices = tool_get_registered_devices(
            self.db,
            customer_id=ticket.customer_id,
        )
        d_ms = int((time.time() - t0) * 1000)

        self.tracer.record_step(
            exec_id,
            "get_registered_devices",
            {"customer_id": ticket.customer_id},
            {"devices_count": len(registered_devices)},
            duration_ms=d_ms,
        )

        # 6. Planning
        t0 = time.time()
        plan: Plan = self.planner.plan_ticket(
            ticket=ticket,
            latest_message=latest_message,
            customer_devices=registered_devices,
        )
        d_ms = int((time.time() - t0) * 1000)

        self.tracer.record_step(
            exec_id,
            "planning",
            {
                "subject": ticket.subject,
                "message_snippet": latest_message[:100],
            },
            {
                "intent": plan.intent,
                "category": plan.category,
                "needed_skills": plan.needed_skills,
                "requires_clarification": plan.requires_clarification,
                "reasoning": plan.reasoning,
            },
            duration_ms=d_ms,
        )

        # 7. Execute Skills & Tools
        order_skill_result = None

        if (
            "order_status_assistance" in plan.needed_skills
            or "get_customer_orders" in plan.needed_tools
        ):
            t0 = time.time()

            order_skill_result = self.skills.order_status_assistance(
                customer_id=ticket.customer_id,
                order_number=plan.extracted_order_number,
            )

            d_ms = int((time.time() - t0) * 1000)

            self.tracer.record_step(
                exec_id,
                "order_status_assistance",
                {"order_number": plan.extracted_order_number},
                {
                    "has_orders": order_skill_result.get("has_orders"),
                    "summary": order_skill_result.get("summary"),
                },
                duration_ms=d_ms,
            )

        # Warranty lookup: always enforce ticket-customer ownership.
        warranty_facts = {}
        target_sn = plan.extracted_serial_number

        if not target_sn and ticket.device and ticket.device.serial_number:
            target_sn = ticket.device.serial_number

        if not target_sn and len(registered_devices) == 1:
            target_sn = registered_devices[0].get("serial_number")

        if target_sn and ticket.customer_id is not None:
            t0 = time.time()

            warranty_skill_res = self.skills.warranty_assistance(
                serial_number=target_sn,
                customer_id=ticket.customer_id,
            )

            warranty_facts = warranty_skill_res.get("details", {})
            d_ms = int((time.time() - t0) * 1000)

            self.tracer.record_step(
                exec_id,
                "warranty_assistance",
                {
                    "customer_id": ticket.customer_id,
                    "serial_number": target_sn,
                },
                {
                    "has_warranty": warranty_skill_res.get("has_warranty"),
                    "is_active": warranty_skill_res.get("is_active"),
                    "summary": warranty_skill_res.get("summary"),
                },
                duration_ms=d_ms,
            )

        # 8. Knowledge Retrieval with Guardrails
        knowledge_matches: list[dict[str, Any]] = []

        # Run KB search for FAQ, troubleshooting, or when the plan needs it.
        # Also run for general_support, policy_faq, and warranty_check with missing device.
        should_search_kb = (
            not plan.requires_clarification
            and (
                "troubleshooting_assistance" in plan.needed_skills
                or "faq_assistance" in plan.needed_skills
                or "search_knowledge_base" in plan.needed_tools
                or plan.intent in ("general_support", "policy_faq", "warranty_check")
            )
        )

        if should_search_kb:
            t0 = time.time()
            search_query = f"{ticket.subject} {latest_message}".strip()

            raw_matches = tool_search_knowledge_base(
                self.db,
                query=search_query,
                product_id=plan.target_product_id,
                min_similarity=self.guardrails.min_similarity,
                top_k=5,
            )

            guarded_matches = self.guardrails.filter_knowledge_by_relevance(
                raw_matches
            )

            # Only filter cross-product KB results when we know the SPECIFIC target product.
            # For general FAQs, policy questions, or warranty policy queries (no product_id),
            # allow ALL relevant results â€” do NOT strip product-named documents.
            if plan.target_product_id is not None:
                # We have a specific product: remove results from other products' documents
                # by checking if the query product name is mentioned in the document.
                # (The search already uses product_id filter in the DB, so this is a
                # secondary content-level guard for cross-contamination.)
                pass  # DB-level product_id filter already handles this correctly

            knowledge_matches = guarded_matches
            d_ms = int((time.time() - t0) * 1000)

            self.tracer.record_step(
                exec_id,
                "search_knowledge_base",
                {
                    "query": search_query[:100],
                    "product_id": plan.target_product_id,
                    "min_similarity": self.guardrails.min_similarity,
                },
                {
                    "raw_matches_count": len(raw_matches),
                    "guarded_matches_count": len(knowledge_matches),
                        "selected_chunks": [
                            {
                                "chunk_id": item.get("chunk_id"),
                                "document_id": item.get("document_id"),
                                "document": item.get("document"),
                                "similarity": item.get("similarity"),
                                "retrieval_method": item.get("retrieval_method"),
                                "citation": item.get("citation"),
                            }
                            for item in knowledge_matches
                        ],
                },
                duration_ms=d_ms,
            )

        # 9. Escalation & Safety Analysis
        escalation_recommended = False
        lower_msg = f"{ticket.subject} {latest_message}".lower()

        if any(
            term in lower_msg
            for term in [
                "lost",
                "never received",
                "not received",
                "no package",
                "missing",
                "stolen",
                "discrepancy",
            ]
        ):
            escalation_recommended = True
            warnings.append(
                "Package delivery discrepancy detected. "
                "Escalation to manager required per shipping policy."
            )

        if warranty_facts and not warranty_facts.get("is_active", True):
            if any(
                term in lower_msg
                for term in ["free", "replacement", "warranty"]
            ):
                escalation_recommended = True
                warnings.append(
                    "Customer requesting warranty service on an expired "
                    "device. Manager approval needed."
                )

        if (
            not knowledge_matches
            and not plan.requires_clarification
            and ("how" in lower_msg or "error" in lower_msg)
        ):
            warnings.append(
                "No directly matching knowledge documentation met the "
                "relevance criteria. Manual review advised."
            )

        # 10. Response Synthesis
        confidence = 0.90
        generation_mode = "deterministic_grounded"

        citations_list = [
            {
                "document": item["document"],
                "citation": item["citation"],
                "relevance": item["similarity"],
            }
            for item in knowledge_matches
        ]

        if plan.intent == "privacy_refusal":
            draft_body = self.skills.customer_privacy_refusal(
                customer_name=customer_name,
                ticket_number=ticket.ticket_number,
            )
            citations_list = []
            confidence = 0.99
            reasoning = "Customer privacy refusal: Denied request for unauthorized third-party customer information."

        elif not ticket.customer_id and plan.intent in ("order_status", "warranty_inquiry"):
            draft_body = self.skills.unverified_account_assistance(
                sender_email=ticket.sender_email or (ticket.customer.email if ticket.customer else "customer"),
                ticket_number=ticket.ticket_number,
            )
            citations_list = []
            confidence = 0.95
            reasoning = "Unverified sender identity: Identity verification required before disclosing private account records."

        elif plan.requires_clarification:
            draft_body = self.skills.customer_clarification_request(
                customer_name=customer_name,
                ticket_number=ticket.ticket_number,
                clarification_reason=(
                    plan.clarification_reason or "Unknown device model"
                ),
            )

            citations_list = []
            confidence = 0.95
            reasoning = (
                f"Clarification requested: {plan.clarification_reason}"
            )

        elif is_llm_configured():
            try:
                from app.services.agents.langchain_tools import ToolExecutionContext
                from app.services.agents.react_agent import run_react_agent_for_ticket

                react_context = ToolExecutionContext(
                    db=self.db,
                    ticket=ticket,
                    tracer=self.tracer,
                    execution_id=exec_id,
                    target_product_id=plan.target_product_id,
                )

                llm_start = time.perf_counter()
                agent_result = run_react_agent_for_ticket(
                    context=react_context,
                    subject=ticket.subject,
                    latest_message=latest_message,
                    thread_context=thread_context,
                    planning_intent=plan.intent,
                )
                llm_elapsed = time.perf_counter() - llm_start
                logger.info(
                    "LangChain ReAct Agent response time: %.2f seconds",
                    llm_elapsed,
                )

                draft_body = agent_result.draft_body
                generation_mode = "langchain_react"
                import re
                draft_body = re.sub(
                    r"(?im)^\s*Subject\s*:\s*.*(?:\r?\n|$)",
                    "",
                    draft_body,
                    count=1,
                ).lstrip()
                confidence = float(agent_result.confidence_score)
                reasoning = agent_result.reasoning_summary

                if agent_result.escalation_recommended:
                    escalation_recommended = True

                # Merge any citations gathered by the ReAct agent
                if react_context.citations:
                    for cit in react_context.citations:
                        if not any(c.get("citation") == cit.get("citation") for c in citations_list):
                            citations_list.append({
                                "document": cit.get("document", "SmartTech Documentation"),
                                "citation": cit.get("citation", ""),
                                "relevance": cit.get("relevance", 0.90),
                                "retrieval_method": cit.get("retrieval_method", "semantic"),
                            })

                if (
                    order_skill_result
                    and order_skill_result.get("has_orders")
                    and order_skill_result.get("selected_order")
                ):
                    ord_num = order_skill_result["selected_order"].get("order_number")
                    if ord_num and ord_num not in draft_body:
                        draft_body += f"\n\n{order_skill_result.get('summary')}"

            except Exception as exc:
                diagnostic = safe_workflow_error(exc)
                logger.warning("Groq response generation failed (%s)", type(exc).__name__)
                self.tracer.complete_trace(
                    exec_id,
                    draft_id=None,
                    intent=plan.intent,
                    category=plan.category,
                    priority=plan.priority,
                    escalation_recommended=False,
                    confidence_score=0.0,
                    reasoning_summary="Groq generation failed before a response could be validated.",
                    total_time_ms=int((time.time() - start_time) * 1000),
                    status="failed",
                    error_message=diagnostic,
                )
                self.db.commit()
                raise RuntimeError(diagnostic) from exc

        else:
            draft_body = self._synthesize_grounded_draft(
                ticket=ticket,
                customer_name=customer_name,
                message=latest_message,
                warranty_facts=warranty_facts,
                knowledge_matches=knowledge_matches,
                order_result=order_skill_result,
                registered_devices=registered_devices,
            )

            reasoning = (
                "Generated grounded deterministic draft with "
                f"{len(knowledge_matches)} evidence source(s)."
            )

        # Add only serial numbers from this customer's registered devices if relevant
        if (
            registered_devices
            and not plan.requires_clarification
            and plan.intent not in ("privacy_refusal", "unverified_account")
        ):
            for device in registered_devices:
                serial_number = device.get("serial_number")

                if serial_number and serial_number not in draft_body:
                    ref_text = f"Device Serial: {serial_number}"

                    if "Best regards" in draft_body:
                        draft_body = draft_body.replace(
                            "Best regards",
                            f"{ref_text}\n\nBest regards",
                        )
                    elif "Sincerely" in draft_body:
                        draft_body = draft_body.replace(
                            "Sincerely",
                            f"{ref_text}\n\nSincerely",
                        )
                    else:
                        draft_body += f"\n\n{ref_text}"

        if (
            warranty_facts
            and warranty_facts.get("policy_number")
            and not plan.requires_clarification
            and plan.intent not in ("privacy_refusal", "unverified_account")
        ):
            pol_num = warranty_facts["policy_number"]
            if pol_num not in draft_body:
                pol_text = f"Warranty Policy: {pol_num}"
                if "Best regards" in draft_body:
                    draft_body = draft_body.replace(
                        "Best regards",
                        f"{pol_text}\n\nBest regards",
                    )
                elif "Sincerely" in draft_body:
                    draft_body = draft_body.replace(
                        "Sincerely",
                        f"{pol_text}\n\nSincerely",
                    )
                else:
                    draft_body += f"\n\n{pol_text}"

        authorized_orders = []
        if order_skill_result:
            orders_list = order_skill_result.get("orders") or order_skill_result.get("all_orders") or []
            authorized_orders = [
                o.get("order_number")
                for o in orders_list
                if o.get("order_number")
            ]
            if order_skill_result.get("selected_order") and order_skill_result["selected_order"].get("order_number"):
                sel_num = order_skill_result["selected_order"]["order_number"]
                if sel_num not in authorized_orders:
                    authorized_orders.append(sel_num)

        # 11. Guardrails Verification
        validated_body, validated_citations, guardrail_warnings = (
            self.guardrails.apply_all_guardrails(
                draft_body=draft_body,
                plan=plan,
                registered_devices=registered_devices,
                warranty_facts=warranty_facts,
                citations=citations_list,
            knowledge_matches=knowledge_matches,
                customer_name=customer_name,
                ticket_number=ticket.ticket_number,
                authorized_orders=authorized_orders,
                product_catalog_names=self.db.scalars(
                    select(Product.name)
                ).all(),
            )
        )

        warnings.extend(guardrail_warnings)

        # 12. Upsert Draft (Human-in-the-Loop review preserved)
        draft = self.db.scalar(
            select(Draft).where(Draft.ticket_id == ticket.id)
        )

        resolved_recipient = (
            ticket.sender_email
            or (ticket.customer.email if ticket.customer else None)
            or "customer@example.com"
        )

        if not draft:
            draft = Draft(
                ticket_id=ticket.id,
                recipient=resolved_recipient,
                subject=f"Re: {ticket.subject}",
                body=validated_body,
                status="pending_review",
                citations=json.dumps(validated_citations),
            )
            self.db.add(draft)

        elif draft.status == "pending_review" or force_regenerate:
            draft.body = validated_body
            draft.status = "pending_review"
            if not draft.recipient or draft.recipient == "customer@example.com":
                draft.recipient = resolved_recipient
            draft.approved_by = None
            draft.approved_body_hash = None
            draft.citations = json.dumps(validated_citations)


        # Update Ticket Classification
        ticket.category = plan.category
        ticket.priority = plan.priority

        if escalation_recommended:
            ticket.escalation_status = "recommended"

        ticket.status = "awaiting_approval"

        self.db.flush()

        self.tracer.record_step(
            exec_id,
            "ai_draft_generated",
            output_result={
                "response_present": bool(validated_body),
                "citation_count": len(validated_citations),
            },
            duration_ms=0,
        )
        self.tracer.record_step(
            exec_id,
            "draft_saved",
            output_result={"draft_id": draft.id, "status": draft.status},
            duration_ms=0,
        )

        # 13. Finalize Observability Trace
        total_time_ms = int((time.time() - start_time) * 1000)

        self.tracer.complete_trace(
            execution_id=exec_id,
            draft_id=draft.id,
            intent=plan.intent,
            category=plan.category,
            priority=plan.priority,
            escalation_recommended=escalation_recommended,
            confidence_score=confidence,
            reasoning_summary=reasoning,
            total_time_ms=total_time_ms,
            status="completed",
        )

        self.db.commit()
        self.db.refresh(draft)

        return AgentRunResponse(
            success=True,
            generation_mode=generation_mode,
            execution_id=exec_id,
            ticket_id=ticket.id,
            draft_id=draft.id,
            category=plan.category,
            priority=plan.priority,
            escalation_recommended=escalation_recommended,
            confidence_score=confidence,
            draft_body=validated_body,
            citations=validated_citations,
            warnings=warnings,
            execution_time_ms=total_time_ms,
        )

    def _synthesize_grounded_draft(
        self,
        ticket: SupportTicket,
        customer_name: str,
        message: str,
        warranty_facts: dict[str, Any],
        knowledge_matches: list[dict[str, Any]],
        order_result: dict[str, Any] | None,
        registered_devices: list[dict[str, Any]],
    ) -> str:
        """Create a grounded response using verified evidence."""
        greeting = (
            f"Dear {customer_name},\n\n"
            "Thank you for reaching out to SmartTech Customer Support "
            f'regarding ticket {ticket.ticket_number} ("{ticket.subject}").\n\n'
        )

        body_parts = [greeting]

        if order_result:
            body_parts.append(
                f"{order_result.get('summary')}\n\n"
            )

        if knowledge_matches:
            top_knowledge = knowledge_matches[0]
            # Search results can split a documented procedure across adjacent
            # chunks. Keep the strongest evidence plus nearby results from the
            # same document, while never combining evidence from another
            # product document.
            same_document = [
                item for item in knowledge_matches
                if item.get("document_id") == top_knowledge.get("document_id")
            ][:3]
            evidence_chunks = list(dict.fromkeys(
                item.get("content", "").strip()
                for item in same_document
                if item.get("content", "").strip()
            ))
            content = "\n\n".join(evidence_chunks)

            if content:
                body_parts.append(
                    f"Regarding your inquiry, our official "
                    f"{top_knowledge['document']} recommends "
                    f"the following steps:\n\n"
                )

                # Preserve the complete retrieved knowledge chunk.
                body_parts.append(content + "\n\n")

        if warranty_facts and warranty_facts.get("has_warranty"):
            if warranty_facts.get("is_active"):
                body_parts.append(
                    "Our records confirm your device has an active "
                    "manufacturer warranty "
                    f"(Policy #{warranty_facts.get('policy_number')}, "
                    f"valid through {warranty_facts.get('end_date')}). "
                    "If the troubleshooting steps above do not resolve "
                    "the problem, our hardware engineering team can help "
                    "with the next steps for warranty service.\n\n"
                )
            else:
                body_parts.append(
                    "Please note that the standard manufacturer warranty "
                    f"for this hardware expired on "
                    f"{warranty_facts.get('end_date')} "
                    f"(Policy #{warranty_facts.get('policy_number')}). "
                    "We can guide you through troubleshooting or available "
                    "out-of-warranty repair options.\n\n"
                )

        lower_message = message.lower()

        if "lost" in lower_message or "not received" in lower_message:
            body_parts.append(
                "We take shipment delivery discrepancies seriously. "
                "Your concern requires review by the appropriate support "
                "team. We will provide an update after the investigation "
                "status has been verified.\n\n"
            )

        closing = (
            "Please let us know how these steps work for you. "
            "We are here to assist.\n\n"
            "Best regards,\n"
            "SmartTech Support Team"
        )

        body_parts.append(closing)

        return "".join(body_parts)

