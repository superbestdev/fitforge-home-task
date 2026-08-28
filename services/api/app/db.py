"""Postgres access.

A single connection pool shared by the API, the agent tools and the ingest
worker. psycopg3 with dict rows: the tool layer hands plain dicts to Pydantic
models, so there is no ORM in the path between the catalog and the agent.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=2,
            max_size=12,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn


def query(sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> list[dict]:
    """Run a SELECT and return all rows as dicts."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(
    sql: str, params: Sequence[Any] | dict[str, Any] | None = None
) -> dict | None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(
    sql: str, params: Sequence[Any] | dict[str, Any] | None = None
) -> dict | None:
    """Run a write. Returns the RETURNING row when the statement has one."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description else None
        conn.commit()
        return row


def execute_many(sql: str, rows: Sequence[Sequence[Any]]) -> None:
    with connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)
        conn.commit()


def healthy() -> bool:
    try:
        return query_one("SELECT 1 AS ok") is not None
    except Exception as exc:  # pragma: no cover - health path
        log.warning("database health check failed: %s", exc)
        return False


def run_migrations(migrations_dir: str = "/app/db/migrations") -> list[str]:
    """Apply any migration files that have not been applied yet.

    Postgres only runs `docker-entrypoint-initdb.d` against an empty data
    volume, so on an existing database a new migration would otherwise never
    land — and the failure looks like a missing table at runtime. Applying them
    at startup means a `git pull` is enough to get a schema change.

    Files are applied in filename order and recorded, so this is idempotent.
    """
    from pathlib import Path

    path = Path(migrations_dir)
    if not path.is_dir():
        log.warning("no migrations directory at %s", path)
        return []

    execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    applied = {r["filename"] for r in query("SELECT filename FROM schema_migrations")}

    # 001 created the base schema. On a fresh volume Postgres ran it via
    # initdb, so record it as applied rather than running it a second time.
    files = sorted(p for p in path.glob("*.sql"))
    if not applied and query_one("SELECT to_regclass('public.models') AS t")["t"]:
        for early in files:
            if early.name.startswith("001"):
                execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s) "
                    "ON CONFLICT DO NOTHING",
                    (early.name,),
                )
                applied.add(early.name)

    newly_applied: list[str] = []
    for f in files:
        if f.name in applied:
            continue
        log.info("applying migration %s", f.name)
        sql = f.read_text(encoding="utf-8")
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s) "
                    "ON CONFLICT DO NOTHING",
                    (f.name,),
                )
            conn.commit()
        newly_applied.append(f.name)

    return newly_applied


def audit(
    action: str,
    *,
    actor: str = "system",
    session_id: str | None = None,
    issue_id: str | None = None,
    payload: dict | None = None,
) -> None:
    """Append to the immutable audit log.

    Every consequential action — a coverage verdict, a charge, an order, an
    escalation — lands here. When a customer disputes something the agent did,
    this table is the answer, not the chat transcript.
    """
    import json

    execute(
        """
        INSERT INTO audit_log (session_id, issue_id, actor, action, payload)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (session_id, issue_id, actor, action, json.dumps(payload or {})),
    )
