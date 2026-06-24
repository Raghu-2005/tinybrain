"""
seed.py — Seed the local DuckDB with synthetic warehouse data.

Each agent container gets a different TENANT_ID environment variable,
which drives which dataset gets loaded. This simulates two customers
having completely different warehouses with different schemas and data.

DuckDB is used as an in-process warehouse substitute:
- No separate process or port needed
- Full SQL support (aggregations, window functions, joins)
- Reads/writes a single .db file on disk
- Perfect stand-in for a real warehouse at demo scale
"""

import duckdb
import os
import logging

logger = logging.getLogger(__name__)


def seed_database(db_path: str) -> None:
    """
    Create and populate tables appropriate for this tenant's 'warehouse'.
    The TENANT_ID env var selects which synthetic dataset to load.
    """
    tenant_id = os.environ.get("TENANT_ID", "unknown")
    con = duckdb.connect(db_path)

    # Tenant A simulates a retail company with sales data
    if tenant_id.startswith("tnt_a") or os.environ.get("TENANT_SEED") == "A":
        _seed_tenant_a(con)

    # Tenant B simulates a SaaS company with subscription/MRR data
    elif tenant_id.startswith("tnt_b") or os.environ.get("TENANT_SEED") == "B":
        _seed_tenant_b(con)

    else:
        # Fallback: generic seed so the agent always has something to query
        _seed_generic(con)

    con.close()
    logger.info("DuckDB seeded at %s for tenant %s", db_path, tenant_id)


def _seed_tenant_a(con: duckdb.DuckDBPyConnection) -> None:
    """Tenant A — retail sales data"""
    con.execute("DROP TABLE IF EXISTS sales")
    con.execute("""
        CREATE TABLE sales (
            order_id    INTEGER,
            region      VARCHAR,
            product     VARCHAR,
            revenue     DECIMAL(10,2),
            units       INTEGER,
            order_date  DATE
        )
    """)
    con.execute("""
        INSERT INTO sales VALUES
            (1,  'North', 'Widget Pro',   1200.00, 4, '2024-01-05'),
            (2,  'South', 'Widget Lite',   450.00, 3, '2024-01-08'),
            (3,  'East',  'Widget Pro',   2100.00, 7, '2024-01-12'),
            (4,  'West',  'Gadget X',     3400.00, 5, '2024-01-15'),
            (5,  'North', 'Gadget X',     1800.00, 3, '2024-01-20'),
            (6,  'South', 'Widget Pro',   2750.00, 9, '2024-02-01'),
            (7,  'East',  'Widget Lite',   600.00, 4, '2024-02-05'),
            (8,  'West',  'Widget Pro',   1950.00, 6, '2024-02-10'),
            (9,  'North', 'Gadget X',     4200.00, 7, '2024-02-14'),
            (10, 'South', 'Widget Lite',   900.00, 6, '2024-02-20')
    """)
    logger.info("Tenant A: seeded 'sales' table with 10 rows")


def _seed_tenant_b(con: duckdb.DuckDBPyConnection) -> None:
    """Tenant B — SaaS subscription / MRR data"""
    con.execute("DROP TABLE IF EXISTS subscriptions")
    con.execute("""
        CREATE TABLE subscriptions (
            customer_id   INTEGER,
            plan          VARCHAR,
            mrr           DECIMAL(10,2),
            status        VARCHAR,
            started_at    DATE,
            churned_at    DATE
        )
    """)
    con.execute("""
        INSERT INTO subscriptions VALUES
            (101, 'starter',      49.00, 'active',   '2023-06-01', NULL),
            (102, 'growth',      199.00, 'active',   '2023-07-15', NULL),
            (103, 'enterprise',  999.00, 'active',   '2023-08-01', NULL),
            (104, 'starter',      49.00, 'churned',  '2023-09-01', '2024-01-01'),
            (105, 'growth',      199.00, 'active',   '2023-10-10', NULL),
            (106, 'starter',      49.00, 'active',   '2023-11-01', NULL),
            (107, 'enterprise',  999.00, 'active',   '2024-01-05', NULL),
            (108, 'growth',      199.00, 'churned',  '2024-01-10', '2024-04-01'),
            (109, 'starter',      49.00, 'active',   '2024-02-01', NULL),
            (110, 'growth',      199.00, 'active',   '2024-03-01', NULL)
    """)
    logger.info("Tenant B: seeded 'subscriptions' table with 10 rows")


def _seed_generic(con: duckdb.DuckDBPyConnection) -> None:
    """Fallback seed for unknown tenants"""
    con.execute("DROP TABLE IF EXISTS metrics")
    con.execute("CREATE TABLE metrics (name VARCHAR, value INTEGER)")
    con.execute("INSERT INTO metrics VALUES ('requests', 1000), ('errors', 5)")
    logger.info("Generic seed applied")
