# Tinybrain — Architecture & Design

## 1.1 Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    CONTROL PLANE  (SaaS · Databrain-managed)            │
│                                                                         │
│  ┌─────────────────┐   ┌──────────────────┐   ┌──────────────────────┐ │
│  │   REST API       │   │  Agent Protocol  │   │     PostgreSQL       │ │
│  │                 │   │                  │   │                      │ │
│  │ POST /tenants   │   │ POST /enroll     │   │  tenants             │ │
│  │ POST /dashboards│   │ POST /heartbeat  │   │  agents              │ │
│  │ GET  /dash/:id  │   │ GET  /jobs/next  │   │  dashboards          │ │
│  │      /data      │   │ POST /jobs/:id/  │   │  jobs                │ │
│  │                 │   │       result     │   │  results             │ │
│  └────────┬────────┘   └────────┬─────────┘   └──────────────────────┘ │
│           │                     │                        ▲              │
│           └─────────────────────┴────────────────────────┘              │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  Scheduler (asyncio background task — runs every 5 seconds)      │   │
│  │  · Finds dashboards where next_run_at <= now()                   │   │
│  │  · Inserts pending job rows into jobs table                      │   │
│  │  · Resets stale running jobs back to pending (crash recovery)    │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
                ▲                                    ▲
                │  outbound HTTPS only               │  outbound HTTPS only
                │  (agent initiates)                 │  (agent initiates)
                │                                    │
┌───────────────┴──────────────┐    ┌────────────────┴─────────────────────┐
│  DATA PLANE — Tenant A       │    │  DATA PLANE — Tenant B               │
│  (customer-hosted)           │    │  (customer-hosted)                   │
│                              │    │                                      │
│  ┌────────────────────────┐  │    │  ┌────────────────────────┐          │
│  │  tinybrain-agent       │  │    │  │  tinybrain-agent       │          │
│  │                        │  │    │  │                        │          │
│  │  1. enroll() once      │  │    │  │  1. enroll() once      │          │
│  │  2. heartbeat thread   │  │    │  │  2. heartbeat thread   │          │
│  │  3. poll_loop forever  │  │    │  │  3. poll_loop forever  │          │
│  │  4. execute_query()    │  │    │  │  4. execute_query()    │          │
│  │  5. post_result()      │  │    │  │  5. post_result()      │          │
│  └──────────┬─────────────┘  │    │  └──────────┬─────────────┘          │
│             │                │    │             │                         │
│  ┌──────────▼─────────────┐  │    │  ┌──────────▼─────────────┐          │
│  │  DuckDB (Tenant A)     │  │    │  │  DuckDB (Tenant B)     │          │
│  │  sales table           │  │    │  │  subscriptions table   │          │
│  │  /data/warehouse.db    │  │    │  │  /data/warehouse.db    │          │
│  │                        │  │    │  │                        │          │
│  │  Credentials stay here │  │    │  │  Credentials stay here │          │
│  │  — never transmitted   │  │    │  │  — never transmitted   │          │
│  └────────────────────────┘  │    │  └────────────────────────┘          │
└──────────────────────────────┘    └──────────────────────────────────────┘

Browser / embed caller
  └─→ GET /v1/dashboards/:id/data  →  Control plane reads results table
       No warehouse query on this path. Result was pre-computed by the agent.
