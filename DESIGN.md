# Tinybrain — Architecture & Design

**Author:** Platform Engineering Candidate  
**Date:** 2025  
**Section:** 1 of 2 (Design Document)

---

## 1.1 Architecture Diagram & Request Walkthrough

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     CONTROL PLANE  (SaaS · Databrain-managed)               │
│                                                                             │
│  ┌───────────────┐    ┌──────────────────┐    ┌──────────────────────────┐ │
│  │  REST API      │    │  Agent Protocol  │    │  Postgres                │ │
│  │               │    │  Layer           │    │                          │ │
│  │ POST /tenants │    │ POST /enroll     │    │  tenants                 │ │
│  │ POST /dash..  │    │ GET  /jobs/next  │    │  dashboards              │ │
│  │ GET  /dash/:id│    │ POST /jobs/:id/  │    │  agents                  │ │
│  │    /data      │    │      result      │    │  jobs                    │ │
│  │               │    │ POST /heartbeat  │    │  results                 │ │
│  └───────┬───────┘    └────────┬─────────┘    └──────────────────────────┘ │
│          │                     │                          ▲                 │
│          └─────────────────────┴──────────────────────────┘                 │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Scheduler (background thread)                                       │  │
│  │  Reads dashboards with refresh_interval, enqueues jobs in Postgres   │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                      ▲                           ▲
                      │  HTTPS (outbound only)    │  HTTPS (outbound only)
                      │  from data plane →        │  from data plane →
                      │                           │
┌─────────────────────┴──────────┐   ┌────────────┴───────────────────────────┐
│  DATA PLANE — Tenant A         │   │  DATA PLANE — Tenant B                 │
│  (customer-hosted)             │   │  (customer-hosted)                     │
│                                │   │                                        │
│  ┌──────────────────────────┐  │   │  ┌──────────────────────────┐          │
│  │  tinybrain-agent         │  │   │  │  tinybrain-agent         │          │
│  │  - enrolls once          │  │   │  │  - enrolls once          │          │
│  │  - heartbeats every 30s  │  │   │  │  - heartbeats every 30s  │          │
│  │  - long-polls for jobs   │  │   │  │  - long-polls for jobs   │          │
│  │  - executes SQL locally  │  │   │  │  - executes SQL locally  │          │
│  │  - posts results back    │  │   │  │  - posts results back    │          │
│  └─────────────┬────────────┘  │   │  └─────────────┬────────────┘          │
│                │                │   │                │                       │
│  ┌─────────────▼────────────┐  │   │  ┌─────────────▼────────────┐          │
│  │  DuckDB (Tenant A data)  │  │   │  │  DuckDB (Tenant B data)  │          │
│  │  Warehouse credentials   │  │   │  │  Warehouse credentials   │          │
│  │  never leave this box    │  │   │  │  never leave this box    │          │
│  └──────────────────────────┘  │   │  └──────────────────────────┘          │
└────────────────────────────────┘   └────────────────────────────────────────┘

Browser / embed caller
  └─→ GET /v1/dashboards/:id/data  →  Control plane  →  returns latest result
       (session owned by control plane; result was pre-computed by agent)
