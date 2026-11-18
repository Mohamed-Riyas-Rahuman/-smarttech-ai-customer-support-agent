"""LangChain tool adapters for SmartTech AI Support Agent.

Exposes verified backend tools to the LangChain ReAct agent while enforcing:
- Strict authorization bounds (customer-isolation enforced by backend context, not LLM).
- Dual recording: logs to both AgentExecutionTracer (PostgreSQL) and Langfuse callbacks.
- PII and credential sanitization.
- Safe structured outputs.
"""

import time
from typing import Any, Callable

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.ticket import SupportTicket
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
from app.services.rag.retrieval import identify_product_id


class ToolExecutionContext:
    """Carries execution state, citations, and captured tool results for an agent run."""

    def __init__(
        self,
        db: Session,
        ticket: SupportTicket,
        tracer: AgentExecutionTracer | None = None,
        execution_id: int | None = None,
        target_product_id: int | None = None,
    ):
        self.db = db
        self.ticket = ticket
        self.customer_id = ticket.customer_id
        self.target_product_id = target_product_id
        self.tracer = tracer
        self.execution_id = execution_id
        self.citations: list[dict[str, Any]] = []
        self.called_tools: list[str] = []
        self.tool_results: dict[str, Any] = {}

    def record_step(
        self,
        tool_name: str,
        input_params: dict[str, Any] | None,
        output_result: Any,
        duration_ms: int,
        status: str = "success",
    ) -> None:
        self.called_tools.append(tool_name)
        if self.tracer and self.execution_id is not None:
            try:
                self.tracer.record_step(
                    execution_id=self.execution_id,
                    step_name=tool_name,
                    input_params=input_params,
                    output_result=output_result,
                    status=status,
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to record step %s to tracer: %s", tool_name, exc
                )


class SerialLookupInput(BaseModel):
    serial_number: str = Field(
        ...,
        description="Hardware serial number of the device to inspect (e.g. ST-SN-AUD-88219).",
    )


class ProductSpecInput(BaseModel):
    sku: str | None = Field(
        None,
        description="Product SKU identifier (e.g. ST-AUD-001).",
    )
    product_name: str | None = Field(
        None,
        description="Name or partial name of the product (e.g. SoundPulse Pro ANC Headphones).",
    )


class KnowledgeSearchInput(BaseModel):
    query: str = Field(
        ...,
        description="Technical troubleshooting or policy question to search in the documentation.",
    )
    product_id: int | None = Field(
        None,
        description="Optional product ID to restrict knowledge search to relevant product manual.",
    )


