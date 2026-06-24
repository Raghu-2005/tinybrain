"""
routes/agent.py — Agent-facing protocol endpoints

All endpoints here are called by tinybrain-agent, never by browsers.
Every endpoint (except /enroll) requires a valid agent bearer token.

Endpoints:
  POST /v1/agent/enroll              One-time enrollment with enrollment token
  POST /v1/agent/heartbeat           Liveness signal from agent
  GET  /v1/agent/jobs/next           Long-poll for next available job
  POST /v1/agent/jobs/:id/result     Agent posts completed job result
  POST /v1/admin/agents/:id/revoke   Admin: revoke a compromised agent

Critical invariant enforced here:
  Every job query is scoped by tenant_id derived from the agent's token.
  An agent can ONLY see jobs belonging to its own tenant. This is the
  tenant isolation guarantee — enforced in SQL, not application logic.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Response

from auth import (
    extract_bearer_token,
    generate_agent_token,
    generate_id,
    hash_token,
)
from db import get_pool
from models import (
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    JobResponse,
    JobResultRequest,
    JobResultResponse,
    RevokeAgentResponse,
)

router      = APIRouter()
admin_router = APIRouter()
logger      = logging.getLogger(__name__)

# Maximum result payload size in bytes.
# Enforced before writing to DB — prevents runaway result sets from
# bloating Postgres JSONB columns.
MAX_RESULT_BYTES = 1 * 1024 * 1024  # 1 MB

# How long the server holds a long-poll connection open waiting for a job.
# Client sends ?timeout=N (capped at this value).
MAX_POLL_TIMEOUT = 25  # seconds

# How often the long-poll loop checks for new jobs (seconds).
# Shorter = lower latency; longer = fewer DB queries per idle agent.
POLL_INTERVAL = 1.0  # second


# ── Auth helper ───────────────────────────────────────────────────────────────

async def _resolve_agent(authorization: str | None) -> dict:
    """
    Validate the bearer token and return the agent record.

    Raises 401 if:
    - Header is missing or malformed
    - Token hash not found in DB
    - Agent has been revoked

    This is called at the top of every agent-facing endpoint (except /enroll).
    Centralising it here means auth logic is in one place.
    """
    token = extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing or malformed Bearer token")

    token_hash = hash_token(token)
    pool = get_pool()

    async with pool.acquire() as conn:
        agent = await conn.fetchrow(
            """
            SELECT id, tenant_id, revoked_at
            FROM agents
            WHERE token_hash = $1
            """,
            token_hash
        )

    if not agent:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Revoked agents get 401 — the agent process should stop on receiving this.
    if agent["revoked_at"] is not None:
        raise HTTPException(status_code=401, detail="Agent token has been revoked")

    return dict(agent)


# ── Enrollment ────────────────────────────────────────────────────────────────

@router.post("/enroll", response_model=EnrollResponse, status_code=201)
async def enroll_agent(
    body: EnrollRequest,
    authorization: Optional[str] = Header(default=None),
):
    """
    Exchange a one-time enrollment token for a long-lived agent token.

    The enrollment token is validated against tenants.enrollment_token.
    On success:
    - enrolled_at is set on the tenant row (burns the token)
    - A new agents row is created with the hashed agent token
    - The plaintext agent token is returned — the ONLY time it appears in plaintext

    If enrollment_token was already used: 401.
    """
    enrollment_token = extract_bearer_token(authorization)
    if not enrollment_token:
        raise HTTPException(status_code=401, detail="Missing enrollment token")

    pool = get_pool()

    async with pool.acquire() as conn:
        # Find the tenant by enrollment token.
        # enrolled_at IS NULL check ensures the token is single-use.
        tenant = await conn.fetchrow(
            """
            SELECT id FROM tenants
            WHERE enrollment_token = $1
              AND enrolled_at IS NULL
            """,
            enrollment_token
        )

        if not tenant:
            # Token not found OR already used — same response to prevent enumeration
            raise HTTPException(
                status_code=401,
                detail="Enrollment token is invalid or has already been used"
            )

        tenant_id  = tenant["id"]
        agent_id   = generate_id("agt_")
        # Generate plaintext token — returned to caller, never stored
        plain_token = generate_agent_token()
        # Only the hash is persisted
        token_hash  = hash_token(plain_token)
        now = datetime.now(timezone.utc)

        # Atomic: burn the enrollment token AND create the agent in one transaction.
        # If either fails, neither happens.
        async with conn.transaction():
            await conn.execute(
                "UPDATE tenants SET enrolled_at = $1 WHERE id = $2",
                now, tenant_id
            )
            await conn.execute(
                """
                INSERT INTO agents
                    (id, tenant_id, token_hash, hostname, agent_version,
                     last_seen_at, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                agent_id, tenant_id, token_hash,
                body.hostname, body.agent_version, now, now
            )

    logger.info("Agent %s enrolled for tenant %s", agent_id, tenant_id)

    return EnrollResponse(
        agent_id=agent_id,
        agent_token=plain_token,   # plaintext — caller must store securely
        tenant_id=tenant_id,
    )