```

### End-to-end request walkthrough

A user loads an embedded dashboard in their browser. Here is the precise sequence of events:

1. **Browser → Control plane.** The browser (or host SaaS app) calls `GET /v1/dashboards/:id/data`. The control plane owns the session and validates the caller's API key. The warehouse and its credentials are never involved in this step.

2. **Control plane reads from Postgres.** The control plane queries the `results` table for the most recent successful result for this `dashboard_id`. SQL was composed in an earlier background cycle (see step 4 below) — not on this request path. The control plane returns the stored result immediately as JSON.

3. **Pixels appear.** The browser renders the result. Latency is purely network + Postgres read. No warehouse query happens during embedding.

**How the result got there (background path):**

4. **Scheduler enqueues a job.** A background thread on the control plane reads all dashboards whose `next_run_at <= now()`. For each, it inserts a row into the `jobs` table with `status = pending` and the dashboard's compiled SQL.

5. **Agent polls.** The agent is running inside the customer's network, making outbound HTTPS requests to `GET /v1/agent/jobs/next`. The control plane holds the connection open up to 30 seconds (long-poll). When a job exists for this agent's tenant, it returns immediately.

6. **Tenant isolation enforced.** The control plane resolves the agent's bearer token → `agent_id` → `tenant_id`. The job query is `WHERE tenant_id = $agent_tenant_id AND status = 'pending'`. An agent can never receive a job belonging to another tenant.

7. **Agent executes SQL locally.** The agent runs the job's SQL against its local DuckDB instance. Warehouse credentials never leave the customer's network. The control plane never sees raw data — only the result set.

8. **Agent posts result.** `POST /v1/agent/jobs/:id/result` sends the JSON result back. The control plane writes it to the `results` table keyed by `dashboard_id`. The embedding endpoint now serves this result.

**Component ownership summary:**
- Session: control plane
- Warehouse credentials: data plane only
- SQL composition: control plane (stored with the dashboard definition)
- SQL execution: data plane (inside the agent, against DuckDB)
- Result between execution and rendering: Postgres on the control plane (`results` table)

---

## 1.2 The Boundary

### Ownership table

| Concern | Owner | Why |
|---|---|---|
| Dashboard definitions (name, SQL, refresh interval) | **Control** | Databrain-authored config, no customer data involved. Central source of truth. |
| Warehouse connection credentials | **Data** | Must never leave the customer's network. The control plane is stateless about these. |
| Compiled SQL for a given dashboard | **Control** | SQL is authored by Databrain / the tenant admin and stored with the dashboard definition. The agent receives it as part of the job payload. |
| Raw query results | **Data** (transiently) | The agent holds raw results for the duration of the POST. Once delivered, they live in the control plane's results store — but only as the most recent snapshot, not a full history. |
| Cached / aggregated query results | **Control** | The control plane stores the latest result per dashboard in Postgres. This is the embedding endpoint's source of truth. |
| User accounts and SSO | **Control** | Identity belongs to the SaaS layer. The data plane has no concept of end-users. |
| Audit log of who viewed which dashboard | **Control** | Access events are generated when the embedding endpoint is called — no data plane involvement. |
| Audit log of which SQL ran against the warehouse | **Both** | The control plane records what SQL was dispatched (job log). The data plane records what actually executed. Both logs are required for a complete audit trail. |
| Per-dashboard query latency metrics | **Both** | The agent measures execution time and reports it in the job result. The control plane stores and aggregates it. |
| Per-dashboard query error messages | **Both** | The agent captures the error and posts it back. The control plane stores it so the embedding endpoint can surface it. Error messages must be scrubbed of credential or schema details before leaving the data plane. |
| Agent version and uptime | **Control** | Reported via heartbeat. The control plane tracks this for observability and upgrade targeting. |

### What the control plane is forbidden from knowing or doing

These are hard security invariants. If we ever detect the control plane doing any of these in production, it is treated as a security incident requiring immediate incident response:

- **Storing warehouse credentials.** The control plane must never receive, store, or log connection strings, passwords, API keys, or any credential used to access a customer's data warehouse. Enrollment flow must not ask for them.
- **Opening a connection toward the data plane.** The control plane has no IP address, hostname, or port for any agent. All connectivity is initiated by the agent. Violating this breaks the firewall model and exposes customers to inbound network risk.
- **Executing SQL against customer data.** The control plane composes SQL but never runs it. Execution is the data plane's exclusive responsibility.
- **Accessing one tenant's data on behalf of another.** Every database query on the control plane that touches jobs, results, dashboards, or agents must be scoped by `tenant_id`. A cross-tenant data leak from the control plane is a P0 security incident regardless of cause.
- **Retaining raw row-level data from customer warehouses beyond the single latest result per dashboard.** The control plane is a result cache, not a data lake. Storing query history or multiple result versions creates a data residency liability that customers did not consent to.

---

## 1.3 Protocol Sketch

All communication is HTTPS. Authentication uses `Authorization: Bearer <token>` on every agent-facing request. The control plane never opens a connection to the agent.

### Enrollment — proving identity the first time

Enrollment converts a one-time token (issued at tenant creation) into a long-lived agent bearer token.

**Precondition:** A tenant has been created via `POST /v1/tenants`, which returns a single-use `enrollment_token`.

```
POST /v1/agent/enroll
Authorization: Bearer <enrollment_token>

Request body:
{
  "agent_version": "0.1.0",
  "hostname":      "prod-agent-acme-1"   // informational only
}