def create_agent_tools(context: ToolExecutionContext) -> list[BaseTool]:
    """Builds authorized LangChain tools strictly scoped to the ticket customer."""

    @tool
    def get_customer_details() -> dict[str, Any]:
        """Retrieve verified customer profile details for the authenticated customer."""
        t0 = time.time()
        try:
            res = tool_get_customer_details(
                db=context.db,
                customer_id=context.customer_id,
            )
            d_ms = int((time.time() - t0) * 1000)
            context.record_step("get_customer_details", {"customer_id": context.customer_id}, res, d_ms)
            context.tool_results["customer_details"] = res
            return res
        except Exception as exc:
            d_ms = int((time.time() - t0) * 1000)
            err = {"error": f"Failed to retrieve customer details: {exc}"}
            context.record_step("get_customer_details", {"customer_id": context.customer_id}, err, d_ms, status="error")
            return err

    @tool
    def get_registered_devices() -> list[dict[str, Any]]:
        """Retrieve all hardware devices registered to the verified customer."""
        t0 = time.time()
        try:
            res = tool_get_registered_devices(
                db=context.db,
                customer_id=context.customer_id,
            )
            d_ms = int((time.time() - t0) * 1000)
            context.record_step("get_registered_devices", {"customer_id": context.customer_id}, {"count": len(res)}, d_ms)
            context.tool_results["registered_devices"] = res
            return res
        except Exception as exc:
            d_ms = int((time.time() - t0) * 1000)
            err = [{"error": f"Failed to retrieve registered devices: {exc}"}]
            context.record_step("get_registered_devices", {"customer_id": context.customer_id}, err, d_ms, status="error")
            return err

    @tool
    def get_customer_orders() -> list[dict[str, Any]]:
        """Retrieve verified order history and tracking information belonging exclusively to the customer."""
        t0 = time.time()
        try:
            res = tool_get_customer_orders(
                db=context.db,
                customer_id=context.customer_id,
            )
            d_ms = int((time.time() - t0) * 1000)
            context.record_step("get_customer_orders", {"customer_id": context.customer_id}, {"count": len(res)}, d_ms)
            context.tool_results["orders"] = res
            return res
        except Exception as exc:
            d_ms = int((time.time() - t0) * 1000)
            err = [{"error": f"Failed to retrieve customer orders: {exc}"}]
            context.record_step("get_customer_orders", {"customer_id": context.customer_id}, err, d_ms, status="error")
            return err

    @tool(args_schema=SerialLookupInput)
    def get_device_by_serial(serial_number: str) -> dict[str, Any]:
        """Look up device record by serial number, verifying customer ownership."""
        t0 = time.time()
        try:
            res = tool_get_device_by_serial(
                db=context.db,
                serial_number=serial_number,
                customer_id=context.customer_id,
            )
            d_ms = int((time.time() - t0) * 1000)
            context.record_step("get_device_by_serial", {"serial_number": serial_number}, res, d_ms)
            context.tool_results[f"device_{serial_number}"] = res
            return res
        except Exception as exc:
            d_ms = int((time.time() - t0) * 1000)
            err = {"error": f"Failed to lookup device by serial: {exc}"}
            context.record_step("get_device_by_serial", {"serial_number": serial_number}, err, d_ms, status="error")
            return err

    @tool(args_schema=SerialLookupInput)
    def get_warranty_details(serial_number: str) -> dict[str, Any]:
        """Look up warranty status, policy number, and coverage period for a customer device by serial number."""
        t0 = time.time()
        try:
            res = tool_get_warranty_details(
                db=context.db,
                serial_number=serial_number,
                customer_id=context.customer_id,
            )
            d_ms = int((time.time() - t0) * 1000)
            context.record_step("get_warranty_details", {"serial_number": serial_number}, res, d_ms)
            context.tool_results[f"warranty_{serial_number}"] = res
            return res
        except Exception as exc:
            d_ms = int((time.time() - t0) * 1000)
            err = {"error": f"Failed to retrieve warranty details: {exc}"}
            context.record_step("get_warranty_details", {"serial_number": serial_number}, err, d_ms, status="error")
            return err

    @tool(args_schema=ProductSpecInput)
    def get_product_specifications(
        sku: str | None = None,
        product_name: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve official product specifications from the product catalog by SKU or product name."""
        t0 = time.time()
        try:
            res = tool_get_product_specifications(
                db=context.db,
                sku=sku,
                product_name=product_name,
            )
            d_ms = int((time.time() - t0) * 1000)
            context.record_step("get_product_specifications", {"sku": sku, "product_name": product_name}, res, d_ms)
            context.tool_results["product_specifications"] = res
            return res
        except Exception as exc:
            d_ms = int((time.time() - t0) * 1000)
            err = {"error": f"Failed to retrieve product specifications: {exc}"}
            context.record_step("get_product_specifications", {"sku": sku, "product_name": product_name}, err, d_ms, status="error")
            return err

    @tool(args_schema=KnowledgeSearchInput)
    def search_knowledge_base(
        query: str,
        product_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search approved technical documentation, user manuals, and policy documentation."""
        t0 = time.time()
        try:
            query_product_id = identify_product_id(query, context.db)
            scoped_product_id = query_product_id or context.target_product_id
            matches = tool_search_knowledge_base(
                db=context.db,
                query=query,
                product_id=scoped_product_id,
                min_similarity=0.40,
                top_k=4,
            )
            d_ms = int((time.time() - t0) * 1000)

            # Record citations for draft grounding
            for item in matches:
                citation = {
                    "document": item.get("document", "SmartTech Documentation"),
                    "citation": item.get("citation", ""),
                    "relevance": float(item.get("similarity", 0.0)),
                    "retrieval_method": item.get("retrieval_method", "semantic"),
                    "content": item.get("content", ""),
                }
                if citation not in context.citations:
                    context.citations.append(citation)

            context.record_step(
                "search_knowledge_base",
                {"query": query[:500], "product_id": scoped_product_id},
                {
                    "matches_count": len(matches),
                    "selected_chunks": [
                        {
                            "chunk_id": item.get("chunk_id"),
                            "document_id": item.get("document_id"),
                            "document": item.get("document"),
                            "similarity": item.get("similarity"),
                            "retrieval_method": item.get("retrieval_method"),
                            "citation": item.get("citation"),
                            "content": item.get("content"),
                        }
                        for item in matches
                    ],
                },
                d_ms,
            )
            context.tool_results["knowledge_matches"] = matches
            return matches
        except Exception as exc:
            d_ms = int((time.time() - t0) * 1000)
            err = [{"error": f"Failed to search knowledge base: {exc}"}]
            context.record_step(
                "search_knowledge_base",
                {"query": query[:100], "product_id": scoped_product_id},
                err,
                d_ms,
                status="error",
            )
            return err

    return [
        get_customer_details,
        get_registered_devices,
        get_customer_orders,
        get_device_by_serial,
        get_warranty_details,
        get_product_specifications,
        search_knowledge_base,
    ]
