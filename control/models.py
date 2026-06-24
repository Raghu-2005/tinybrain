"""
models.py — Pydantic v2 request/response models for tinybrain-control

Pydantic is used for two things here:
1. Request validation — FastAPI automatically parses + validates JSON bodies
   against these models and returns 422 with details if validation fails.
2. Response serialisation — using typed response models means the API contract
   is explicit and documented automatically in /docs (Swagger UI).

All IDs use a prefixed format ("tnt_", "agt_", "dsh_", "job_") so a stray
ID in a log line is immediately identifiable — a pattern from Stripe's API design.
"""

from pydantic import BaseModel, Field
from typing import Any, Optional
from datetime import datetime


# ── Tenant ────────────────────────────────────────────────────────────────────

class CreateTenantRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class CreateTenantResponse(BaseModel):
    tenant_id:        str
    name:             str
    # Returned once — agent uses this to enroll. Never stored in plaintext again.
    enrollment_token: str
    created_at:       datetime


# ── Dashboard ─────────────────────────────────────────────────────────────────

class CreateDashboardRequest(BaseModel):
    tenant_id:        str
    name:             str = Field(..., min_length=1, max_length=200)
    sql:              str = Field(..., min_length=1)
    # How often the scheduler should re-run this dashboard's query (seconds).
    refresh_interval: int = Field(default=60, ge=5, le=86400)


class CreateDashboardResponse(BaseModel):
    dashboard_id: str
    tenant_id:    str
    name:         str
    sql:          str
    refresh_interval: int
    created_at:   datetime


class DashboardDataResponse(BaseModel):
    """Response for the embedding endpoint GET /v1/dashboards/:id/data"""
    dashboard_id: str
    name:         str
    status:       str           # "success" | "error" | "pending"
    rows:         Optional[list[dict[str, Any]]] = None
    row_count:    Optional[int] = None
    execution_ms: Optional[int] = None
    truncated:    bool = False
    error_code:   Optional[str] = None
    error_message: Optional[str] = None
    recorded_at:  Optional[datetime] = None


# ── Agent protocol ────────────────────────────────────────────────────────────

class EnrollRequest(BaseModel):
    agent_version: str = Field(default="unknown", max_length=50)
    hostname:      str = Field(default="unknown", max_length=255)


class EnrollResponse(BaseModel):
    agent_id:    str
    # Long-lived bearer token — plaintext returned once, stored as hash.
    agent_token: str
    tenant_id:   str


class HeartbeatRequest(BaseModel):
    agent_version: str = Field(default="unknown", max_length=50)
    # "idle" or "running" — lets the control plane surface what agents are doing
    status:        str = Field(default="idle", pattern="^(idle|running)$")
    job_id:        Optional[str] = None   # current job if status=running


class HeartbeatResponse(BaseModel):
    ok: bool


class JobResponse(BaseModel):
    """Returned by GET /v1/agent/jobs/next when a job is available."""
    job_id:       str
    dashboard_id: str
    sql:          str
    timeout_ms:   int


class JobResultRequest(BaseModel):
    """Posted by the agent after executing a job."""
    status:        str = Field(..., pattern="^(success|error)$")
    rows:          Optional[list[dict[str, Any]]] = None
    row_count:     Optional[int] = None
    execution_ms:  Optional[int] = None
    truncated:     bool = False
    error_code:    Optional[str] = None
    error_message: Optional[str] = None


class JobResultResponse(BaseModel):
    accepted: bool


class RevokeAgentResponse(BaseModel):
    agent_id:   str
    revoked_at: datetime
