"""
scheduler.py — Background scheduler for tinybrain-control

Two responsibilities:
1. Job enqueuer: reads dashboards where next_run_at <= now() and creates
   a pending job for each. Updates next_run_at to now + refresh_interval.

2. Stale-job reaper: finds jobs stuck in 'running' status beyond their
   timeout window and resets them to 'pending' so another agent can retry.
   This handles: agent crash, network partition, agent killed mid-job.

Why asyncio.create_task() instead of threading.Thread()?
- FastAPI runs on asyncio. A blocking thread.sleep() inside a Thread
  works but can't share the asyncpg pool (asyncpg connections are
  not thread-safe). asyncio.sleep() yields the event loop — other
  HTTP requests continue processing while the scheduler waits.
- This approach lets the scheduler and the HTTP server share the same
  asyncpg connection pool without any locking.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from db import get_pool
from auth import generate_id

logger = logging.getLogger(__name__)

# How often the scheduler wakes up to look for work (seconds).
# Shorter = more responsive; longer = fewer idle DB queries.
SCHEDULER_TICK = 5  # seconds

# Grace period beyond job timeout before we consider it stale.
# Avoids reaping jobs that are almost done.
STALE_JOB_GRACE_SECONDS = 10


async def scheduler_loop() -> None:
    """
    Main scheduler loop. Runs forever until cancelled (at app shutdown).
    Cancellation is handled gracefully via asyncio.CancelledError.
    """
    logger.info("Scheduler started (tick=%ds)", SCHEDULER_TICK)

    while True:
        try:
            await _enqueue_due_dashboards()
            await _reap_stale_jobs()
        except asyncio.CancelledError:
            # App is shutting down — exit cleanly
            logger.info("Scheduler cancelled, exiting")
            return
        except Exception as exc:
            # Log the error but keep the scheduler running.
            # A single DB hiccup should not kill the scheduler permanently.
            logger.error("Scheduler tick error: %s", exc, exc_info=True)

        await asyncio.sleep(SCHEDULER_TICK)


async def _enqueue_due_dashboards() -> None:
    """
    Find every dashboard whose next_run_at has passed, create a pending job,
    and advance next_run_at by refresh_interval.

    Using a single SQL statement (UPDATE ... RETURNING) to atomically find
    and advance the schedule prevents double-enqueuing if two scheduler
    instances ever ran simultaneously.
    """
    pool = get_pool()
    now  = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        # Fetch dashboards due for a run
        due = await conn.fetch(
            """
            UPDATE dashboards
            SET next_run_at = next_run_at + (refresh_interval * INTERVAL '1 second')
            WHERE next_run_at <= $1
            RETURNING id, tenant_id, sql, refresh_interval
            """,
            now
        )

        if not due:
            return

        # Bulk insert jobs for all due dashboards
        for dash in due:
            job_id = generate_id("job_")
            await conn.execute(
                """
                INSERT INTO jobs (id, tenant_id, dashboard_id, sql, status, created_at)
                VALUES ($1, $2, $3, $4, 'pending', $5)
                """,
                job_id, dash["tenant_id"], dash["id"], dash["sql"], now
            )
            logger.info(
                "Enqueued job %s for dashboard %s (tenant %s)",
                job_id, dash["id"], dash["tenant_id"]
            )


async def _reap_stale_jobs() -> None:
    """
    Reset jobs that have been 'running' for longer than their timeout + grace period.

    A job gets stuck in 'running' if the agent:
    - Crashed while executing
    - Lost network connectivity before posting the result
    - Was killed (SIGKILL) mid-execution

    Resetting to 'pending' allows any healthy agent for that tenant to retry.
    """
    pool = get_pool()
    now  = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        reaped = await conn.fetch(
            """
            UPDATE jobs
            SET status     = 'pending',
                claimed_by = NULL,
                claimed_at = NULL
            WHERE status = 'running'
              AND claimed_at < $1
            RETURNING id, dashboard_id, tenant_id
            """,
            # Jobs claimed longer ago than timeout + grace are considered stale.
            # We use the maximum timeout (30s) + grace as a conservative bound
            # since we don't store per-job timeout in a way we can compute here.
            now - timedelta(seconds=30 + STALE_JOB_GRACE_SECONDS)
        )

        for job in reaped:
            logger.warning(
                "Reaped stale job %s (dashboard %s, tenant %s) — reset to pending",
                job["id"], job["dashboard_id"], job["tenant_id"]
            )
