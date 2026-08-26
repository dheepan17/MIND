"""Phase 4.3 tests for AuditService.

These run against the isolated ``mind_test_db`` via the shared ``db``
fixture (which rolls back after each test). They exercise the append-only
audit boundary: recording success/denied/error, target/context capture,
read/list access, pagination, and service-boundary validation.
"""
from __future__ import annotations

import pytest
from psycopg.pq import TransactionStatus

from app.services import AuditService, ValidationError

# A stable actor identity for the tests below.
ACTOR_TYPE = "agent"
ACTOR_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _isolate_audit_table(db):
    """Truncate audit_logs at the start of each test so exact-count
    assertions are isolated from any rows committed by earlier runs.
    Test data only; the schema is never touched.
    """
    with db.cursor() as cur:
        cur.execute("TRUNCATE audit_logs")
    db.commit()
    yield


@pytest.fixture
def service(db):
    return AuditService(db)


def _record(service, **overrides):
    """Record a minimal audit entry, allowing overrides."""
    payload = {
        "actor_type": ACTOR_TYPE,
        "actor_id": ACTOR_ID,
        "action": "test.action",
        "outcome": "success",
    }
    payload.update(overrides)
    return service.record(**payload)


# ── 1. record success ────────────────────────────────────
def test_record_success(service):
    rec = _record(service)
    assert rec["id"] is not None
    assert rec["outcome"] == "success"
    assert rec["actor_type"] == ACTOR_TYPE
    assert rec["actor_id"] == ACTOR_ID
    assert rec["action"] == "test.action"


# ── 2. record denied ─────────────────────────────────────
def test_record_denied(service):
    rec = _record(service, outcome="denied")
    assert rec["outcome"] == "denied"


# ── 3. record error ──────────────────────────────────────
def test_record_error(service):
    rec = _record(service, outcome="error")
    assert rec["outcome"] == "error"


# ── 4. record with target information ────────────────────
def test_record_with_target(service):
    rec = _record(
        service,
        target_type="task",
        target_id="22222222-2222-2222-2222-222222222222",
    )
    assert rec["target_type"] == "task"
    assert rec["target_id"] == "22222222-2222-2222-2222-222222222222"


# ── 5. record with context ───────────────────────────────
def test_record_with_context(service):
    ctx = {"ip": "10.0.0.1", "request_id": "abc"}
    rec = _record(service, context=ctx)
    assert rec["context"] == ctx


# ── 6. get existing record ───────────────────────────────
def test_get_existing(service):
    rec = _record(service)
    fetched = service.get(rec["id"])
    assert fetched is not None
    assert fetched["id"] == rec["id"]
    assert fetched["action"] == "test.action"


# ── 7. get nonexistent record returns None ───────────────
def test_get_nonexistent_returns_none(service):
    assert service.get(9_999_999) is None


# ── 8. list_by_actor ─────────────────────────────────────
def test_list_by_actor(service):
    _record(service, action="a.one")
    _record(service, action="a.two")
    rows = service.list_by_actor(ACTOR_TYPE, ACTOR_ID)
    assert len(rows) == 2
    # Newest first.
    actions = [r["action"] for r in rows]
    assert "a.one" in actions and "a.two" in actions


# ── 9. list_by_target ────────────────────────────────────
def test_list_by_target(service):
    tid = "33333333-3333-3333-3333-333333333333"
    _record(service, target_type="task", target_id=tid)
    _record(service, target_type="task", target_id=tid)
    rows = service.list_by_target("task", tid)
    assert len(rows) == 2
    assert all(r["target_id"] == tid for r in rows)


# ── 10. list_by_outcome ──────────────────────────────────
def test_list_by_outcome(service):
    _record(service, outcome="denied")
    _record(service, outcome="denied")
    _record(service, outcome="success")
    rows = service.list_by_outcome("denied")
    assert len(rows) == 2
    assert all(r["outcome"] == "denied" for r in rows)


# ── 11. pagination limit ─────────────────────────────────
def test_pagination_limit(service):
    for i in range(5):
        _record(service, action=f"p{i}")
    rows = service.list_by_actor(ACTOR_TYPE, ACTOR_ID, limit=2)
    assert len(rows) == 2


# ── 12. pagination offset ────────────────────────────────
def test_pagination_offset(service):
    for i in range(5):
        _record(service, action=f"p{i}")
    first = service.list_by_actor(ACTOR_TYPE, ACTOR_ID, limit=2, offset=0)
    second = service.list_by_actor(ACTOR_TYPE, ACTOR_ID, limit=2, offset=2)
    assert len(second) == 2
    # Offset page must not overlap the first page.
    first_ids = {r["id"] for r in first}
    second_ids = {r["id"] for r in second}
    assert first_ids.isdisjoint(second_ids)


# ── 13. invalid limit ────────────────────────────────────
def test_invalid_limit(service):
    with pytest.raises(ValidationError):
        service.list_by_actor(ACTOR_TYPE, ACTOR_ID, limit=0)
    with pytest.raises(ValidationError):
        service.list_by_actor(ACTOR_TYPE, ACTOR_ID, limit=101)
    with pytest.raises(ValidationError):
        service.list_by_actor(ACTOR_TYPE, ACTOR_ID, limit=-1)


# ── 14. invalid offset ───────────────────────────────────
def test_invalid_offset(service):
    with pytest.raises(ValidationError):
        service.list_by_actor(ACTOR_TYPE, ACTOR_ID, offset=-1)


# ── 15. invalid outcome ──────────────────────────────────
def test_invalid_outcome(service):
    with pytest.raises(ValidationError):
        _record(service, outcome="maybe")
    with pytest.raises(ValidationError):
        _record(service, outcome="")


# ── 16. empty actor_type ─────────────────────────────────
def test_empty_actor_type(service):
    with pytest.raises(ValidationError):
        _record(service, actor_type="")


# ── 17. empty action ─────────────────────────────────────
def test_empty_action(service):
    with pytest.raises(ValidationError):
        _record(service, action="")


# ── 18. empty target_type rejected ───────────────────────
def test_empty_target_type_rejected(service):
    with pytest.raises(ValidationError):
        _record(service, target_type="")


# ── 19. empty capability_used rejected ───────────────────
def test_empty_capability_used_rejected(service):
    with pytest.raises(ValidationError):
        _record(service, capability_used="")


# ── 20. transaction remains caller-owned after record ────
def test_transaction_remains_caller_owned(db):
    from app.db.transaction import transaction

    service = AuditService(db)
    with transaction(db):
        service.record(
            actor_type=ACTOR_TYPE,
            actor_id=ACTOR_ID,
            action="tx.action",
            outcome="success",
        )
        # The service must NOT commit; the connection stays in-transaction
        # so the caller can compose further work (e.g. the operation
        # being audited) and commit once.
        assert db.info.transaction_status == TransactionStatus.INTRANS
        # Discard the row so this test does not persist data (the manager
        # would otherwise commit on clean exit). The service itself never
        # commits; this rollback is test hygiene only.
        db.rollback()


# ── immutability: no update/delete methods exist ─────────
def test_no_update_or_delete_methods(service):
    for name in ("update", "delete", "modify"):
        assert not hasattr(service, name), (
            f"AuditService must not expose {name!r}"
        )