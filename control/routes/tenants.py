"""
routes/tenants.py — Tenant management endpoints

POST /v1/tenants   Create a tenant, get back an enrollment token.

The enrollment token is the bootstrap credential: the customer takes it,
puts it in their agent's environment, and the agent uses it exactly once
to register itself. After that, the token is burned and the agent uses
its own long-lived token.
"""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException

from db import get_pool
from auth import generate_enrollment_token, generate_id
from models import CreateTenantRequest, CreateTenantResponse

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("", response_model=CreateTenantResponse, status_code=201)
async def create_tenant(body: CreateTenantRequest):
    """
    Create a new tenant and return a one-time enrollment token.

    The enrollment token is stored in plaintext in the tenants table
    because it needs to be compared against what the agent sends at
    enrollment time. It has no value after enrollment (it's burned).
    """
    pool = get_pool()
    tenant_id        = generate_id("tnt_")
    enrollment_token = generate_enrollment_token()
    now              = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tenants (id, name, enrollment_token, created_at)
            VALUES ($1, $2, $3, $4)
            """,
            tenant_id, body.name, enrollment_token, now
        )

    logger.info("Created tenant %s (%s)", tenant_id, body.name)

    return CreateTenantResponse(
        tenant_id=tenant_id,
        name=body.name,
        enrollment_token=enrollment_token,
        created_at=now,
    )