# ── Heartbeat ─────────────────────────────────────────────────────────────────

@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    body: HeartbeatRequest,
    authorization: Optional[str] = Header(default=None),
):
    """
    Liveness signal from the agent.

    Updates last_seen_at on the agent record. The control plane uses this
    to surface "agent offline" warnings in observability (agents with
    last_seen_at > 90s ago are considered unhealthy).

    Returns 401 if the agent has been revoked — the agent must stop on
    receiving this response.
    """
    agent = await _resolve_agent(authorization)
    pool  = get_pool()

    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agents
            SET last_seen_at  = $1,
                agent_version = $2
            WHERE id = $3
            """,
            now, body.agent_version, agent["id"]
        )

    return HeartbeatResponse(ok=True)


# ── Job polling (long-poll) ───────────────────────────────────────────────────

@router.get("/jobs/next")
async def get_next_job(
    authorization: Optional[str] = Header(default=None),
    timeout: int = Query(default=25, ge=1, le=MAX_POLL_TIMEOUT),
):
    """
    Long-poll for the next pending job.

    The agent calls this in a tight loop. The server holds the connection
    open for up to `timeout` seconds, checking for a new job every POLL_INTERVAL.
    This is the outbound-only pattern: the agent initiates, the server responds.

    Returns:
      200 + JobResponse  — a job is available and has been claimed
      204                — no job arrived within the timeout window
      401                — token revoked (agent must stop)

    TENANT ISOLATION: The SQL query filters by tenant_id derived from the
    agent's token. An agent cannot receive jobs from another tenant regardless
    of what it sends in the request — the tenant_id comes from our DB, not the caller.

    FOR UPDATE SKIP LOCKED: Prevents two agents (if a tenant runs multiple)
    from racing to claim the same job. Other databases call this "advisory locks"
    or "SELECT ... FOR UPDATE NOWAIT". Postgres's SKIP LOCKED is purpose-built
    for job queue patterns.
    """
    agent     = await _resolve_agent(authorization)
    tenant_id = agent["tenant_id"]
    agent_id  = agent["id"]
    pool      = get_pool()

    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Claim the next pending job for THIS tenant only.
                # SKIP LOCKED means if another agent claimed it a millisecond ago,
                # we skip it rather than waiting — no deadlocks possible.
                job = await conn.fetchrow(
                    """
                    SELECT id, dashboard_id, sql, timeout_ms
                    FROM jobs
                    WHERE tenant_id = $1
                      AND status    = 'pending'
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    tenant_id
                )

                if job:
                    # Atomically claim the job inside the same transaction
                    await conn.execute(
                        """
                        UPDATE jobs
                        SET status     = 'running',
                            claimed_by = $1,
                            claimed_at = $2
                        WHERE id = $3
                        """,
                        agent_id, datetime.now(timezone.utc), job["id"]
                    )

                    logger.info(
                        "Agent %s (tenant %s) claimed job %s",
                        agent_id, tenant_id, job["id"]
                    )

                    return JobResponse(
                        job_id=job["id"],
                        dashboard_id=job["dashboard_id"],
                        sql=job["sql"],
                        timeout_ms=job["timeout_ms"],
                    )

        # No job yet — yield the event loop and check again after POLL_INTERVAL.
        # asyncio.sleep() is non-blocking: other requests proceed normally.
        await asyncio.sleep(POLL_INTERVAL)

    # Timeout elapsed with no job — return 204 No Content.
    # The agent will immediately call this endpoint again.
    return Response(status_code=204)


# ── Result submission ─────────────────────────────────────────────────────────

