"""Shared pytest fixtures for mind-core database tests.

This fixture is the ONLY sanctioned entry point for live database tests.
It connects exclusively to the isolated test database (``mind_test_db``)
via ``TEST_DATABASE_URL`` and refuses to run against any other database,
including the application database ``mind_db``.

Safety guarantees:
* Reads ``TEST_DATABASE_URL`` only. It never reads ``DATABASE_URL`` and
  never falls back to it.
* Skips (does not error) when ``TEST_DATABASE_URL`` is unset.
* Fails loudly if the connected database is not ``mind_test_db``
  (no silent redirection).
* Never executes migrations, never recreates tables, never alters schema.
* Wraps each test in a transaction that is rolled back afterwards, so no
  test data is ever persisted.
* Closes the connection in all cases.
* Never logs or prints connection URLs or credentials.
"""
from __future__ import annotations

import os

import psycopg
import pytest

# The isolated automated-test database. Application code / migrations use
# ``mind_db`` via DATABASE_URL; tests must never touch it.
EXPECTED_TEST_DB_NAME = "mind_test_db"

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def db():
    """Yield a connection to mind_test_db, rolled back after the test.

    The connection is in non-autocommit mode; every statement issued
    during the test belongs to a single transaction that is rolled back
    in teardown, leaving the database unchanged.
    """
    if not TEST_DATABASE_URL:
        pytest.skip(
            "TEST_DATABASE_URL is not set; skipping live database tests. "
            "Point it at the isolated 'mind_test_db' database."
        )

    conn = psycopg.connect(TEST_DATABASE_URL, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            actual_db = cur.fetchone()[0]
        if actual_db != EXPECTED_TEST_DB_NAME:
            pytest.fail(
                f"Refusing to run tests against database '{actual_db}'. "
                f"Live schema tests must target '{EXPECTED_TEST_DB_NAME}' "
                f"via TEST_DATABASE_URL."
            )
        yield conn
    finally:
        # Roll back any uncommitted work and always release the connection.
        try:
            conn.rollback()
        finally:
            conn.close()