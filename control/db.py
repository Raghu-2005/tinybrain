"""
db.py — Database layer for tinybrain-control

Uses asyncpg (async Postgres driver) instead of psycopg2 because:
- FastAPI is fully async; a synchronous DB driver inside an async route
  blocks the entire event loop, killing concurrency under load.
- asyncpg is the fastest Python Postgres driver benchmarked (~3x psycopg2).

Connection pool is created once at startup (via lifespan in main.py) and
shared across all requests. Never create a new connection per request.
"""

import asyncpg
import os
import logging

logger = logging.getLogger(__name__)

# Module-level pool — initialised in init_db(), used everywhere else.
# Keeping it module-level avoids passing it through every function signature.
_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    """
    Create the connection pool and run schema migrations.
    Called once at application startup via FastAPI lifespan.
    """
    global _pool

    database_url = os.environ["DATABASE_URL"]

    # min_size=2: always keep 2 connections warm so the first requests
    # don't pay connection-setup latency.
    # max_size=10: enough for dev; production would size this against
    # Postgres max_connections and expected concurrency.
    _pool = await asyncpg.create_pool(
        database_url,
        min_size=2,
        max_size=10,
        # Fail fast if Postgres is unreachable at startup rather than
        # letting the app start in a broken state.
        timeout=30,
    )

    await _create_schema()
    logger.info("Database pool initialised and schema applied")


async def close_db() -> None:
    """Gracefully drain the pool. Called at application shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        logger.info("Database pool closed")


def get_pool() -> asyncpg.Pool:
    """
    Return the module-level pool.
    Raises if called before init_db() — this is intentional: a missing
    pool is a programming error, not a runtime condition to silently handle.
    """
    if _pool is None:
        raise RuntimeError("Database pool not initialised. Call init_db() first.")
    return _pool


async def _create_schema() -> None:
    """
    Idempotent schema creation using CREATE TABLE IF NOT EXISTS.
    Safe to run on every startup — acts as a lightweight migration for
    this exercise. In production you'd use Alembic.
    """
    async with _pool.acquire() as conn:
        await conn.execute("""
            -- ----------------------------------------------------------------
            -- tenants: one row per customer organisation.
            -- enrollment_token is a one-time-use UUID issued at creation.
            -- enrolled_at is NULL until an agent claims the token.
            -- ----------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS tenants (
                id               TEXT PRIMARY KEY,          -- "tnt_<uuid4_hex>"
                name             TEXT NOT NULL,
                enrollment_token TEXT NOT NULL UNIQUE,      -- plaintext UUID, one-time
                enrolled_at      TIMESTAMPTZ,               -- set when agent enrolls
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            -- ----------------------------------------------------------------
            -- agents: one row per enrolled agent process.
            -- token_hash stores SHA-256(plaintext_token) — the plaintext is
            -- returned once at enrollment and never stored here.
            -- revoked_at: when set, all requests from this agent get 401.
            -- ----------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS agents (
                id            TEXT PRIMARY KEY,             -- "agt_<uuid4_hex>"
                tenant_id     TEXT NOT NULL REFERENCES tenants(id),
                token_hash    TEXT NOT NULL UNIQUE,         -- SHA-256 hex digest
                hostname      TEXT,
                agent_version TEXT,
                last_seen_at  TIMESTAMPTZ,
                revoked_at    TIMESTAMPTZ,                  -- NULL = active
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            -- ----------------------------------------------------------------
            -- dashboards: the query definition owned by the control plane.
            -- SQL lives here, not on the agent — the agent is a dumb executor.
            -- next_run_at: scheduler uses this to decide when to enqueue a job.
            -- ----------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS dashboards (
                id               TEXT PRIMARY KEY,          -- "dsh_<uuid4_hex>"
                tenant_id        TEXT NOT NULL REFERENCES tenants(id),
                name             TEXT NOT NULL,
                sql              TEXT NOT NULL,
                refresh_interval INT  NOT NULL DEFAULT 60,  -- seconds
                next_run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            -- ----------------------------------------------------------------
            -- jobs: the work queue. The agent polls this table via the API.
            -- FOR UPDATE SKIP LOCKED in the poll query prevents two agents
            -- from claiming the same job simultaneously without app-level locking.
            -- status lifecycle: pending → running → done | failed
            -- ----------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS jobs (
                id           TEXT PRIMARY KEY,              -- "job_<uuid4_hex>"
                tenant_id    TEXT NOT NULL REFERENCES tenants(id),
                dashboard_id TEXT NOT NULL REFERENCES dashboards(id),
                sql          TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending','running','done','failed')),
                claimed_by   TEXT REFERENCES agents(id),   -- NULL until claimed
                claimed_at   TIMESTAMPTZ,
                timeout_ms   INT  NOT NULL DEFAULT 30000,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            -- Index so the scheduler's "find pending jobs for tenant" query
            -- doesn't do a full table scan as the jobs table grows.
            CREATE INDEX IF NOT EXISTS idx_jobs_tenant_status
                ON jobs(tenant_id, status);

            -- ----------------------------------------------------------------
            -- results: one row per dashboard, overwritten on each successful run.
            -- The embedding endpoint reads from here — it never triggers a query.
            -- job_id stored for idempotency: duplicate result posts are rejected.
            -- ----------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS results (
                dashboard_id TEXT PRIMARY KEY REFERENCES dashboards(id),
                tenant_id    TEXT NOT NULL REFERENCES tenants(id),
                job_id       TEXT NOT NULL,                 -- idempotency key
                status       TEXT NOT NULL,                 -- "success" | "error"
                rows         JSONB,                         -- result data
                row_count    INT,
                execution_ms INT,
                truncated    BOOLEAN NOT NULL DEFAULT false,
                error_code   TEXT,
                error_message TEXT,
                recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