@router.post("/jobs/{job_id}/result", response_model=JobResultResponse)
async def submit_job_result(
    job_id: str,
    body: JobResultRequest,
    authorization: Optional[str] = Header(default=None),
):
    """
    Agent posts the result of a completed job.

    Idempotency: if a result for this job_id already exists, return 409.
    This handles the case where the agent posts successfully but the
    network drops before it receives the 200 — the retry won't double-write.

    Cross-tenant guard: verifies the job belongs to this agent's tenant.
    An agent cannot submit results for another tenant's job.

    On success: upserts the results table (keyed by dashboard_id) and
    updates the job status to 'done' or 'failed'.
    """
    agent     = await _resolve_agent(authorization)
    tenant_id = agent["tenant_id"]
    pool      = get_pool()

    async with pool.acquire() as conn:
        # Verify job exists and belongs to this agent's tenant
        job = await conn.fetchrow(
            """
            SELECT id, dashboard_id, tenant_id
            FROM jobs
            WHERE id = $1
            """,
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Cross-tenant guard — should never trigger if the agent is legitimate,
        # but defence in depth: never trust the caller's claimed identity
        if job["tenant_id"] != tenant_id:
            logger.warning(
                "SECURITY: Agent %s (tenant %s) attempted to submit result "
                "for job %s (tenant %s)",
                agent["id"], tenant_id, job_id, job["tenant_id"]
            )
            raise HTTPException(status_code=403, detail="Job does not belong to your tenant")

        # Idempotency check — was a result already recorded for this job?
        existing = await conn.fetchrow(
            "SELECT job_id FROM results WHERE job_id = $1", job_id
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail="Result for this job_id was already submitted"
            )

        now = datetime.now(timezone.utc)

        async with conn.transaction():
            # Upsert the result (INSERT ... ON CONFLICT UPDATE) because the
            # results table is keyed by dashboard_id — newer results overwrite older.
            # Serialize rows to JSON if present — asyncpg JSONB columns need JSON strings
            rows_json = json.dumps(body.rows) if body.rows is not None else None
            
            await conn.execute(
                """
                INSERT INTO results
                    (dashboard_id, tenant_id, job_id, status, rows, row_count,
                     execution_ms, truncated, error_code, error_message, recorded_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (dashboard_id) DO UPDATE SET
                    job_id        = EXCLUDED.job_id,
                    status        = EXCLUDED.status,
                    rows          = EXCLUDED.rows,
                    row_count     = EXCLUDED.row_count,
                    execution_ms  = EXCLUDED.execution_ms,
                    truncated     = EXCLUDED.truncated,
                    error_code    = EXCLUDED.error_code,
                    error_message = EXCLUDED.error_message,
                    recorded_at   = EXCLUDED.recorded_at
                """,
                job["dashboard_id"], tenant_id, job_id,
                body.status, rows_json, body.row_count,
                body.execution_ms, body.truncated,
                body.error_code, body.error_message, now
            )

            # Mark the job terminal
            final_status = "done" if body.status == "success" else "failed"
            await conn.execute(
                "UPDATE jobs SET status = $1 WHERE id = $2",
                final_status, job_id
            )

    logger.info(
        "Result accepted for job %s (dashboard %s, status=%s, %dms)",
        job_id, job["dashboard_id"], body.status, body.execution_ms or 0
    )

    return JobResultResponse(accepted=True)


# ── Admin: revoke agent ───────────────────────────────────────────────────────

@admin_router.post("/agents/{agent_id}/revoke", response_model=RevokeAgentResponse)
async def revoke_agent(
    agent_id: str,
    admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
):
    """
    Revoke a compromised or decommissioned agent.

    After revocation, all subsequent heartbeat and job-poll requests
    from that agent return 401. The agent's poll loop must exit on 401.

    Protected by a static admin key (env var ADMIN_API_KEY).
    In production this would be an IAM-scoped endpoint or require MFA.
    """
    expected_key = os.environ.get("ADMIN_API_KEY", "dev-admin-key")
    if admin_key != expected_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")

    pool = get_pool()
    now  = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        result = await conn.fetchrow(
            """
            UPDATE agents
            SET revoked_at = $1
            WHERE id = $2
              AND revoked_at IS NULL
            RETURNING id, revoked_at
            """,
            now, agent_id
        )

    if not result:
        raise HTTPException(
            status_code=404,
            detail="Agent not found or already revoked"
        )

    logger.warning("Agent %s has been REVOKED", agent_id)

    return RevokeAgentResponse(
        agent_id=result["id"],
        revoked_at=result["revoked_at"],
    )