Response 200:
{
  "agent_id":    "agt_a1b2c3d4",
  "agent_token": "tk_live_xxxxxxxxxxxx",  // long-lived; store securely
  "tenant_id":   "tnt_xyz789"
}

Response 401: enrollment_token already used or expired
Response 404: enrollment_token not found
```

**Implementation notes:**
- `enrollment_token` is a UUID stored in the `tenants` table with `enrolled_at = NULL`. On use, set `enrolled_at = now()`. Subsequent calls with the same token return 401.
- `agent_token` is a randomly generated 32-byte hex string stored (hashed with SHA-256) in the `agents` table. The plaintext is returned once and never stored.
- One enrollment token → one agent. To replace a compromised agent, an admin creates a new enrollment token for the tenant.

---

### Ongoing auth — every subsequent request

Every request after enrollment uses `Authorization: Bearer <agent_token>`. The control plane:
1. Looks up the SHA-256 hash of the provided token in the `agents` table.
2. Checks `revoked_at IS NULL`.
3. Resolves `tenant_id` from the agent record. All subsequent DB queries are scoped to this `tenant_id`.

**Revoking a compromised agent:**

```
POST /v1/admin/agents/:agent_id/revoke
Authorization: Bearer <admin_api_key>

Response 200: { "revoked_at": "2025-06-10T14:22:00Z" }
```

Once revoked, all subsequent requests from that agent return `401 Unauthorized`. The agent's poll loop must handle 401 by stopping (not retrying with exponential backoff, which would only spam the endpoint).

---

### Job dispatch — outbound-only, long-poll

The agent discovers work by polling. The control plane never pushes.

```
GET /v1/agent/jobs/next
Authorization: Bearer <agent_token>

Query params:
  ?timeout=25   // seconds to hold connection (server-side long-poll)

Response 200 (job available):
{
  "job_id":       "job_cc3f1a22",
  "dashboard_id": "dsh_9f2e3b",
  "sql":          "SELECT region, SUM(revenue) FROM sales GROUP BY region",
  "timeout_ms":   30000    // agent must abandon execution after this
}

Response 204: no jobs pending within timeout window (agent should poll again immediately)
Response 401: token revoked or invalid — agent must stop
Response 429: agent is polling too fast — back off for retry_after seconds
```

**Implementation notes:**
- The control plane holds the HTTP connection open for up to `timeout` seconds using a polling loop against the `jobs` table (`SELECT ... WHERE tenant_id = $1 AND status = 'pending' LIMIT 1 FOR UPDATE SKIP LOCKED`). `FOR UPDATE SKIP LOCKED` prevents two agents (if a tenant ever runs multiple) from claiming the same job.
- On claiming a job, set `status = 'running'`, `claimed_by = agent_id`, `claimed_at = now()`.
- The scheduler also runs a stale-job reaper: any job in `status = 'running'` for more than `timeout_ms + 10s` is reset to `pending` for retry.

---

### Result delivery — posting back a completed job

```
POST /v1/agent/jobs/:job_id/result
Authorization: Bearer <agent_token>

Request body (success):
{
  "status":        "success",
  "rows":          [ {"region": "North", "revenue": 142000}, ... ],
  "row_count":     12,
  "execution_ms":  340,
  "truncated":     false
}

Request body (failure):
{
  "status":        "error",
  "error_code":    "QUERY_TIMEOUT",
  "error_message": "Query exceeded 30000ms limit",
  "execution_ms":  30001
}

Response 200: { "accepted": true }
Response 400: job_id does not belong to this agent's tenant
Response 409: result already submitted for this job_id (idempotency guard)
```

**Size limit:** Result body must not exceed 1 MB. If the agent detects the serialised result exceeds this threshold before posting, it truncates rows and sets `"truncated": true`. The control plane enforces this limit at the HTTP layer (`Content-Length` check) and returns `413` if exceeded.

**Idempotency:** The control plane stores `job_id` on the `results` row. A duplicate POST for the same `job_id` returns 409. This guards against agent retries on network failure.

---

### Heartbeat / liveness

```
POST /v1/agent/heartbeat
Authorization: Bearer <agent_token>

Request body:
{
  "agent_version": "0.1.0",
  "status":        "idle",   // or "running" if executing a job
  "job_id":        null      // or current job_id if running
}

