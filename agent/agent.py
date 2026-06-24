"""
agent.py — tinybrain-agent worker process

This is the ONLY component that runs inside the customer's network and
the ONLY component that touches the customer's warehouse (DuckDB here).

Lifecycle:
  1. Seed DuckDB with synthetic data
  2. Enroll with the control plane (exchange one-time token for agent token)
  3. Start heartbeat loop (background thread — keeps last_seen_at fresh)
  4. Poll loop: GET /jobs/next → execute SQL → POST result → repeat

Security properties enforced here:
  - The agent only executes SQL that arrived through the official job protocol.
    There is no eval(), no exec(), no shell injection surface.
  - Warehouse credentials (if this were a real warehouse) never leave this process.
  - On 401 from the control plane, the agent stops immediately — it does not
    retry with exponential backoff (which would just spam a revoked token).

All outbound traffic goes to CONTROL_PLANE_URL — the agent opens no
listening ports. No inbound firewall rules are needed on the customer's end.
"""

import duckdb
import json
import logging
import os
import sys
import time
import threading
from decimal import Decimal

import requests  # synchronous HTTP — agent is a simple worker, no async needed

from seed import seed_database


def _convert_to_json_serializable(obj):
    """
    Recursively convert objects to JSON-serializable types.
    Converts Decimal to float, and handles nested dicts/lists.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: _convert_to_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_convert_to_json_serializable(item) for item in obj]
    else:
        return obj

# ── Configuration from environment ───────────────────────────────────────────
CONTROL_URL      = os.environ["CONTROL_PLANE_URL"].rstrip("/")
ENROLLMENT_TOKEN = os.environ["ENROLLMENT_TOKEN"]
TENANT_ID        = os.environ.get("TENANT_ID", "unknown")
DB_PATH          = os.environ.get("DB_PATH", "/data/warehouse.db")
HEARTBEAT_SEC    = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
POLL_TIMEOUT     = int(os.environ.get("POLL_TIMEOUT", "25"))  # long-poll seconds
AGENT_VERSION    = "0.1.0"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] agent(%(name)s): %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(TENANT_ID)

# Maximum result size the agent will attempt to POST (matches control plane limit)
MAX_RESULT_BYTES = 1 * 1024 * 1024  # 1 MB

# Module-level agent token — set after enrollment, used by all subsequent calls
_agent_token: str | None = None
_agent_id: str | None = None


# ── Enrollment ────────────────────────────────────────────────────────────────

def enroll() -> None:
    """
    Exchange the one-time enrollment token for a long-lived agent token.

    Retries with backoff if the control plane isn't ready yet (common at
    Docker Compose startup when containers come up simultaneously).
    """
    global _agent_token, _agent_id

    logger.info("Enrolling with control plane at %s", CONTROL_URL)

    for attempt in range(1, 11):  # max 10 attempts
        try:
            resp = requests.post(
                f"{CONTROL_URL}/v1/agent/enroll",
                json={
                    "agent_version": AGENT_VERSION,
                    "hostname":      os.environ.get("HOSTNAME", "unknown"),
                },
                headers={"Authorization": f"Bearer {ENROLLMENT_TOKEN}"},
                timeout=10,
            )

            if resp.status_code == 201:
                data = resp.json()
                _agent_token = data["agent_token"]
                _agent_id    = data["agent_id"]
                logger.info(
                    "Enrolled successfully. agent_id=%s tenant_id=%s",
                    _agent_id, data["tenant_id"]
                )
                return

            elif resp.status_code == 401:
                # Token already used or invalid — fatal, do not retry
                logger.error(
                    "Enrollment rejected (401): %s. "
                    "Check ENROLLMENT_TOKEN env var.", resp.text
                )
                sys.exit(1)

            else:
                logger.warning(
                    "Enrollment attempt %d failed (HTTP %d): %s",
                    attempt, resp.status_code, resp.text
                )

        except requests.exceptions.ConnectionError:
            logger.warning(
                "Attempt %d: control plane not reachable yet, retrying in 3s...",
                attempt
            )

        time.sleep(3)

    logger.error("Could not enroll after 10 attempts. Exiting.")
    sys.exit(1)


# ── Heartbeat (runs in a background thread) ───────────────────────────────────

def _heartbeat_loop() -> None:
    """
    Sends a heartbeat to the control plane every HEARTBEAT_SEC seconds.

    Runs in a daemon thread so it doesn't prevent process exit.
    On 401 (revoked token), logs the error and kills the whole process.

    Why a thread instead of async?
    The agent is synchronous (uses requests, not httpx/aiohttp). A daemon
    thread for heartbeating is simple and correct here — there's no event
    loop to share.
    """
    while True:
        time.sleep(HEARTBEAT_SEC)
        try:
            resp = requests.post(
                f"{CONTROL_URL}/v1/agent/heartbeat",
                json={"agent_version": AGENT_VERSION, "status": "idle"},
                headers={"Authorization": f"Bearer {_agent_token}"},
                timeout=5,
            )

            if resp.status_code == 200:
                logger.debug("Heartbeat OK")

            elif resp.status_code == 401:
                # Control plane has revoked this agent — must stop immediately.
                # This is the failure mode demonstrated in demo.sh.
                logger.error(
                    "HEARTBEAT 401 — agent token has been REVOKED. "
                    "Shutting down immediately."
                )
                # os._exit is intentional: we want to terminate the whole process
                # including the main poll loop, not just this thread.
                os._exit(1)

            else:
                logger.warning("Heartbeat returned HTTP %d", resp.status_code)

        except Exception as exc:
            # Network blips are non-fatal for heartbeat — control plane will
            # mark agent offline after 90s of silence, but we keep trying.
            logger.warning("Heartbeat error: %s", exc)


def start_heartbeat() -> None:
    """Start the heartbeat daemon thread."""
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    t.start()
    logger.info("Heartbeat thread started (interval=%ds)", HEARTBEAT_SEC)


# ── SQL Execution ─────────────────────────────────────────────────────────────

def execute_query(sql: str, timeout_ms: int) -> dict:
    """
    Execute SQL against the local DuckDB and return a result dict.

    DuckDB runs in-process — no network, no credentials, no attack surface
    beyond the SQL string itself. The SQL came from the control plane via
    the official job protocol; the agent does not accept SQL from any
    other source.

    Returns a dict matching JobResultRequest schema.
    """
    start_ms = int(time.time() * 1000)

    try:
        con = duckdb.connect(DB_PATH, read_only=False)

        # DuckDB doesn't have a native query timeout, so we set a memory limit
        # and rely on the result-size check below as a safety valve.
        # In production you'd run the query in a subprocess with a wall-clock
        # timeout enforced by the OS.
        con.execute(f"SET memory_limit='{256}MB'")

        result = con.execute(sql).fetchall()
        columns = [desc[0] for desc in con.description]
        con.close()

        elapsed_ms = int(time.time() * 1000) - start_ms

        # Check timeout — if execution took longer than allowed, mark as error
        if elapsed_ms > timeout_ms:
            return {
                "status":        "error",
                "error_code":    "QUERY_TIMEOUT",
                "error_message": f"Query exceeded {timeout_ms}ms limit (took {elapsed_ms}ms)",
                "execution_ms":  elapsed_ms,
            }

        rows = [dict(zip(columns, row)) for row in result]
        
        # Convert Decimal and other non-JSON-serializable types to JSON-compatible types
        rows = _convert_to_json_serializable(rows)

        # Check result size before posting
        serialised = json.dumps(rows)
        truncated  = False

        if len(serialised.encode()) > MAX_RESULT_BYTES:
            # Truncate rows until we're under the limit
            while rows and len(json.dumps(rows).encode()) > MAX_RESULT_BYTES:
                rows = rows[: len(rows) // 2]
            truncated = True
            logger.warning(
                "Result truncated to %d rows (exceeded 1MB limit)", len(rows)
            )

        return {
            "status":       "success",
            "rows":          rows,
            "row_count":    len(rows),
            "execution_ms": elapsed_ms,
            "truncated":    truncated,
        }

    except Exception as exc:
        elapsed_ms = int(time.time() * 1000) - start_ms
        logger.error("Query execution failed: %s", exc)
        return {
            "status":        "error",
            "error_code":    "EXECUTION_ERROR",
            # Scrub error messages — don't leak schema/credential details
            # to the control plane. In production you'd sanitise more aggressively.
            "error_message": str(exc)[:500],
            "execution_ms":  elapsed_ms,
        }


# ── Result posting ────────────────────────────────────────────────────────────

def post_result(job_id: str, result: dict) -> None:
    """
    POST the job result back to the control plane.

    Retries once on network failure (idempotency guard on the server
    means a duplicate POST returns 409, which we treat as success).
    """
    for attempt in range(1, 4):  # max 3 attempts
        try:
            resp = requests.post(
                f"{CONTROL_URL}/v1/agent/jobs/{job_id}/result",
                json=result,
                headers={"Authorization": f"Bearer {_agent_token}"},
                timeout=15,
            )

            if resp.status_code == 200:
                logger.info("Result posted for job %s", job_id)
                return

            elif resp.status_code == 409:
                # Already submitted — idempotency guard fired. Not an error.
                logger.info("Job %s result already submitted (409), skipping", job_id)
                return

            elif resp.status_code == 401:
                logger.error("401 posting result — token revoked. Shutting down.")
                os._exit(1)

            else:
                logger.warning(
                    "Result post attempt %d failed (HTTP %d): %s",
                    attempt, resp.status_code, resp.text
                )

        except requests.exceptions.RequestException as exc:
            logger.warning("Result post attempt %d error: %s", attempt, exc)

        time.sleep(2 * attempt)  # simple backoff: 2s, 4s

    logger.error("Failed to post result for job %s after 3 attempts", job_id)


# ── Main poll loop ────────────────────────────────────────────────────────────

def poll_loop() -> None:
    """
    Main job polling loop. Runs forever until the process exits.

    GET /v1/agent/jobs/next with long-poll timeout:
    - 200: job available — execute and post result
    - 204: no job within timeout — poll again immediately
    - 401: revoked — exit
    - Other errors: backoff and retry
    """
    logger.info("Poll loop started (long-poll timeout=%ds)", POLL_TIMEOUT)

    consecutive_errors = 0

    while True:
        try:
            resp = requests.get(
                f"{CONTROL_URL}/v1/agent/jobs/next",
                params={"timeout": POLL_TIMEOUT},
                headers={"Authorization": f"Bearer {_agent_token}"},
                # Client timeout must be longer than the server's long-poll timeout
                # to avoid the client cutting the connection before the server responds.
                timeout=POLL_TIMEOUT + 5,
            )

            consecutive_errors = 0  # reset on any successful HTTP response

            if resp.status_code == 200:
                job = resp.json()
                logger.info(
                    "Received job %s for dashboard %s",
                    job["job_id"], job["dashboard_id"]
                )

                # Execute the SQL locally against DuckDB
                result = execute_query(job["sql"], job.get("timeout_ms", 30000))

                # Post the result back — warehouse data travels to control plane
                # only as a result set, never as credentials or raw warehouse access
                post_result(job["job_id"], result)

            elif resp.status_code == 204:
                # No job available — loop immediately, the server already waited
                logger.debug("No job available (204), polling again")

            elif resp.status_code == 401:
                # Token revoked — must stop. Do not retry.
                logger.error(
                    "POLL 401 — agent token has been REVOKED. "
                    "Shutting down immediately."
                )
                sys.exit(1)

            elif resp.status_code == 429:
                # Rate limited — back off
                retry_after = int(resp.headers.get("Retry-After", "5"))
                logger.warning("Rate limited, backing off %ds", retry_after)
                time.sleep(retry_after)

            else:
                logger.warning("Unexpected status from poll: HTTP %d", resp.status_code)
                time.sleep(5)

        except requests.exceptions.ConnectionError:
            consecutive_errors += 1
            backoff = min(30, 2 ** consecutive_errors)
            logger.warning(
                "Connection error polling for jobs (attempt %d), "
                "retrying in %ds...", consecutive_errors, backoff
            )
            time.sleep(backoff)

        except Exception as exc:
            consecutive_errors += 1
            logger.error("Unexpected poll error: %s", exc, exc_info=True)
            time.sleep(5)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(
        "tinybrain-agent v%s starting | tenant=%s | db=%s",
        AGENT_VERSION, TENANT_ID, DB_PATH
    )

    # Step 1: Seed the local DuckDB with synthetic warehouse data
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    seed_database(DB_PATH)

    # Step 2: Enroll with the control plane
    enroll()

    # Step 3: Start heartbeat in background thread
    start_heartbeat()

    # Step 4: Run the job poll loop (blocks forever)
    poll_loop()
