"""Complete LangChain-based Customer Support Email Agent Workflow."""

import time
import uuid
import json
import re
import traceback

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger

from app.services.email_processing.email_service import (
    CustomerEmailPayload,
    EmailService,
)

from app.services.rag.retrieval import (
    identify_product_id,
    resolve_product,
    retrieve_knowledge_context,
    _is_troubleshooting_query,
    select_relevant_evidence_excerpt,
    RetrievedContext,
)

from app.services.langchain_workflow.chains import (
    get_response_generation_chain,
)

from app.services.langchain_workflow.quality_checks import (
    QualityChecker,
    QualityEvaluationResult,
    SafetyCheckResult,
    SentimentCheckResult,
)

from app.services.response_management.draft_service import (
    DraftOutcome,
    DraftService,
)

from app.services.ticket_management.ticket_service import TicketService

from app.services.privacy import find_cross_customer_record_request

from app.services.response_validation import validate_generated_response

from app.models.ticket import SupportTicket
from app.services.agents.tracing import (
    LangfuseTrace,
    start_langfuse_trace,
)


@dataclass
class WorkflowExecutionResult:
    """Execution outcome of the complete workflow."""

    ticket_id: int
    email_id: int
    draft_id: Optional[int]
    status: str
    action_taken: str
    generated_subject: str
    generated_body: str
    citations: List[str]
    confidence_score: float
    sentiment: str
    quality_checks_passed: bool
    requires_human_review: bool
    rejection_reasons: List[str]
    trace_id: Optional[str] = None
    execution_duration_ms: int = 0
    diagnostics: Dict[str, Any] = field(default_factory=dict)


def _mask_pii(text: str) -> str:
    """Mask email addresses and phone numbers in traces/logs."""

    masked = re.sub(
        r'([a-zA-Z0-9_.+-])[a-zA-Z0-9_.+-]*@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)',
        r'\1***@\2',
        text,
    )

    masked = re.sub(
        r'\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b',
        r'***-***-****',
        masked,
    )

    return masked


def _normalize_product_text(text: str) -> str:
    """Normalize product-related text for comparison."""

    return re.sub(
        r"[^a-z0-9]+",
        " ",
        (text or "").lower(),
    ).strip()


def _detect_product_type(text: str) -> Optional[str]:
    """
    Detect the product type mentioned in a product name or inquiry.

    Returns a normalized product type or None.
    """

    normalized = _normalize_product_text(text)

    product_types = {
        "speaker": [
            "speaker",
            "speakers",
            "soundbar",
            "soundbars",
        ],
        "headphones": [
            "headphone",
            "headphones",
            "headset",
            "headsets",
            "earbuds",
            "earbud",
            "earphones",
            "earphone",
        ],
        "laptop": [
            "laptop",
            "laptops",
            "notebook",
            "notebooks",
        ],
        "monitor": [
            "monitor",
            "monitors",
        ],
        "projector": [
            "projector",
            "projectors",
        ],
        "keyboard": [
            "keyboard",
            "keyboards",
        ],
        "mouse": [
            "mouse",
            "mice",
        ],
        "printer": [
            "printer",
            "printers",
        ],
        "smartphone": [
            "smartphone",
            "smartphones",
            "mobile phone",
            "mobile phones",
        ],
        "tablet": [
            "tablet",
            "tablets",
        ],
        "camera": [
            "camera",
            "cameras",
        ],
        "router": [
            "router",
            "routers",
        ],
        "television": [
            "television",
            "televisions",
            "tv",
        ],
        "smartwatch": [
            "smartwatch",
            "smartwatches",
            "smart watch",
            "smart watches",
        ],
    }

    detected_types = []

    for product_type, keywords in product_types.items():

        for keyword in keywords:

            if re.search(
                rf"\b{re.escape(keyword)}\b",
                normalized,
            ):

                detected_types.append(product_type)

                break

    # If the inquiry mentions multiple different product types,
    # do not arbitrarily select one.
    if len(detected_types) == 1:
        return detected_types[0]

    return None


def _build_evidence_grounded_reply(
    *,
    query: str,
    customer_name: str,
    context: RetrievedContext,
) -> str | None:
    """Use verbatim retrieved issue text if generated prose fails grounding."""

    excerpt = select_relevant_evidence_excerpt(query, context.results)
    if not excerpt:
        return None

    greeting = f"Hello {customer_name}," if customer_name != "there" else "Hello,"
    return (
        f"{greeting}\n\n"
        "Thank you for contacting SmartTech Support. I found a directly "
        "matching section in the product documentation. Its documented "
        "instructions are included below:\n\n"
        f"{excerpt}\n\n"
        "Best regards,\nSmartTech Support"
    )


def _build_documentation_limitation_reply(
    *,
    customer_name: str,
    product=None,
) -> str:
    """State a specific documentation limit without discarding product identity."""

    greeting = f"Hello {customer_name}," if customer_name != "there" else "Hello,"
    product_description = (
        f"We identified your product as {product.name} (SKU: {product.sku}).\n\n"
        if product else ""
    )
    return (
        f"{greeting}\n\nThank you for contacting SmartTech Support.\n\n"
        f"{product_description}We could not confirm a directly matching answer "
        "to this question from the product documentation available for this request. "
        "We do not want to guess or provide unverified information.\n\n"
        "Best regards,\nSmartTech Support"
    )


