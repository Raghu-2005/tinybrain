# Tinybrain

A working implementation of a data-plane / control-plane split architecture.

The control plane is a SaaS API that owns dashboard definitions, tenant accounts, job scheduling, and cached results. The data plane is a lightweight agent that runs inside the customer's network, polls for jobs, executes SQL against a local database, and posts results back. The control plane never connects to the agent. Warehouse credentials never leave the customer's network.

---

## How it works

A user loads an embedded dashboard. The browser calls the control plane, which reads a pre-computed result from its database and returns it instantly. No warehouse query happens on the user-facing request path.

In the background, a scheduler creates jobs for each dashboard based on its refresh interval. The agent running in the customer's network picks up jobs by polling the control plane, runs the SQL locally against DuckDB, and posts the result back. The next time someone loads the dashboard, the fresh result is already there.

Tenant isolation is enforced at the database level. Every job query has `WHERE tenant_id = agent's own tenant`. An agent from a different tenant gets a 204 response — the job simply does not exist from their perspective.

---

## Requirements

You need these installed before running anything:

- **Docker Desktop** — https://www.docker.com/products/docker-desktop  
  After installing, open it and wait for the whale icon in the system tray to stop animating. Docker Desktop must be running before you use any docker commands.

- **Git** — https://git-scm.com  
  On Windows, install with Git Bash included.

- **Python 3** — https://www.python.org  
  Used by the demo script to parse JSON responses. Version 3.8 or higher.

- **curl** — pre-installed on Mac and Linux. On Windows it comes with Git Bash.

---

## Getting started

Clone the repository and move into the folder:

```bash
git clone https://github.com/Raghu-2005/tinybrain.git
cd tinybrain
```

Build and start the entire stack:

```bash
docker compose up -d --build
```

This starts four containers — Postgres, the control plane, and two agent containers representing two different tenants. The first run takes 3 to 5 minutes because Docker downloads base images and installs dependencies. Subsequent runs use cached layers and take about 10 seconds.

Wait for everything to be ready:

```bash
docker compose ps
```

You should see all four containers running:

```
NAME                    STATUS
tinybrain-postgres-1    healthy
tinybrain-control-1     running
tinybrain-agent-a-1     running
tinybrain-agent-b-1     running
```

Confirm the API is live:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status": "ok", "service": "tinybrain-control"}
```

---

## Running the demo

The demo script runs the full end-to-end test. It creates two tenants, enrolls both agents, defines a dashboard for each tenant, waits for results to populate, reads the embedding endpoints, proves tenant isolation, and demonstrates token revocation.

```bash
chmod +x demo.sh
./demo.sh
```

On Windows, run this in Git Bash, not PowerShell or CMD.

The script takes about 60 seconds to complete. Every line should show a green checkmark. The final output should be:

```
PASSED: 14
All checks passed.
```

The two most important checks to look for:

```
✓ PASS  ISOLATION CONFIRMED — Got 204: Tenant A job invisible to different tenant
✓ PASS  POST-REVOKE heartbeat → 401 Unauthorized
```

---

## API endpoints

The control plane runs at `http://localhost:8000`. You can browse all endpoints at `http://localhost:8000/docs`.

**Create a tenant:**
```bash
curl -X POST http://localhost:8000/v1/tenants \
  -H "Content-Type: application/json" \
  -d '{"name": "My Company"}'
```

**Create a dashboard:**
```bash
curl -X POST http://localhost:8000/v1/dashboards \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tnt_...",
    "name": "Revenue by Region",
    "sql": "SELECT region, SUM(revenue) FROM sales GROUP BY region",
    "refresh_interval": 30
  }'
```

**Read dashboard results (embedding endpoint):**
```bash
curl http://localhost:8000/v1/dashboards/dsh_.../data
```

**Revoke an agent:**
```bash
curl -X POST http://localhost:8000/v1/admin/agents/agt_.../revoke \
  -H "X-Admin-Key: dev-admin-key"
```

---

## Project structure

```
tinybrain/
├── DESIGN.md                    Architecture design document (Section 1)
├── README.md                    This file
├── docker-compose.yml           Stack orchestration
├── demo.sh                      End-to-end demo and test script
│
├── control/                     Control plane (SaaS API)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                  FastAPI entry point and startup
│   ├── db.py                    Postgres connection pool and schema
│   ├── auth.py                  Token generation and SHA-256 hashing
│   ├── scheduler.py             Background job enqueuer and stale-job reaper
│   ├── models.py                Pydantic request and response models
│   └── routes/
│       ├── tenants.py           POST /v1/tenants
│       ├── dashboards.py        POST /v1/dashboards, GET /v1/dashboards/:id/data
│       └── agent.py             Enroll, heartbeat, job poll, result, revoke
│
└── agent/                       Data plane (customer-hosted worker)
    ├── Dockerfile
    ├── requirements.txt
    ├── agent.py                 Main worker loop
    └── seed.py                  Seeds DuckDB with synthetic warehouse data
```

---

## Stopping the stack

Stop all containers but keep the database data:

```bash
docker compose down
```

Stop everything and delete all data for a clean reset:

```bash
docker compose down -v
```

After a full reset, run `docker compose up -d --build` followed by `./demo.sh` to start fresh.

---
