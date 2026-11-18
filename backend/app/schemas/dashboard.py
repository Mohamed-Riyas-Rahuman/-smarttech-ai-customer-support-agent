"""Dashboard, Analytics, Audit, and Settings schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class DashboardStats(BaseModel):
    total_open_tickets: int
    new_tickets: int
    tickets_awaiting_approval: int
    high_priority_tickets: int
    escalated_tickets: int
    avg_first_response_time_hours: float
    total_customers: int
    total_devices: int
    total_tickets: int = 0
    total_resolved_tickets: int = 0
    approved_drafts_count: int = 0
    knowledge_documents_count: int = 0
    total_drafts_pending: int
    recent_activity: list[dict] = []
    status_distribution: list[dict] = []
    priority_distribution: list[dict] = []
    ticket_trend: list[dict] = []
    # Consolidated Operational Monitoring Metrics
    incoming_emails_count: int = 0
    processed_emails_count: int = 0
    ai_success_count: int = 0
    ai_failed_count: int = 0
    pending_responses_count: int = 0
    sent_responses_count: int = 0
    failed_sending_count: int = 0
    auto_sent_count: int = 0
    blocked_auto_sends_count: int = 0
    tickets_requiring_attention_count: int = 0
    recent_agent_errors: list[dict] = []
    tickets_requiring_attention: list[dict] = []
    pending_drafts_list: list[dict] = []


class AnalyticsReport(BaseModel):
    ticket_volume_by_day: list[dict]
    category_distribution: list[dict]
    priority_distribution: list[dict]
    agent_workload: list[dict]
    ai_metrics: dict
    draft_approval_metrics: dict


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None = None
    action: str
    entity_type: str
    entity_id: str | None = None
    details: str | None = None
    ip_address: str | None = None
    created_at: datetime
    user_email: str | None = None


class SettingOut(BaseModel):
    key: str
    value: str
    description: str | None = None
    is_secret: bool = False
    updated_at: datetime


class SettingUpdate(BaseModel):
    value: str


class SystemStatusOut(BaseModel):
    app_version: str
    environment: str
    database_status: str
    pgvector_status: str
    groq_status: str
    groq_model: str
    groq_error_message: str | None = None
    embedding_model: str
    gmail_status: str
    auto_send_enabled: bool
    confidence_threshold: float
    dependencies: dict[str, str] = {}
    rag_status: str = "operational"
    agent_status: str = "operational"
    email_sending_status: str = "safe_mode"
    gmail_connected_email: str | None = None
    gmail_authorization_error: str | None = None
    gmail_authorization_message: str | None = None
    gmail_permissions_sufficient: bool = False
    gmail_required_scopes: list[str] = []
    gmail_last_authorized_at: datetime | None = None
    gmail_oauth_authorized: bool = False
    gmail_account_verified: bool = False
    gmail_api_available: bool | None = None
    gmail_send_permission: bool = False
    human_approval_policy: str = "Required for responses flagged for human review."
    gmail_redirect_uri: str | None = None