def _has_unregistered_product_variant(
    query: str,
    db: Session,
    product_id: int | None,
) -> bool:
    """
    Detect a product-type mismatch between the customer's request
    and the product selected from the database.

    Example:

        Database:
            SoundPulse Pro ANC Headphones

        Customer:
            SoundPulse Pro Speaker

        Initially identified product:
            ID 1 - SoundPulse Pro ANC Headphones

        Result:
            True

    The workflow must not retrieve the headphones manual.
    """

    from app.models.product import Product

    normalized_query = _normalize_product_text(query)

    if not normalized_query:
        return False

    requested_type = _detect_product_type(
        normalized_query
    )

    if not requested_type:
        return False

    # Check the product selected by identify_product_id().
    # This is the important correction: do not immediately return
    # False merely because product_id is not None.

    selected_product = None

    if product_id is not None:

        selected_product = db.get(
            Product,
            product_id,
        )

    if selected_product:

        selected_product_type = _detect_product_type(
            selected_product.name
        )

        if (
            selected_product_type is not None
            and selected_product_type != requested_type
        ):

            logger.warning(
                "PRODUCT TYPE MISMATCH DETECTED | "
                "Selected product: %s | "
                "Selected type: %s | "
                "Requested type: %s",
                selected_product.name,
                selected_product_type,
                requested_type,
            )

            return True

        # A shared brand or generic product suffix must not make a new model
        # variant look registered (for example, catalog "VisionBeam 4K
        # Portable Projector" vs. customer "VisionBeam Mini Projector").
        selected_name_tokens = set(
            _normalize_product_text(selected_product.name).split()
        )
        query_tokens = normalized_query.split()
        product_category_tokens = {
            token for token in query_tokens
            if token in {
                "speaker", "speakers", "soundbar", "soundbars",
                "headphone", "headphones", "headset", "headsets",
                "earbud", "earbuds", "earphone", "earphones",
                "laptop", "laptops", "notebook", "notebooks",
                "monitor", "monitors", "projector", "projectors",
                "keyboard", "keyboards", "mouse", "mice",
                "printer", "printers", "smartphone", "smartphones",
                "tablet", "tablets", "camera", "cameras",
                "router", "routers", "television", "tv",
                "smartwatch", "smartwatches",
            }
        }
        selected_brand = next(
            (
                token for token in _normalize_product_text(selected_product.name).split()
                if len(token) >= 4 and token not in {
                    "portable", "projector", "laptop", "monitor", "speaker",
                    "headphones", "wireless", "smart",
                }
            ),
            None,
        )
        if selected_brand and product_category_tokens:
            try:
                brand_index = query_tokens.index(selected_brand)
            except ValueError:
                brand_index = -1
            category_indices = [
                index for index, token in enumerate(query_tokens)
                if token in product_category_tokens and index > brand_index
            ]
            if brand_index >= 0 and category_indices:
                category_index = category_indices[0]
                variant_tokens = set(query_tokens[brand_index + 1:category_index])
                variant_tokens -= {
                    "my", "our", "the", "a", "an", "this", "that",
                    "new", "old", "device", "product",
                }
                unknown_variant_tokens = variant_tokens - selected_name_tokens
                if unknown_variant_tokens:
                    logger.warning(
                        "PRODUCT VARIANT NOT IN CATALOG | candidate_product_id=%s | tokens=%s",
                        product_id,
                        sorted(unknown_variant_tokens),
                    )
                    return True

        return False

    # Unregistered/ambiguous decisions belong to resolve_product(); this
    # helper only detects a type conflict against an identified catalog row.
    return False


