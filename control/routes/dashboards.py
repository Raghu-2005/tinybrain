import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from db import get_pool
from auth import generate_id
from models import (
    CreateDashboardRequest,
    CreateDashboardResponse,
    DashboardDataResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("", response_model=CreateDashboardResponse, status_code=201)
async def create_dashboard(body: CreateDashboardRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id FROM tenants WHERE id = $1", body.tenant_id
        )
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        dashboard_id = generate_id("dsh_")
        now = datetime.now(timezone.utc)

        await conn.execute(
            """
            INSERT INTO dashboards
                (id, tenant_id, name, sql, refresh_interval, next_run_at, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            dashboard_id, body.tenant_id, body.name,
            body.sql, body.refresh_interval, now, now
        )

    logger.info("Created dashboard %s for tenant %s", dashboard_id, body.tenant_id)

    return CreateDashboardResponse(
        dashboard_id=dashboard_id,
        tenant_id=body.tenant_id,
        name=body.name,
        sql=body.sql,
        refresh_interval=body.refresh_interval,
        created_at=now,
    )


@router.get("/{dashboard_id}/data", response_model=DashboardDataResponse)
async def get_dashboard_data(dashboard_id: str):
    pool = get_pool()

    async with pool.acquire() as conn:
        dashboard = await conn.fetchrow(
            "SELECT id, name FROM dashboards WHERE id = $1", dashboard_id
        )
        if not dashboard:
            raise HTTPException(status_code=404, detail="Dashboard not found")

        result = await conn.fetchrow(
            """
            SELECT status, rows, row_count, execution_ms,
                   truncated, error_code, error_message, recorded_at
            FROM results
            WHERE dashboard_id = $1
            """,
            dashboard_id
        )

    if not result:
        return DashboardDataResponse(
            dashboard_id=dashboard_id,
            name=dashboard["name"],
            status="pending",
        )

    # asyncpg returns JSONB as a string — parse it explicitly
    import json
    raw_rows = result["rows"]
    if isinstance(raw_rows, str):
        parsed_rows = json.loads(raw_rows)
    elif raw_rows is None:
        parsed_rows = None
    else:
        parsed_rows = list(raw_rows)

    return DashboardDataResponse(
        dashboard_id=dashboard_id,
        name=dashboard["name"],
        status=result["status"],
        rows=parsed_rows,
        row_count=result["row_count"],
        execution_ms=result["execution_ms"],
        truncated=result["truncated"],
        error_code=result["error_code"],
        error_message=result["error_message"],
        recorded_at=result["recorded_at"],
    )