```

### End-to-end Walkthrough

A user loads an embedded dashboard. Here is the exact sequence:

**Embedding path (user-facing, fast):**

1. Browser calls `GET /v1/dashboards/:id/data` on the control plane.
2. Control plane validates the request, queries the `results` table in PostgreSQL for the most recent result for that `dashboard_id`.
3. Returns the stored JSON result immediately. No agent involved. No warehouse query. Latency is network + one Postgres read.

**Background path (scheduled, async):**

4. The scheduler wakes up every 5 seconds. It queries `dashboards WHERE next_run_at <= now()`, atomically advances `next_run_at` by `refresh_interval`, and inserts a new row into the `jobs` table with `status = pending`.
5. The agent is running inside the customer's network, making an outbound long-poll request: `GET /v1/agent/jobs/next?timeout=25`.
6. The control plane resolves the agent's bearer token → `agent_id` → `tenant_id`. The job query is `WHERE tenant_id = $agent_tenant_id AND status = 'pending' FOR UPDATE SKIP LOCKED`. The agent can only ever see jobs belonging to its own tenant.
7. The control plane claims the job (sets `status = running`, `claimed_by = agent_id`) and returns it to the agent.
8. The agent runs the SQL against local DuckDB. Warehouse credentials never leave the agent process.
9. The agent calls `POST /v1/agent/jobs/:id/result` with the result rows. The control plane writes to the `results` table (upsert by `dashboard_id`) and sets job `status = done`.
10. The next call to the embedding endpoint returns the fresh result.

**Component ownership:**
- Session ownership: control plane
- Warehouse credentials: data plane only, never transmitted
- SQL authoring: control plane (stored with the dashboard definition)
- SQL execution: data plane (agent, against local DuckDB)
- Result storage between execution and rendering: PostgreSQL `results` table on the control plane

---

## 1.2 The Boundary

### Ownership Table

| Concern | Owner | Reason |
|---|---|---|
| Dashboard definitions (name, SQL, refresh interval) | Control | Databrain-authored config. No customer data. Central source of truth. |
| Warehouse connection credentials | Data | Never leave the customer's network. Control plane has no knowledge of them. |
| Compiled SQL for a given dashboard | Control | SQL is authored and stored with the dashboard. Sent to the agent as part of the job payload. |
| Raw query results | Data (transiently) | The agent holds raw results briefly during execution. After posting, they live on the control plane as the latest result snapshot only. |
| Cached / aggregated query results | Control | Stored in the `results` table keyed by `dashboard_id`. The embedding endpoint reads this. |
| User accounts and SSO | Control | Identity belongs to the SaaS layer. The data plane has no concept of end-users. |
| Audit log of who viewed which dashboard | Control | Access events are generated when the embedding endpoint is called. No agent involvement. |
| Audit log of which SQL ran against the warehouse | Both | Control plane records what SQL was dispatched (job row). Agent records what actually executed (agent logs). Both are needed for a complete audit trail. |
| Per-dashboard query latency metrics | Both | Agent measures and reports `execution_ms` in the result payload. Control plane stores and exposes it. |
| Per-dashboard query error messages | Both | Agent captures the error and posts it back. Control plane stores and surfaces it on the embedding endpoint. |
| Agent version and uptime | Control | Reported via heartbeat. Stored as `last_seen_at` and `agent_version` on the `agents` table. |

### What the Control Plane is Forbidden From Knowing or Doing

These are hard security invariants. Violation of any of these in production is treated as a security incident:

- **It must never store, receive, or log warehouse credentials** of any kind — connection strings, passwords, API keys. The enrollment flow does not ask for them. They are not part of any request schema.
- **It must never open a connection toward the data plane.** The control plane holds no IP address, hostname, or port for any agent. All network connectivity is initiated by the agent. This is enforced architecturally — there is no outbound connection code in the control plane.
- **It must never execute SQL against customer data.** The control plane composes SQL and stores it. Execution is exclusively the agent's responsibility.
- **It must never return one tenant's data to another tenant's request.** Every database query touching jobs, results, dashboards, or agents is scoped by `tenant_id`. A cross-tenant data leak is a P0 security incident regardless of cause.
- **It must never retain a history of raw row-level warehouse data.** The `results` table stores one result per dashboard — the most recent. There is no historical result log. The control plane is a result cache, not a data warehouse.

---

## 1.3 Protocol

All communication is HTTPS. Agent authentication uses `Authorization: Bearer <token>` on every request after enrollment. The control plane never initiates connections to the agent.

### Enrollment — one-time token exchange

**Precondition:** A tenant has been created via `POST /v1/tenants`, which returns a single-use `enrollment_token` (UUID4).

```
POST /v1/agent/enroll
Authorization: Bearer <enrollment_token>
Content-Type: application/json

{
  "agent_version": "0.1.0",
  "hostname":      "prod-agent-acme-1"
}

Response 201:
{
  "agent_id":    "agt_a1b2c3d4...",
  "agent_token": "a3f9e2...64-char hex...",
  "tenant_id":   "tnt_xyz789..."
}

Response 401: token already used or not found
```

**Implementation:**
- `enrollment_token` is stored in plaintext on the `tenants` table. On use, `enrolled_at` is set and the token cannot be reused (`enrolled_at IS NULL` check in the query).
- `agent_token` is 32 bytes from `secrets.token_hex()` — 256 bits of entropy. Only the SHA-256 hash is stored in the `agents` table. The plaintext is returned once and never stored anywhere.
- The enrollment and agent creation are wrapped in a single database transaction — atomic. If either fails, neither happens.

### Ongoing Authentication

Every request after enrollment sends `Authorization: Bearer <agent_token>`. On each request the control plane:
1. Computes `SHA-256(incoming_token)` and looks it up in `agents.token_hash`.
2. Checks `revoked_at IS NULL`.
3. Derives `tenant_id` from the agent record. All subsequent queries are scoped to this `tenant_id`.

### Agent Revocation

```
POST /v1/admin/agents/:agent_id/revoke
X-Admin-Key: <admin_api_key>