class LangChainSupportWorkflow:
    """
    Connected LangChain-based Customer Support AI Workflow.

    Receives email
    -> Extracts context
    -> Validates product
    -> PostgreSQL pgvector retrieval
    -> Constructs prompt
    -> Invokes LLM
    -> Performs sentiment and safety checks
    -> Coordinates draft/send
    -> Updates ticket status
    -> Langfuse observability
    """

    def __init__(self, db: Session, langfuse_trace: LangfuseTrace | None = None):

        self.db = db

        self.email_service = EmailService(db)

        self.draft_service = DraftService(db)

        self.ticket_service = TicketService(db)

        self._owns_langfuse_trace = langfuse_trace is None
        self.langfuse_trace = langfuse_trace
        self._langfuse_span_started_at: dict[int, float] = {}

    def _get_langfuse_callback(self, parent_span=None):
        """Build Langfuse callback handler."""
        if self.langfuse_trace is None:
            return None
        parent_id = getattr(parent_span, "id", None)
        return self.langfuse_trace.callback_handler(parent_span_id=parent_id)

    def process_incoming_email(
        self,
        sender: str,
        subject: str,
        body_text: str,
        thread_id: Optional[str] = None,
        provider_message_id: Optional[str] = None,
        custom_instructions: Optional[str] = None,
    ) -> WorkflowExecutionResult:

        """Capture incoming customer email and execute workflow."""

        email_payload = self.email_service.capture_incoming_email(
            sender=sender,
            subject=subject,
            body_text=body_text,
            thread_id=thread_id,
            provider_message_id=provider_message_id,
        )

        return self.execute_workflow_for_payload(
            email_payload,
            custom_instructions=custom_instructions,
        )

    def run_for_ticket(
        self,
        ticket_id: int,
        force_regenerate: bool = False,
        custom_instructions: Optional[str] = None,
        email_id: Optional[int] = None,
    ) -> WorkflowExecutionResult:

        """Process or regenerate workflow for an existing ticket."""

        from app.models.email import EmailMessage

        current_email = (
            self.db.get(EmailMessage, email_id)
            if email_id
            else None
        )

        email_payload = self.email_service.extract_context_for_ticket(
            ticket_id,
            current_email=current_email,
        )

        return self.execute_workflow_for_payload(
            email_payload,
            custom_instructions=custom_instructions,
        )

    def execute_workflow_for_payload(
        self,
        payload: CustomerEmailPayload,
        custom_instructions: Optional[str] = None,
    ) -> WorkflowExecutionResult:

        """Execute the complete customer support workflow."""

        start_time = time.time()

        trace_id = uuid.uuid4().hex

        stages: dict[str, dict[str, Any]] = {}

        active_stage: str | None = None

        active_started: float | None = None

        def stage_start(name: str, **details):

            nonlocal active_stage, active_started

            active_stage = name

            active_started = time.perf_counter()

            stages[name] = {
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "details": details,
            }

            return active_started

        def stage_end(name: str, started: float, **details):

            stages[name].update({
                "status": "succeeded",
                "duration_ms": int(
                    (time.perf_counter() - started) * 1000
                ),
            })

            stages[name]["details"].update(details)

        diagnostics: Dict[str, Any] = {
            "trace_id": trace_id,
            "provider": "Groq",
            "model": settings.groq_model,
            "stages": stages,
        }

        if self.langfuse_trace is None:
            self.langfuse_trace = start_langfuse_trace(
                name="smarttech_email_support_workflow",
                correlation_id=trace_id,
                source="smarttech_support_agent",
                workflow="langchain_email_support",
                tags=["gmail", "support-agent", "rag", "groq", "auto-send"],
                input_summary={
                    "email_id_present": payload.email_id > 0,
                    "ticket_present": payload.ticket_id > 0,
                    "subject_present": bool(payload.subject),
                    "body_present": bool(payload.body_text),
                },
            )
        if self.langfuse_trace:
            trace_id = self.langfuse_trace.trace_id
            diagnostics["trace_id"] = trace_id
            diagnostics["langfuse_trace_id"] = trace_id
            diagnostics["langfuse_root_span_id"] = str(self.langfuse_trace.root.id)

        logger.info(
            "Starting LangChain support workflow (trace_id=%s)",
            trace_id,
        )

        ticket_for_identity = self.db.get(
            SupportTicket,
            payload.ticket_id,
        )

        verified_name = "there"

        if (
            ticket_for_identity
            and ticket_for_identity.customer
            and ticket_for_identity.customer.email.strip().casefold()
            == payload.sender_email.strip().casefold()
            and payload.sender_name != "there"
        ):

            verified_name = (
                ticket_for_identity.customer.full_name.strip()
                or "there"
            )

        # Stage 1: Email Processing Span

        email_span = self._start_span(
            "email_processing",
            {
                "trace_id": trace_id,
            },
        )

        try:

            from app.services.email_risk import assess_inbound_email_risk
            email_risk = assess_inbound_email_risk(
                payload.subject,
                payload.body_text,
            )
            risk_started = stage_start("fraud_risk_assessment")
            stage_end(
                "fraud_risk_assessment",
                risk_started,
                requires_human_review=email_risk.requires_human_review,
                signal=email_risk.signal,
                reason=email_risk.reason,
            )
            diagnostics["fraud_risk"] = {
                "requires_human_review": email_risk.requires_human_review,
                "signal": email_risk.signal,
                "reason": email_risk.reason,
            }

            email_started = stage_start(
                "email_context",
                ticket_id=payload.ticket_id,
                email_id=payload.email_id,
                body_characters=len(payload.body_text or ""),
                sender_present=bool(payload.sender_email),
            )

            stage_end(
                "email_context",
                email_started,
            )

            # --------------------------------------------------
            # STAGE 1.5: CROSS-CUSTOMER PRIVACY CHECK
            # --------------------------------------------------

            privacy_started = stage_start(
                "cross_customer_ownership_check"
            )

            ownership_match = find_cross_customer_record_request(
                self.db,
                customer_id=payload.customer_id,
                subject=payload.subject,
                body=payload.body_text,
            )

            stage_end(
                "cross_customer_ownership_check",
                privacy_started,
                matched=bool(ownership_match),
                record_type=(
                    ownership_match.record_type
                    if ownership_match
                    else None
                ),
            )

            if ownership_match:

                reason = (
                    "A database record linked to a different customer "
                    "was referenced; manual privacy review is required."
                )

                privacy_greeting = (
                    f"Hello {verified_name},"
                    if verified_name != "there"
                    else "Hello,"
                )

                safe_body = (
                    f"{privacy_greeting}\n\n"
                    "Thank you for contacting SmartTech Support. "
                    "For privacy, we can only provide registered "
                    "product or service information to the customer "
                    "account associated with that record. Please have "
                    "the registered account holder contact SmartTech "
                    "Support through their verified account. This draft "
                    "does not include any account-specific information.\n\n"
                    "Best regards,\nSmartTech Support"
                )

                draft_outcome = self.draft_service.save_pending_draft(
                    ticket_id=payload.ticket_id,
                    email_id=(
                        payload.email_id
                        if payload.email_id > 0
                        else None
                    ),
                    recipient=payload.sender_email,
                    subject=(
                        f"Re: {payload.subject}"
                        if not payload.subject.lower().startswith("re:")
                        else payload.subject
                    ),
                    body=safe_body,
                    citations=[],
                    thread_id=payload.thread_id,
                    rejection_reason=reason,
                )

                ticket = self.db.get(
                    SupportTicket,
                    payload.ticket_id,
                )

                if ticket:

                    ticket.category = "privacy"

                    ticket.status = "awaiting_approval"

                    self.db.commit()

                duration_ms = int(
                    (time.time() - start_time) * 1000
                )

                diagnostics["duration_ms"] = duration_ms

                diagnostics["privacy_review_reason"] = reason

                self._end_span(
                    email_span,
                    {
                        "status": "awaiting_approval",
                        "duration_ms": duration_ms,
                    },
                )

                logger.warning(
                    "Cross-customer record request routed to manual review "
                    "(ticket_id=%s, record_type=%s)",
                    payload.ticket_id,
                    ownership_match.record_type,
                )

                return WorkflowExecutionResult(
                    ticket_id=payload.ticket_id,
                    email_id=payload.email_id,
                    draft_id=draft_outcome.draft_id,
                    status="awaiting_approval",
                    action_taken="saved_for_privacy_review",
                    generated_subject=draft_outcome.subject,
                    generated_body=safe_body,
                    citations=[],
                    confidence_score=0.0,
                    sentiment="neutral",
                    quality_checks_passed=False,
                    requires_human_review=True,
                    rejection_reasons=[reason],
                    trace_id=trace_id,
                    execution_duration_ms=duration_ms,
                    diagnostics=diagnostics,
                )

            # --------------------------------------------------
            # STAGE 2: PRODUCT IDENTIFICATION AND VALIDATION
            # --------------------------------------------------

            retrieval_span = self._start_span(
                "pgvector_retrieval",
                {
                    "trace_id": trace_id,
                },
            )

            retrieval_start = stage_start(
                "knowledge_retrieval"
            )

            search_query = (
                f"{payload.subject} {payload.body_text}"
            )

            # Resolve product identity without collapsing an ambiguous name
            # into the unknown-product state.
            product_resolution = resolve_product(
                search_query,
                self.db,
                customer_id=payload.customer_id,
            )

            from app.models.product import Product
            product_id = product_resolution.product_id
            identified_product = self.db.get(Product, product_id) if product_id else None
            product_state = product_resolution.state
            if product_state == "registered" and _has_unregistered_product_variant(
                query=search_query,
                db=self.db,
                product_id=product_id,
            ):
                logger.warning(
                    "PRODUCT TYPE MISMATCH | ticket_id=%s | candidate_product_id=%s",
                    payload.ticket_id,
                    product_id,
                )
                product_state = "unregistered"
                product_id = None
                identified_product = None

            unregistered_product = product_state == "unregistered"
            ambiguous_products = [
                self.db.get(Product, candidate_id)
                for candidate_id in product_resolution.candidate_ids
            ] if product_state == "ambiguous" else []

            has_product_specific_request = bool(
                _detect_product_type(search_query)
                or _is_troubleshooting_query(search_query)
            )
            identity_clarification_required = (
                product_state == "ambiguous"
                or product_state == "unregistered"
                or (product_state == "unspecified" and has_product_specific_request)
            )

            logger.warning(
                "PRODUCT IDENTIFICATION RESULT | "
                "ticket_id=%s | state=%s | product_id=%s | "
                "candidate_ids=%s",
                payload.ticket_id,
                product_state,
                product_id,
                product_resolution.candidate_ids,
            )

            # Never search other products' manuals for an unknown or
            # ambiguous product. Registered products always keep their ID in
            # the retrieval filter, including when no manual is indexed.
            if identity_clarification_required:

                logger.warning(
                    "Skipping product-specific retrieval for product state=%s, ticket_id=%s",
                    product_state,
                    payload.ticket_id,
                )

                retrieved_ctx = RetrievedContext(
                    total_chunks=0,
                    citations=[],
                    formatted_text=(
                        "The requested product or model could not "
                        "be matched to an exact product in the "
                        "company catalog. Do not use documentation "
                        "belonging to another product."
                    ),
                    results=[],
                )

            else:

                retrieved_ctx = retrieve_knowledge_context(
                    query=search_query,
                    db=self.db,
                    product_id=product_id,
                    top_k=(
                        max(
                            settings.rag_top_k,
                            settings.rag_product_top_k,
                        )
                        if product_id
                        else settings.rag_top_k
                    ),
                )

            relevant_excerpt = select_relevant_evidence_excerpt(
                search_query,
                retrieved_ctx.results,
            )
            if product_state == "registered":
                product_state = (
                    "registered_with_relevant_documentation"
                    if relevant_excerpt
                    else "registered_without_relevant_documentation"
                )
            elif product_state == "unspecified" and retrieved_ctx.results:
                product_state = (
                    "unspecified_with_relevant_company_documentation"
                    if relevant_excerpt
                    else "unspecified_without_relevant_documentation"
                )

            stage_end(
                "knowledge_retrieval",
                retrieval_start,
                customer_query=f"{payload.subject}\n{payload.body_text}",
                retrieval_query=search_query,
                search_strategy="product_scoped_hybrid_vector_full_text",
                product_id=product_id,
                product_name=identified_product.name if identified_product else None,
                product_sku=identified_product.sku if identified_product else None,
                product_state=product_state,
                candidate_product_ids=list(product_resolution.candidate_ids),
                product_filter=(
                    "exact product documents plus unassigned policy/FAQ"
                    if product_id else "unassigned policy/FAQ only"
                ),
                unregistered_product=unregistered_product,
                identity_clarification_required=identity_clarification_required,
                relevant_documentation_found=bool(relevant_excerpt),
                chunks_retrieved=retrieved_ctx.total_chunks,
                documents=sorted({
                    item.filename
                    for item in retrieved_ctx.results
                }),
                citations=retrieved_ctx.citations,
                selected_chunks=[
                    {
                        "chunk_id": item.chunk_id,
                        "document_id": item.document_id,
                        "document_title": item.document_title,
                        "filename": item.filename,
                        "similarity": item.similarity,
                        "citation": item.citation,
                        "content": item.content,
                    }
                    for item in retrieved_ctx.results
                ],
            )

            self._end_span(
                retrieval_span,
                {
                    "chunks_retrieved": retrieved_ctx.total_chunks,
                    "latency_ms": stages[
                        "knowledge_retrieval"
                    ]["duration_ms"],
                    "unregistered_product": unregistered_product,
                    "product_state": product_state,
                    "identity_clarification_required": identity_clarification_required,
                },
            )

            # --------------------------------------------------
            # STAGE 3: PROMPT CONSTRUCTION
            # --------------------------------------------------

            prompt_start = stage_start(
                "prompt_construction"
            )

            prompt_span = self._start_span(
                "prompt_preparation",
                {
                    "citations_count": len(
                        retrieved_ctx.citations
                    ),
                },
            )

            conv_str = (
                "No prior messages in this conversation."
            )

            if payload.conversation_history:

                conv_lines = []

                for m in payload.conversation_history[-4:]:

                    conv_lines.append(
                        f"[{m.get('sender_type', 'user')}]: "
                        f"{m.get('message', '')}"
                    )

                conv_str = "\n".join(conv_lines)

            orders_str = ""

            if payload.customer_context.get("orders"):

                orders_str = (
                    f"\nRecent Orders: "
                    f"{payload.customer_context['orders']}"
                )

            if payload.customer_context.get("devices"):

                orders_str += (
                    f"\nRegistered Devices: "
                    f"{payload.customer_context['devices']}"
                )

            full_context_str = f"{conv_str}{orders_str}"

            if custom_instructions:

                full_context_str += (
                    f"\nSpecial Agent Instructions: "
                    f"{custom_instructions}"
                )

            prompt_inputs = {
                "customer_name": verified_name,
                "customer_email": _mask_pii(
                    payload.sender_email
                ),
                "email_subject": payload.subject,
                "customer_email_body": payload.body_text,
                "conversation_context": full_context_str,
                "retrieved_knowledge": retrieved_ctx.formatted_text,
            }

            self._end_span(
                prompt_span,
                {
                    "prompt_variables": list(
                        prompt_inputs.keys()
                    ),
                    "latency_ms": int(
                        (time.time() - prompt_start) * 1000
                    ),
                },
            )

            stage_end(
                "prompt_construction",
                prompt_start,
                retrieved_context_characters=len(
                    retrieved_ctx.formatted_text
                ),
                prompt_variables=sorted(
                    prompt_inputs.keys()
                ),
                final_retrieved_context=retrieved_ctx.formatted_text,
            )

            # --------------------------------------------------
            # STAGE 4: LLM GENERATION OR FIXED FALLBACK
            # --------------------------------------------------

            llm_span = self._start_span(
                "llm_generation",
                {
                    "model": settings.groq_model,
                    "trace_id": trace_id,
                },
            )
            generation_callback = self._get_langfuse_callback(llm_span)
            generation_callbacks = (
                [generation_callback] if generation_callback else []
            )

            llm_start = stage_start(
                "llm_request_and_parse",
                provider="Groq",
                model=settings.groq_model,
            )

            documentation_only_fallback = product_state not in {
                "registered_with_relevant_documentation",
                "unspecified_with_relevant_company_documentation",
            }

            # No LLM generation for an unregistered product.
            if unregistered_product:

                greeting = (
                    f"Hello {verified_name},"
                    if verified_name != "there"
                    else "Hello,"
                )

                fallback_body = (
                    f"{greeting}\n\n"
                    "Thank you for contacting SmartTech Support.\n\n"
                    "We couldn't find the exact product or model "
                    "you mentioned in our current product database. "
                    "Therefore, we cannot verify its specifications "
                    "or provide product-specific troubleshooting "
                    "instructions at this time.\n\n"
                    "Our support team needs to verify the product "
                    "before providing further instructions. Please "
                    "share the exact model number or a photograph "
                    "of the product label so we can identify it "
                    "correctly.\n\n"
                    "Thank you for your patience.\n\n"
                    "Best regards,\nSmartTech Support"
                )

                generated_response = {
                    "subject": (
                        f"Re: {payload.subject}"
                        if not payload.subject.lower().startswith("re:")
                        else payload.subject
                    ),
                    "body": fallback_body,
                }

            elif documentation_only_fallback:

                greeting = (
                    f"Hello {verified_name},"
                    if verified_name != "there"
                    else "Hello,"
                )

                if product_state == "ambiguous":
                    candidate_list = "; ".join(
                        f"{product.name} (SKU: {product.sku})"
                        for product in ambiguous_products if product
                    )
                    fallback_body = (
                        f"{greeting}\n\nThank you for contacting SmartTech Support.\n\n"
                        "I found more than one matching product in our catalog. "
                        f"Please confirm which product you mean: {candidate_list}.\n\n"
                        "Best regards,\nSmartTech Support"
                    )
                elif identity_clarification_required:
                    fallback_body = (
                        f"{greeting}\n\nThank you for contacting SmartTech Support.\n\n"
                        "I could not identify the product from the information in your message. "
                        "Please share its product name, model number, or a photo of the product label "
                        "so we can check the correct documentation.\n\n"
                        "Best regards,\nSmartTech Support"
                    )
                else:
                    fallback_body = _build_documentation_limitation_reply(
                        customer_name=verified_name,
                        product=identified_product,
                    )

                generated_response = {
                    "subject": (
                        f"Re: {payload.subject}"
                        if not payload.subject.lower().startswith("re:")
                        else payload.subject
                    ),
                    "body": fallback_body,
                }

            else:

                generation_chain = get_response_generation_chain(
                    callbacks=generation_callbacks
                )

                generated_response = generation_chain.invoke(
                    prompt_inputs
                )

            resp_subject = generated_response.get(
                "subject",
                f"Re: {payload.subject}",
            )

            resp_body = generated_response.get(
                "body",
                "",
            )

            if not isinstance(resp_body, str) or not resp_body.strip():

                raise ValueError(
                    "Response generation returned an empty email body."
                )

            if not any(
                character.isalpha()
                for character in resp_body
            ):

                raise ValueError(
                    "Response generation returned no customer-facing message text."
                )

            resp_body = resp_body.strip()

            resp_subject = (
                str(resp_subject).strip()
                or f"Re: {payload.subject}"
            )

            # --------------------------------------------------
            # STAGE 4.5: RESPONSE VALIDATION
            # --------------------------------------------------

            # Never call the LLM retry for a deterministic fallback.

            validation_reason = None

            if not documentation_only_fallback:

                validation_reason = validate_generated_response(
                    body=resp_body,
                    inquiry=(
                        f"{payload.subject}\n"
                        f"{payload.body_text}"
                    ),
                    context=retrieved_ctx,
                    expected_product_id=product_id,
                    db=self.db,
                )

            if validation_reason:

                prompt_inputs["conversation_context"] += (
                    "\nRESPONSE VALIDATION RETRY: "
                    "The previous draft failed because: "
                    + validation_reason
                    + ". Remove the unsupported claim and answer "
                    "only from retrieved evidence."
                )

                try:

                    generated_response = generation_chain.invoke(
                        prompt_inputs
                    )

                    resp_subject = str(
                        generated_response.get("subject")
                        or resp_subject
                    ).strip()

                    resp_body = str(
                        generated_response.get("body")
                        or ""
                    ).strip()

                except Exception as retry_exc:

                    logger.warning(
                        "Retry response generation failed (%s); "
                        "keeping draft for manual review",
                        type(retry_exc).__name__,
                    )

                validation_reason = validate_generated_response(
                    body=resp_body,
                    inquiry=(
                        f"{payload.subject}\n"
                        f"{payload.body_text}"
                    ),
                    context=retrieved_ctx,
                    expected_product_id=product_id,
                    db=self.db,
                )

            if validation_reason:
                # The evidence-based retry still failed. When retrieval contains
                # a directly matching issue block, quote that source verbatim
                # instead of claiming the documentation is missing. If no
                # matching block exists, state the limitation without asking
                # for a model identifier already present in the catalog.
                diagnostics["response_validation_error"] = validation_reason
                grounded_reply = _build_evidence_grounded_reply(
                    query=f"{payload.subject}\n{payload.body_text}",
                    customer_name=verified_name,
                    context=retrieved_ctx,
                )
                if grounded_reply:
                    resp_body = grounded_reply
                    diagnostics["response_validation_fallback"] = "verbatim_matching_knowledge_excerpt"
                else:
                    resp_body = _build_documentation_limitation_reply(
                        customer_name=verified_name,
                        product=identified_product,
                    )
                    diagnostics["response_validation_fallback"] = "honest_limitation"
                documentation_only_fallback = True

            # --------------------------------------------------
            # STAGE 4.6: COMPLETE GENERATION STAGE
            # --------------------------------------------------

            stage_end(
                "llm_request_and_parse",
                llm_start,
                response_body_characters=len(resp_body),
                generation_outcome=(
                    "generated_with_evidence" if retrieved_ctx.results
                    else "generated_without_product_specific_evidence"
                ),
            )

            self._record_langfuse_event(
                "ai_draft_generated",
                output={
                    "response_present": bool(resp_body),
                    "citation_count": len(retrieved_ctx.citations),
                },
                metadata={
                    "provider": "deterministic" if documentation_only_fallback else "Groq",
                    "model": None if documentation_only_fallback else settings.groq_model,
                },
            )

            self._end_span(
                llm_span,
                {
                    "model": settings.groq_model,
                    "body_length": len(resp_body),
                    "latency_ms": stages[
                        "llm_request_and_parse"
                    ]["duration_ms"],
                },
            )

            # --------------------------------------------------
            # STAGE 5: QUALITY AND SAFETY CHECKS
            # --------------------------------------------------

            evaluation_start = stage_start(
                "response_validation"
            )

            from app.services.auto_send import (
                is_auto_send_enabled,
                get_confidence_threshold,
            )

            # This decision comes from the original email and is reused after
            # both response-validation and quality retries.
            approval_required = email_risk.requires_human_review

            eval_span = self._start_span(
                "quality_and_safety_checks",
                {
                    "trace_id": trace_id,
                },
            )
            quality_callback = self._get_langfuse_callback(eval_span)
            quality_callbacks = [quality_callback] if quality_callback else []

            if documentation_only_fallback:

                # Deterministic clarification/fallback response.
                # Missing product documentation alone must NOT block auto-send.
                # The response only asks the customer for information needed
                # to identify or investigate the product.
                # Cross-customer privacy requests are handled earlier and
                # remain routed to human approval.

                eval_result = QualityEvaluationResult(
                    passed=True,
                    confidence_score=1.0,
                    sentiment=SentimentCheckResult(
                        customer_sentiment="neutral",
                        is_empathetic_and_appropriate=True,
                        tone_score=1.0,
                        tone_feedback=(
                            "Neutral, professional clarification or "
                            "documentation-limitation notice."
                        ),
                    ),
                    safety=SafetyCheckResult(
                        is_relevant=True,
                        is_grounded=True,
                        policy_compliant=True,
                        confidence_score=1.0,
                        unsupported_statements=[],
                        requires_human_review=False,
                        safety_reasoning=(
                            "Deterministic fallback contains no "
                            "product-specific technical claims and only "
                            "requests clarification or explains the "
                            "documentation limitation."
                        ),
                    ),
                    requires_human_review=False,
                    rejection_reasons=[],
                )

            else:

                checker = QualityChecker(
                    callbacks=quality_callbacks
                )

                eval_result = checker.run_all_checks(
                    subject=payload.subject,
                    email_body=payload.body_text,
                    retrieved_knowledge=retrieved_ctx.formatted_text,
                    response_body=resp_body,
                    auto_send_enabled=is_auto_send_enabled(
                        self.db
                    ),
                    confidence_threshold=get_confidence_threshold(
                        self.db
                    ),
                )

                # Give a quality failure one evidence-constrained retry. A
                # safety evaluator requesting privacy review is never sent
                # back through generation; it proceeds directly to review.
                if (
                    not eval_result.passed
                    and not eval_result.safety.requires_human_review
                    and not documentation_only_fallback
                ):
                    first_rejection_reasons = list(eval_result.rejection_reasons)
                    prompt_inputs["conversation_context"] += (
                        "\nQUALITY CHECK RETRY: The previous response failed these checks: "
                        + "; ".join(first_rejection_reasons)
                        + ". Rewrite the response to address the customer directly, "
                        "use only retrieved evidence, follow support policy, and "
                        "maintain an empathetic professional tone. Do not mention "
                        "these internal checks."
                    )
                    try:
                        retry_response = generation_chain.invoke(prompt_inputs)
                        retry_body = str(retry_response.get("body") or "").strip()
                        retry_subject = str(
                            retry_response.get("subject") or resp_subject
                        ).strip()
                        retry_validation_reason = validate_generated_response(
                            body=retry_body,
                            inquiry=f"{payload.subject}\n{payload.body_text}",
                            context=retrieved_ctx,
                            expected_product_id=product_id,
                            db=self.db,
                        ) if retry_body else "Response generation returned an empty email body."
                        if retry_validation_reason:
                            diagnostics["quality_retry_validation_error"] = retry_validation_reason
                            logger.warning(
                                "Quality retry rejected by response validation "
                                "(trace_id=%s, reason=%s); retaining original response for review",
                                trace_id,
                                retry_validation_reason,
                            )
                        else:
                            resp_body = retry_body
                            resp_subject = retry_subject or resp_subject
                            eval_result = checker.run_all_checks(
                                subject=payload.subject,
                                email_body=payload.body_text,
                                retrieved_knowledge=retrieved_ctx.formatted_text,
                                response_body=resp_body,
                                auto_send_enabled=is_auto_send_enabled(self.db),
                                confidence_threshold=get_confidence_threshold(self.db),
                            )
                            diagnostics["quality_retry_attempted"] = True
                            diagnostics["quality_retry_initial_failures"] = first_rejection_reasons
                    except Exception as retry_exc:
                        diagnostics["quality_retry_error"] = type(retry_exc).__name__
                        logger.warning(
                            "Quality retry failed (trace_id=%s, error_type=%s); retaining original response for review",
                            trace_id,
                            type(retry_exc).__name__,
                        )

            # Fraud review comes from the original inbound email and remains
            # stable across retries. Preserve the evaluator's explicit safety
            # review signal as an additional privacy/security guardrail.
            eval_result.requires_human_review = bool(
                approval_required
                or getattr(
                    eval_result.safety,
                    "requires_human_review",
                    False,
                )
            )

            self._end_span(
                eval_span,
                {
                    "passed": eval_result.passed,
                    "confidence_score": eval_result.confidence_score,
                    "requires_human_review": eval_result.requires_human_review,
                    "rejection_reason_count": len(
                        eval_result.rejection_reasons
                    ),
                    "latency_ms": int(
                        (time.perf_counter() - evaluation_start) * 1000
                    ),
                },
            )

            # Emit only values produced by the completed evaluator/retriever.
            if self.langfuse_trace:
                similarities = [float(item.similarity) for item in retrieved_ctx.results]
                if similarities:
                    self._record_langfuse_score(
                        "retrieval_quality", max(0.0, min(1.0, sum(similarities) / len(similarities))),
                        observation_id=getattr(retrieval_span, "id", None),
                        metadata={"source": "retrieval_similarity", "result_count": len(similarities)},
                    )
                if not documentation_only_fallback:
                    self._record_langfuse_score(
                        "answer_relevance", float(bool(eval_result.safety.is_relevant)),
                        observation_id=getattr(eval_span, "id", None),
                        metadata={"source": "quality_evaluator"},
                    )
                    self._record_langfuse_score(
                        "safety_check",
                        float(bool(eval_result.safety.policy_compliant and eval_result.safety.is_grounded)),
                        observation_id=getattr(eval_span, "id", None),
                        metadata={"source": "quality_evaluator"},
                    )
                self._record_langfuse_score(
                    "citation_availability", float(bool(retrieved_ctx.citations)),
                    observation_id=getattr(retrieval_span, "id", None),
                    metadata={"source": "retrieval_citations_available", "citation_count": len(retrieved_ctx.citations)},
                )

            stage_end(
                "response_validation",
                evaluation_start,
                passed=eval_result.passed,
                confidence_score=eval_result.confidence_score,
                requires_human_review=eval_result.requires_human_review,
                rejection_reasons=eval_result.rejection_reasons,
            )

            # --------------------------------------------------
            # STAGE 6: DRAFT CREATION AND DELIVERY
            # --------------------------------------------------

            draft_span = self._start_span(
                "draft_creation_and_delivery",
                {
                    "trace_id": trace_id,
                    "passed_safety": eval_result.passed,
                    "requires_review": eval_result.requires_human_review,
                },
            )

            draft_start = stage_start(
                "draft_creation"
            )

            draft_outcome: DraftOutcome = (
                self.draft_service.process_delivery_or_draft(
                    ticket_id=payload.ticket_id,
                    email_id=(
                        payload.email_id
                        if payload.email_id > 0
                        else None
                    ),
                    recipient=payload.sender_email,
                    subject=resp_subject,
                    body=resp_body,
                    passed_safety=eval_result.passed,
                    requires_human_review=eval_result.requires_human_review,
                    confidence_score=eval_result.confidence_score,
                    citations=retrieved_ctx.citations,
                    thread_id=payload.thread_id,
                    grounding_validated=True,
                )
            )

            self._record_langfuse_event(
                "draft_saved",
                output={
                    "draft_created": draft_outcome.draft_id is not None,
                    "draft_id": draft_outcome.draft_id,
                    "status": draft_outcome.status,
                },
                metadata={"action_taken": draft_outcome.action_taken},
            )
            if draft_outcome.is_sent:
                self._record_langfuse_event(
                    "email_sent",
                    output={"sent": True},
                    metadata={"channel": "gmail"},
                )

            self._end_span(
                draft_span,
                {
                    "status": draft_outcome.status,
                    "action_taken": draft_outcome.action_taken,
                    "is_sent": draft_outcome.is_sent,
                    "latency_ms": int(
                        (time.perf_counter() - draft_start) * 1000
                    ),
                },
            )

            stage_end(
                "draft_creation",
                draft_start,
                draft_id=draft_outcome.draft_id,
                status=draft_outcome.status,
            )

            # --------------------------------------------------
            # STAGE 7: TICKET STATUS MANAGEMENT
            # --------------------------------------------------

            ticket_span = self._start_span(
                "ticket_status_update",
                {
                    "trace_id": trace_id,
                    "is_sent": draft_outcome.is_sent,
                },
            )

            self.ticket_service.update_after_workflow(
                ticket_id=payload.ticket_id,
                is_sent=draft_outcome.is_sent,
                sentiment=eval_result.sentiment.customer_sentiment,
                category=None,
            )

            self._end_span(
                ticket_span,
                {
                    "final_status": (
                        "resolved"
                        if draft_outcome.is_sent
                        else "awaiting_approval"
                    ),
                },
            )

            # --------------------------------------------------
            # FINAL RESULT
            # --------------------------------------------------

            duration_ms = int(
                (time.time() - start_time) * 1000
            )

            diagnostics["duration_ms"] = duration_ms

            self._end_span(
                email_span,
                {
                    "status": "completed",
                    "duration_ms": duration_ms,
                    "action_taken": draft_outcome.action_taken,
                },
            )

            self._finish_langfuse_trace(
                status="completed",
                output={
                    "workflow_status": "resolved" if draft_outcome.is_sent else "awaiting_approval",
                    "draft_created": draft_outcome.draft_id is not None,
                    "human_review_required": eval_result.requires_human_review,
                },
                duration_ms=duration_ms,
            )

            return WorkflowExecutionResult(
                ticket_id=payload.ticket_id,
                email_id=payload.email_id,
                draft_id=draft_outcome.draft_id,
                status=(
                    "resolved"
                    if draft_outcome.is_sent
                    else "awaiting_approval"
                ),
                action_taken=draft_outcome.action_taken,
                generated_subject=resp_subject,
                generated_body=resp_body,
                citations=retrieved_ctx.citations,
                confidence_score=eval_result.confidence_score,
                sentiment=eval_result.sentiment.customer_sentiment,
                quality_checks_passed=eval_result.passed,
                requires_human_review=eval_result.requires_human_review,
                rejection_reasons=eval_result.rejection_reasons,
                trace_id=trace_id,
                execution_duration_ms=duration_ms,
                diagnostics=diagnostics,
            )

        except Exception as e:

            if (
                active_stage
                and stages.get(active_stage, {}).get("status") == "running"
            ):

                from app.services.workflow_audit import (
                    safe_workflow_error,
                )

                stages[active_stage].update({
                    "status": "failed",
                    "duration_ms": (
                        int(
                            (time.perf_counter() - active_started) * 1000
                        )
                        if active_started is not None
                        else 0
                    ),
                    "error_type": type(e).__name__,
                    "error": safe_workflow_error(e),
                })

            diagnostics["duration_ms"] = int(
                (time.time() - start_time) * 1000
            )

            diagnostics["traceback"] = [
                {
                    "file": (
                        frame.filename.rsplit("/", 1)[-1]
                        .rsplit("\\", 1)[-1]
                    ),
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in traceback.extract_tb(e.__traceback__)
            ]

            try:
                e.workflow_diagnostics = diagnostics

            except Exception:
                pass

            logger.error(
                "LangChain workflow failed "
                "(trace_id=%s, error_type=%s)",
                trace_id,
                type(e).__name__,
            )

            self._end_span(
                email_span,
                {
                    "status": "error",
                    "error_type": type(e).__name__,
                },
            )
            self._finish_langfuse_trace(
                status="error",
                output={
                    "error_type": type(e).__name__,
                    "failed_stage": active_stage or "workflow",
                },
                duration_ms=diagnostics["duration_ms"],
            )

            raise

    def _start_span(
        self,
        name: str,
        metadata: dict,
    ):
        """Start a child observation under the active workflow trace."""
        if self.langfuse_trace is None:
            return None
        as_type = (
            "retriever" if "retrieval" in name
            else "tool" if name in {"draft_creation_and_delivery", "ticket_status_update"}
            else "chain"
        )
        try:
            observation = self.langfuse_trace.start_observation(
                name=name,
                as_type=as_type,
                input={"operation": name},
                metadata=metadata,
            )
            self._langfuse_span_started_at[id(observation)] = time.perf_counter()
            return observation
        except Exception as exc:
            logger.warning("Could not start Langfuse observation %s (%s)", name, type(exc).__name__)
            return None

    def _end_span(
        self,
        span,
        metadata: dict,
    ):
        """Record safe output and duration, then end a child observation."""
        if not span:
            return
        try:
            started = self._langfuse_span_started_at.pop(id(span), None)
            duration_ms = (
                max(0, int((time.perf_counter() - started) * 1000))
                if started is not None else 0
            )
            output = dict(metadata or {})
            status = str(output.pop("status", "completed"))
            span.update(
                output=output,
                metadata={"status": status, "duration_ms": duration_ms},
                level="ERROR" if status == "error" else "DEFAULT",
            )
            span.end()
        except Exception as exc:
            logger.warning("Could not complete Langfuse observation (%s)", type(exc).__name__)

    def _finish_langfuse_trace(
        self,
        *,
        status: str,
        output: dict,
        duration_ms: int,
    ) -> None:
        if self.langfuse_trace is None or not self._owns_langfuse_trace:
            return
        self.langfuse_trace.finish(
            status=status,
            output=output,
            duration_ms=duration_ms,
        )

    def _record_langfuse_event(
        self,
        name: str,
        *,
        output: dict,
        metadata: dict | None = None,
    ) -> None:
        if self.langfuse_trace is None:
            return
        try:
            self.langfuse_trace.create_event(
                name=name,
                output=output,
                metadata=metadata or {},
            )
        except Exception as exc:
            logger.warning("Langfuse event %s failed (%s)", name, type(exc).__name__)

    def _record_langfuse_score(
        self,
        name: str,
        value: float,
        *,
        observation_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if self.langfuse_trace is None:
            return
        try:
            self.langfuse_trace.client.create_score(
                trace_id=self.langfuse_trace.trace_id,
                observation_id=observation_id,
                name=name,
                value=value,
                data_type="NUMERIC",
                metadata=metadata or {},
            )
        except Exception as exc:
            logger.warning("Langfuse score %s failed (%s)", name, type(exc).__name__)