Response 200: { "ok": true }
Response 401: revoked — agent must stop
```

The agent sends a heartbeat every 30 seconds. The control plane updates `agents.last_seen_at`. Any agent with `last_seen_at < now() - 90s` is considered offline. The control plane does not act on this automatically — it is surfaced as an observability signal only.

---

## 1.4 Migration from Self-Hosted — Acme Corp

**Acme's situation:** `tinybrain-monolith v0.9.0`, fully self-hosted in their AWS account. 40 dashboards, 200 internal users, 12 external customers consuming embedded analytics. The embedded analytics are revenue-generating — any visible outage affects Acme's customers, not just their internal team. This is the most important risk to manage.

---

### Phase 0 — Discovery (before touching anything)

We do not assume we understand Acme's setup. We verify it.

- Inventory all 40 dashboards: which are used by internal users vs. the 12 external customers? Which have SLAs attached? External-facing dashboards are the last to migrate, not the first.
- Identify the warehouse type and connection topology. Is the warehouse in the same AWS VPC as the monolith? The agent will need outbound HTTPS to our control plane — confirm no egress restrictions block this.
- Capture current refresh intervals and p95 query latency per dashboard. These become our acceptance criteria for the migration — if the new stack is slower, we don't cut over.
- Identify Acme's auth mechanism. Are the 200 internal users on SSO? We need to know if user accounts need to be migrated or if they'll be re-invited.
- Agree on a maintenance window for Phase 2 cutover. External-customer-facing dashboards get a window during their lowest-traffic period (check Acme's analytics).

---

### Phase 1 — Coexistence (old and new run side by side)

Goal: the new architecture serves non-critical dashboards while the monolith continues to serve everything else.

1. **Deploy the agent** inside Acme's AWS VPC. It connects outbound to the Databrain control plane. No inbound firewall rules required. Acme's security team reviews the agent binary and network policy before deployment.
2. **Migrate internal-only dashboards first.** Pick 5 low-traffic internal dashboards. Re-create them in the control plane, let the agent start serving them. Acme's team compares results against the monolith for 48 hours. If results match and latency is acceptable, proceed.
3. **Migrate remaining internal dashboards in batches of 10.** Each batch runs in shadow mode: the new stack produces results, but the monolith is still the source of truth for users. Acme's team spot-checks. Shadow mode is easy to implement — just don't update the embed URL yet.
4. **Do not touch external-customer-facing dashboards during Phase 1.** They remain 100% on the monolith.

Rollback at any point in Phase 1: the monolith is untouched. Flip the embed URL back. Zero impact.

---

### Phase 2 — Cutover and rollback

1. **Pre-cutover checklist:** All 40 dashboards defined in the control plane. Agent healthy and producing matching results. Rollback plan reviewed with Acme's on-call team. Maintenance window confirmed with Acme's external customers (brief "scheduled maintenance" banner).
2. **Cutover sequence (during window):**
   - Pause the monolith's dashboard refresh jobs (stop new queries being fired at the warehouse from the old system).
   - Update embed URLs / API keys to point to the control plane's `GET /v1/dashboards/:id/data` endpoint.
   - Verify the first 3 external-customer dashboards load correctly.
   - Roll out to all external-customer dashboards.
   - Total window target: 30 minutes.
3. **Rollback trigger:** If any external-customer dashboard fails to load correct data within the first 5 minutes of cutover, revert embed URLs to the monolith immediately. The monolith is still running and has not been touched — rollback is a URL swap, not a data restoration.
4. **Post-cutover monitoring (24 hours):** Watch query latency, error rates, and heartbeat health in the control plane. Acme's support channel is on standby.

---

### Phase 3 — Decommission

Only after 2 weeks of stable operation on the new stack with no rollback events:

1. Export Acme's dashboard definitions and results from the monolith as a final snapshot (archival only).
2. Shut down the monolith service. Do not delete it yet.
3. After 30 days with no incidents, terminate the monolith EC2 instances and revoke its IAM roles.
4. Communicate closure to Acme: confirm the migration is complete, share the new observability dashboard showing agent health and query latency.

**The principle throughout:** we treat Acme's external customers as our own. Any step that risks them is the last step, not the first.

---

*This document is the source of truth for the system built in Section 2. If the implementation diverges from this design, this document is updated to reflect what was actually built, with a note explaining why.*