Response 200:
{
  "agent_id":   "agt_...",
  "revoked_at": "2025-06-10T14:22:00Z"
}
```

After revocation, every request from that agent returns `401`. The agent process detects `401` on heartbeat or job poll and calls `os._exit(1)` — it stops immediately without retry.

### Job Dispatch — Long-Poll

```
GET /v1/agent/jobs/next?timeout=25
Authorization: Bearer <agent_token>

Response 200 (job available):
{
  "job_id":       "job_cc3f1a22...",
  "dashboard_id": "dsh_9f2e3b...",
  "sql":          "SELECT region, SUM(revenue) FROM sales GROUP BY region",
  "timeout_ms":   30000
}

Response 204: no job arrived within the timeout window
Response 401: token revoked — agent must stop
Response 429: polling too fast — back off
```

**Implementation:** The server holds the HTTP connection open for up to 25 seconds, checking the `jobs` table every 1 second using `asyncio.sleep()` (non-blocking). Job claim uses `SELECT ... FOR UPDATE SKIP LOCKED` — the Postgres-native job queue pattern. This prevents two agents from claiming the same job without application-level locking and without deadlocks.

### Result Delivery

```
POST /v1/agent/jobs/:job_id/result
Authorization: Bearer <agent_token>
Content-Type: application/json

Success:
{
  "status":       "success",
  "rows":         [ {"region": "North", "revenue": 142000}, ... ],
  "row_count":    12,
  "execution_ms": 340,
  "truncated":    false
}

Error:
{
  "status":        "error",
  "error_code":    "EXECUTION_ERROR",
  "error_message": "...",
  "execution_ms":  30001
}

Response 200: { "accepted": true }
Response 409: result already submitted for this job_id (idempotency guard)
Response 403: job does not belong to this agent's tenant
```

**Size limit:** 1 MB. If the serialised result exceeds this, the agent truncates rows and sets `truncated: true` before posting.

**Idempotency:** The control plane rejects duplicate submissions for the same `job_id` with `409`. This handles the case where the agent posts successfully but loses the network before receiving the `200` and retries.

### Heartbeat

```
POST /v1/agent/heartbeat
Authorization: Bearer <agent_token>

{ "agent_version": "0.1.0", "status": "idle" }

Response 200: { "ok": true }
Response 401: revoked — agent must stop immediately
```

The agent sends a heartbeat every 30 seconds. The control plane updates `agents.last_seen_at`. Agents with `last_seen_at > 90 seconds ago` are considered offline — this is surfaced as an observability signal but does not trigger automated action in this implementation.

---

## 1.4 Migration from Self-Hosted — Acme Corp

**Acme's situation:** `tinybrain-monolith v0.9.0`, fully self-hosted in their AWS account. 40 dashboards, 200 internal users, 12 external customers consuming embedded analytics via their app. The external customer dashboards are revenue-generating — any visible outage to Acme's end-users is unacceptable.

### Phase 0 — Discovery

Do not touch anything until we know the following:

- Which of the 40 dashboards are internal-only and which are embedded in Acme's customer-facing product? External-facing ones are last to migrate, not first.
- Current p95 query latency per dashboard. These become acceptance criteria — if the new stack is slower, we do not cut over.
- Warehouse topology: is the warehouse in the same VPC as the monolith? The agent needs outbound HTTPS to Databrain's control plane endpoint — confirm no egress proxy or firewall rule blocks this.
- Acme's SSO/auth setup for the 200 internal users. Do accounts need to be migrated or will users re-register?
- Agree on a maintenance window for the final cutover. External dashboards get a window during Acme's lowest-traffic period — checked against their own analytics.

### Phase 1 — Coexistence

Goal: the new architecture runs in parallel while the monolith continues serving all traffic.

1. Deploy `tinybrain-agent` inside Acme's AWS VPC. It makes outbound HTTPS connections to Databrain's control plane. No inbound firewall rules required. Acme's security team reviews the agent binary and network egress policy.
2. Migrate 5 internal-only, low-traffic dashboards first. Re-create them in the control plane. Run both stacks simultaneously — compare results from new vs. old for 48 hours.
3. If results match and latency is within SLA, migrate the remaining internal dashboards in batches of 10.
4. Do not touch the 12 external-customer dashboards during Phase 1. They stay 100% on the monolith.

Rollback at any point in Phase 1 is zero-risk — the monolith is untouched, embed URLs have not changed.

### Phase 2 — Cutover

1. Pre-cutover checklist: all 40 dashboards defined in the control plane, agent healthy, results matching, rollback plan reviewed, maintenance window confirmed with Acme.
2. Cutover sequence:
   - Pause the monolith's scheduler (stop it firing new warehouse queries).
   - Update embed URLs and API keys to point to `GET /v1/dashboards/:id/data`.
   - Verify the first 3 external-customer dashboards load correctly.
   - Roll out to all remaining dashboards.
   - Target window: 30 minutes.
3. Rollback trigger: if any external-customer dashboard returns incorrect data within the first 5 minutes, revert embed URLs to the monolith immediately. The monolith has not been decommissioned — rollback is a URL swap, not a data restore.

### Phase 3 — Decommission

Only after 14 days of stable operation with no rollback events:

1. Export a final snapshot of the monolith's dashboard definitions and result history for archival.
2. Stop the monolith service. Do not delete infrastructure yet.
3. After 30 further days with no incidents: terminate EC2 instances, revoke IAM roles, delete the old deployment.
4. Confirm closure to Acme with a summary of the migration and a link to the new agent health dashboard.

---

## Database Schema

Five tables, created automatically at control plane startup via `CREATE TABLE IF NOT EXISTS`:

```sql
tenants (
  id               TEXT PRIMARY KEY,   -- "tnt_<uuid4_hex>"
  name             TEXT NOT NULL,
  enrollment_token TEXT NOT NULL UNIQUE, -- plaintext UUID, single-use
  enrolled_at      TIMESTAMPTZ,          -- NULL until agent enrolls
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)

