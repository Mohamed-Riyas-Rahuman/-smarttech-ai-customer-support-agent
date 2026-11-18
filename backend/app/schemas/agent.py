"""AI Agent Execution and Tool schemas."""
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class AgentRunRequest(BaseModel):
    ticket_id: int
    force_regenerate: bool = False
    custom_instructions: str | None = None


class ToolExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tool_name: str
    input_parameters: str | None = None
    output_result: str | None = None
    status: str
    duration_ms: int
    created_at: datetime


class AgentExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    draft_id: int | None = None
    status: str
    user_intent: str | None = None
    issue_category: str | None = None
    priority_recommendation: str | None = None
    escalation_recommended: bool = False
    confidence_score: float | None = None
    reasoning_summary: str | None = None
    error_message: str | None = None
    execution_time_ms: int | None = None
    created_at: datetime
    tool_executions: list[ToolExecutionOut] = []


class AgentRunResponse(BaseModel):
    success: bool
    generation_mode: str = "langchain_react"
    execution_id: int
    ticket_id: int
    draft_id: int | None = None
    category: str
    priority: str
    escalation_recommended: bool
    confidence_score: float
    draft_body: str
    citations: list[Any] = []
    warnings: list[str] = []
    execution_time_ms: int
