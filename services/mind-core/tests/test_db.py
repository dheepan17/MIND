"""Tests for the mind-core database access layer (Phase 1).

Pure tests (no database):
    * missing DATABASE_URL is rejected,
    * explicit URL is accepted (without touching a real server),
    * TEST_DATABASE_URL is never used by application connection code.

Database-backed tests (require TEST_DATABASE_URL -> mind_test_db):
    * transaction commits on success,
    * transaction rolls back on exception,
    * connection is closed correctly,
    * tests never connect to mind_db.

The DB-backed tests are skipped when TEST_DATABASE_URL is unset or psycopg
is unavailable, so they never touch the production ``mind_db``.
"""
from __future__ import annotations

import os

import pytest

from app.db import DatabaseConfigError, connect, transaction

# ── Pure (no-DB) tests ──────────────────────────────────────────

def test_missing_database_url_rejected(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    # Ensure config picks up the cleared environment.
    with pytest.raises(DatabaseConfigError):
        connect()


def test_explicit_url_is_used_over_environment(monkeypatch):
    """An explicit URL is passed straight to psycopg, not the env URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    captured = {}

    def fake_connect(url, autocommit=False):
        captured["url"] = url
        raise RuntimeError("no real connection in pure test")

    monkeypatch.setattr("app.db.connection.psycopg.connect", fake_connect)
    with pytest.raises(RuntimeError):
        connect("postgresql://explicit/explicit_db")
    assert captured["url"] == "postgresql://explicit/explicit_db"


def test_application_code_never_uses_test_database_url(monkeypatch):
    """connect() must ignore TEST_DATABASE_URL and use DATABASE_URL only."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://test/test_db")
    # With DATABASE_URL unset, connect() must refuse even though a test URL
    # exists -- proving application code cannot accidentally use the test DB.
    with pytest.raises(DatabaseConfigError):
        connect()


# ── Database-backed tests (mind_test_db only) ───────────────────

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _require_test_db() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping live DB tests")


@pytest.fixture
def test_conn():
    _require_test_db()
    conn = connect(TEST_DATABASE_URL)
    try:
        yield conn
    finally:
        # Only roll back if the test left the connection open. A test may
        # intentionally close the connection (e.g. test_connection_is_closed);
        # rolling back a closed connection raises OperationalError. Genuine
        # database errors during rollback are still propagated.
        if not conn.closed:
            conn.rollback()
        conn.close()


def test_transaction_commits_on_success(test_conn):
    with transaction(test_conn) as cur:
        cur.execute(
            "INSERT INTO agents (owner, purpose) VALUES ('db-test', 'commit') "
            "RETURNING id"
        )
        new_id = cur.fetchone()[0]
    # Outside the block the transaction is committed.
    with test_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM agents WHERE id = %s", (new_id,))
        assert cur.fetchone()[0] == 1


def test_transaction_rolls_back_on_exception(test_conn):
    new_id = None
    try:
        with transaction(test_conn) as cur:
            cur.execute(
                "INSERT INTO agents (owner, purpose) VALUES ('db-test', 'rb') "
                "RETURNING id"
            )
            new_id = cur.fetchone()[0]
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass
    # The insert performed inside the transaction() block must be rolled back.
    # With autocommit=False the entire connection transaction is rolled back,
    # so the row inserted above is no longer visible.
    with test_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM agents WHERE id = %s", (new_id,))
        assert cur.fetchone()[0] == 0


def test_connection_is_closed(test_conn):
    assert not test_conn.closed
    test_conn.close()
    assert test_conn.closed


def test_never_connects_to_mind_db():
    """Guard: this test suite must never target the production database."""
    assert "mind_db" not in (TEST_DATABASE_URL or "")