agents (
  id            TEXT PRIMARY KEY,       -- "agt_<uuid4_hex>"
  tenant_id     TEXT REFERENCES tenants(id),
  token_hash    TEXT NOT NULL UNIQUE,   -- SHA-256(plaintext_token)
  hostname      TEXT,
  agent_version TEXT,
  last_seen_at  TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ,            -- NULL = active
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)

dashboards (
  id               TEXT PRIMARY KEY,   -- "dsh_<uuid4_hex>"
  tenant_id        TEXT REFERENCES tenants(id),
  name             TEXT NOT NULL,
  sql              TEXT NOT NULL,
  refresh_interval INT  NOT NULL DEFAULT 60,  -- seconds
  next_run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)

jobs (
  id           TEXT PRIMARY KEY,       -- "job_<uuid4_hex>"
  tenant_id    TEXT REFERENCES tenants(id),
  dashboard_id TEXT REFERENCES dashboards(id),
  sql          TEXT NOT NULL,
  status       TEXT CHECK (status IN ('pending','running','done','failed')),
  claimed_by   TEXT REFERENCES agents(id),
  claimed_at   TIMESTAMPTZ,
  timeout_ms   INT  NOT NULL DEFAULT 30000,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)

results (
  dashboard_id  TEXT PRIMARY KEY REFERENCES dashboards(id),
  tenant_id     TEXT REFERENCES tenants(id),
  job_id        TEXT NOT NULL,         -- idempotency key
  status        TEXT NOT NULL,         -- "success" | "error"
  rows          JSONB,
  row_count     INT,
  execution_ms  INT,
  truncated     BOOLEAN DEFAULT false,
  error_code    TEXT,
  error_message TEXT,
  recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

Index on `jobs(tenant_id, status)` ensures the scheduler's pending-job query does not do a full table scan.

---

## Key Technical Decisions

**Why long-polling instead of WebSockets or a message queue?**

The requirement is that all traffic must be outbound from the data plane — no inbound firewall holes. Long-polling satisfies this with plain HTTPS. The agent makes a `GET` request and the server holds it open for up to 25 seconds waiting for a job. No persistent connection state, no broker to operate, no Kafka to manage. At this scale, HTTPS polling is the correct tool.

**Why SHA-256 for token storage instead of bcrypt?**

Agent tokens are generated with `secrets.token_hex(32)` — 256 bits of cryptographic randomness. There is no dictionary to attack. bcrypt's deliberate slowness (~150ms per hash) exists to resist brute-force on low-entropy secrets like passwords. Applying it to a 256-bit random token adds 150ms latency to every API request with zero security improvement. SHA-256 is correct here.

**Why asyncpg instead of psycopg2?**

FastAPI runs on asyncio. A synchronous database driver inside an `async def` route blocks the entire event loop during the query — the server becomes single-threaded under any database load. asyncpg is async-native, shares the event loop, and is approximately 3x faster than psycopg2 in benchmarks.

**Why `FOR UPDATE SKIP LOCKED` in the job claim query?**

This is the standard Postgres pattern for a job queue. When multiple agents for the same tenant race to claim a job, `SKIP LOCKED` causes each agent to skip rows that are already locked by another agent rather than waiting. No deadlocks. No application-level locking. No external queue infrastructure.

**Why the stale-job reaper?**

Without it, an agent crash mid-execution leaves the job permanently in `running` status. The scheduler reaper runs every 5 seconds and resets any job that has been in `running` for longer than 40 seconds back to `pending`. This gives the system automatic recovery from agent crashes with no manual intervention.

---
