"""
main.py — FastAPI application entry point for tinybrain-control

Application lifecycle:
1. lifespan() context manager runs at startup/shutdown
2. Startup: init DB pool → run schema → start scheduler background task
3. Shutdown: cancel scheduler → close DB pool

Why lifespan() instead of @app.on_event("startup")?
- @app.on_event is deprecated since FastAPI 0.93.
- lifespan() uses Python's contextlib.asynccontextmanager — the code
  before `yield` runs at startup, after `yield` runs at shutdown.
  This guarantees cleanup even if startup partially fails.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import init_db, close_db
from scheduler import scheduler_loop
from routes.tenants import router as tenants_router
from routes.dashboards import router as dashboards_router
from routes.agent import router as agent_router, admin_router

# Configure structured logging.
# In production you'd use JSON logging + a log aggregator (Datadog, Loki, etc.)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages the application lifecycle.

    Everything before `yield` runs at startup.
    Everything after `yield` runs at shutdown (even on crash).
    """
    logger.info("tinybrain-control starting up")

    # 1. Initialise database pool and run schema migrations
    await init_db()

    # 2. Launch scheduler as a background asyncio task.
    #    asyncio.create_task() schedules it on the running event loop.
    #    It runs concurrently with HTTP request handling.
    scheduler_task = asyncio.create_task(scheduler_loop())
    logger.info("Scheduler task started")

    yield  # Application is now running and serving requests

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("tinybrain-control shutting down")

    # Cancel the scheduler gracefully — scheduler_loop() catches CancelledError
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass

    # Close the DB pool (drains in-flight queries first)
    await close_db()
    logger.info("Shutdown complete")


app = FastAPI(
    title="tinybrain-control",
    description="Control plane for the Tinybrain data-plane/control-plane split",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: in production, restrict allow_origins to known embedding domains.
# For this exercise, allow all origins so curl and any browser work.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Route registration ────────────────────────────────────────────────────────
app.include_router(tenants_router,    prefix="/v1/tenants")
app.include_router(dashboards_router, prefix="/v1/dashboards")
app.include_router(agent_router,      prefix="/v1/agent")
app.include_router(admin_router,      prefix="/v1/admin")


@app.get("/health")
async def health():
    """
    Health check endpoint for Docker Compose healthcheck and load balancers.
    Returns 200 as long as the process is running.
    In production you'd also check DB connectivity here.
    """
    return {"status": "ok", "service": "tinybrain-control"}
