"""Lightweight PostgreSQL migration runner for mind-core.

Applies every ``*.sql`` file in this directory in lexical order, tracking
applied migrations in ``_schema_migrations`` so re-runs are idempotent.

Usage:
    DATABASE_URL=postgresql://user:pass@localhost:5432/mind \\
        python -m migrations.run
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent


def get_connection():
    """Open a PostgreSQL connection using DATABASE_URL."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL environment variable is not set.")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "psycopg is required to run migrations. "
            "Install with: pip install 'psycopg[binary]'"
        ) from exc
    return psycopg.connect(database_url, autocommit=False)


def ensure_migrations_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS _schema_migrations (
                version    TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


def applied_versions(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM _schema_migrations")
        return {row[0] for row in cur.fetchall()}


def run() -> None:
    conn = get_connection()
    try:
        ensure_migrations_table(conn)
        already = applied_versions(conn)
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            print("No migration files found.")
            return
        for path in files:
            version = path.stem
            if version in already:
                print(f"skip  {version}")
                continue
            print(f"apply {version} ({path.name})")
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO _schema_migrations (version) VALUES (%s)",
                    (version,),
                )
            conn.commit()
            print(f"done  {version}")
    finally:
        conn.close()


if __name__ == "__main__":
    # Allow `python -m migrations.run` from the service root.
    sys.path.insert(0, str(MIGRATIONS_DIR.parent))
    run()