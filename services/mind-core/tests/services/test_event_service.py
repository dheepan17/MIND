"""Phase 4.7 tests for EventService.

Run against the isolated ``mind_test_db`` via the shared ``db`` fixture
(which rolls back after each test). They exercise the event-stream
business boundary: append-only creation, reads, listing by correlation /
producer / security flag, deterministic pagination, validation,
NotFoundError semantics, transaction ownership (the service never
commits or rolls back itself), and the deliberate absence of audit
mirroring for ordinary event creation.

All identifiers are unique per test so the suite is self-isolating and
never relies on a clean persistent DB or global truncation.
"""
from __future__ import annotations

import uuid

import pytest
from psycopg.pq import TransactionStatus

from app.db.transaction import transaction
from app.repositories.agents import AgentRepository
from app.repositories.events import EventRepository
from app.services import (
    AuditService,
    EventService,
    NotFoundError,
    ValidationError,
)

MISSING = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def service(db):
    return EventService(db)


@pytest.fixture
def agents(db):
    return AgentRepository(db)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _make_agent(agents) -> dict:
    return agents.create(owner=_uid("owner"), purpose="p")


def _make_event(service, agents=None, **overrides) -> dict:
    # EventService.create() takes keyword-only arguments: ``type`` and
    # ``payload`` are required, plus optional ``producer_agent_id``,
    # ``correlation_id``, ``signature`` and ``is_security_event``. The
    # helper keeps ``type`` and ``payload`` as distinct arguments (it must
    # NOT merge them into one dict) and only auto-assigns a producer agent
    # when the caller did not supply one.
    kwargs: dict = {"type": "test", "payload": {}}
    if agents is not None and "producer_agent_id" not in overrides:
        kwargs["producer_agent_id"] = _make_agent(agents)["id"]
    kwargs.update(overrides)
    return service.create(**kwargs)


# ── 1. create basic event ────────────────────────────────
def test_create_basic_event(service, agents):
    agent = _make_agent(agents)
    event = service.create(
        type="task.created",
        payload={"k": "v"},
        producer_agent_id=agent["id"],
    )
    assert event["id"] is not None
    assert event["type"] == "task.created"
    assert event["payload"] == {"k": "v"}
    assert event["producer_agent_id"] == agent["id"]
    assert event["is_security_event"] is False
    assert event["signature"] is None


# ── 2. create event with payload ─────────────────────────
def test_create_event_with_payload(service, agents):
    agent = _make_agent(agents)
    payload = {"nested": {"a": [1, 2, 3]}, "flag": True}
    event = service.create(
        type="memory.stored",
        payload=payload,
        producer_agent_id=agent["id"],
    )
    assert event["payload"] == payload


# ── 3. create security event ─────────────────────────────
def test_create_security_event(service, agents):
    agent = _make_agent(agents)
    event = service.create(
        type="auth.denied",
        payload={"reason": "no-cap"},
        producer_agent_id=agent["id"],
        is_security_event=True,
    )
    assert event["is_security_event"] is True


# ── 4. create event with signature ───────────────────────
def test_create_event_with_signature(service, agents):
    agent = _make_agent(agents)
    # Signature is stored verbatim; the service never verifies it.
    event = service.create(
        type="signed.event",
        payload={"x": 1},
        producer_agent_id=agent["id"],
        signature="deadbeef" * 8,
    )
    assert event["signature"] == "deadbeef" * 8


# ── 5. create with optional fields ───────────────────────
def test_create_event_with_optional_fields(service, agents):
    agent = _make_agent(agents)
    correlation = uuid.uuid4()
    event = service.create(
        type="correlated.event",
        payload={"v": 1},
        producer_agent_id=agent["id"],
        correlation_id=correlation,
        signature="sig",
        is_security_event=True,
    )
    assert event["correlation_id"] == correlation
    assert event["signature"] == "sig"
    assert event["is_security_event"] is True


# ── 6. create without producer (internal/unsigned) ───────
def test_create_event_without_producer(service):
    event = service.create(
        type="system.boot",
        payload={"uptime": 0},
    )
    assert event["producer_agent_id"] is None
    assert event["signature"] is None


# ── 7. producer agent existence validation ───────────────
def test_create_nonexistent_producer_rejected(service):
    with pytest.raises(NotFoundError):
        service.create(
            type="x",
            payload={"v": 1},
            producer_agent_id=MISSING,
        )


# ── 8. get existing event ────────────────────────────────
def test_get_existing_event(service, agents):
    event = _make_event(service, agents)
    fetched = service.get(event["id"])
    assert fetched["id"] == event["id"]


# ── 9. get nonexistent event ─────────────────────────────
def test_get_nonexistent_event(service):
    with pytest.raises(NotFoundError):
        service.get(MISSING)
    with pytest.raises(ValidationError):
        service.get("not-a-uuid")


# ── 10. list_by_correlation ──────────────────────────────
def test_list_by_correlation(service, agents):
    agent = _make_agent(agents)
    correlation = uuid.uuid4()
    service.create(
        type="a", payload={"v": 1}, producer_agent_id=agent["id"],
        correlation_id=correlation,
    )
    service.create(
        type="b", payload={"v": 2}, producer_agent_id=agent["id"],
        correlation_id=correlation,
    )
    service.create(
        type="c", payload={"v": 3}, producer_agent_id=agent["id"],
        correlation_id=str(uuid.uuid4()),  # unrelated
    )
    rows = service.list_by_correlation(correlation)
    assert len(rows) == 2
    assert all(r["correlation_id"] == correlation for r in rows)


# ── 11. list_by_correlation validation ───────────────────
def test_list_by_correlation_validation(service):
    with pytest.raises(ValidationError):
        service.list_by_correlation("not-a-uuid")


# ── 12. list_by_producer ─────────────────────────────────
def test_list_by_producer(service, agents):
    agent = _make_agent(agents)
    other = _make_agent(agents)
    service.create(type="a", payload={}, producer_agent_id=agent["id"])
    service.create(type="b", payload={}, producer_agent_id=agent["id"])
    service.create(type="c", payload={}, producer_agent_id=other["id"])
    rows = service.list_by_producer(agent["id"])
    assert len(rows) == 2
    assert all(r["producer_agent_id"] == agent["id"] for r in rows)


# ── 13. list_by_producer validation ──────────────────────
def test_list_by_producer_validation(service):
    with pytest.raises(ValidationError):
        service.list_by_producer("not-a-uuid")


# ── 14. list_security_events ─────────────────────────────
def test_list_security_events(service, agents):
    agent = _make_agent(agents)
    service.create(
        type="a", payload={}, producer_agent_id=agent["id"],
        is_security_event=True,
    )
    service.create(
        type="b", payload={}, producer_agent_id=agent["id"],
        is_security_event=True,
    )
    service.create(type="c", payload={}, producer_agent_id=agent["id"])
    rows = service.list_security_events()
    assert len(rows) == 2
    assert all(r["is_security_event"] is True for r in rows)


# ── 15. pagination limit ──────────────────────────────────
def test_pagination_limit(service, agents):
    agent = _make_agent(agents)
    for _ in range(5):
        service.create(type="x", payload={}, producer_agent_id=agent["id"])
    rows = service.list_by_producer(agent["id"], limit=2)
    assert len(rows) == 2


# ── 16. pagination offset (deterministic) ────────────────
def test_pagination_offset_deterministic(service, agents):
    # All events share the default created_at = transaction start time, so
    # the id DESC tiebreaker is what makes the slices disjoint/deterministic.
    agent = _make_agent(agents)
    for _ in range(5):
        service.create(type="x", payload={}, producer_agent_id=agent["id"])
    first = service.list_by_producer(agent["id"], limit=2, offset=0)
    second = service.list_by_producer(agent["id"], limit=2, offset=2)
    assert len(second) == 2
    first_ids = {r["id"] for r in first}
    second_ids = {r["id"] for r in second}
    assert first_ids.isdisjoint(second_ids)

# ── 17. invalid pagination ───────────────────────────────
def test_invalid_pagination(service, agents):
    agent = _make_agent(agents)
    with pytest.raises(ValidationError):
        service.list_by_producer(agent["id"], limit=0)
    with pytest.raises(ValidationError):
        service.list_by_producer(agent["id"], limit=101)
    with pytest.raises(ValidationError):
        service.list_by_producer(agent["id"], limit=-1)
    with pytest.raises(ValidationError):
        service.list_by_producer(agent["id"], offset=-1)
    with pytest.raises(ValidationError):
        service.list_by_producer(agent["id"], limit=True)
    with pytest.raises(ValidationError):
        service.list_by_producer(agent["id"], offset=True)


# ── 18. validation: invalid required strings ─────────────
def test_create_validation_strings(service, agents):
    agent = _make_agent(agents)
    with pytest.raises(ValidationError):
        service.create(type="", payload={}, producer_agent_id=agent["id"])
    with pytest.raises(ValidationError):
        service.create(type=123, payload={}, producer_agent_id=agent["id"])
    with pytest.raises(ValidationError):
        service.create(type="x", payload=None, producer_agent_id=agent["id"])


# ── 19. validation: invalid UUIDs ────────────────────────
def test_create_validation_uuids(service):
    with pytest.raises(ValidationError):
        service.create(type="x", payload={}, producer_agent_id="not-a-uuid")
    with pytest.raises(ValidationError):
        service.create(type="x", payload={}, correlation_id="not-a-uuid")


# ── 20. validation: invalid domain values ────────────────
def test_create_validation_domain(service, agents):
    agent = _make_agent(agents)
    with pytest.raises(ValidationError):
        service.create(
            type="x", payload={}, producer_agent_id=agent["id"],
            is_security_event="yes",
        )
    with pytest.raises(ValidationError):
        service.create(
            type="x", payload={}, producer_agent_id=agent["id"],
            signature=123,
        )


# ── 21. immutability: no update/delete API on service ────
def test_service_has_no_mutation_api(service):
    for name in ("update", "delete", "remove", "purge"):
        assert not hasattr(service, name), (
            f"EventService must not expose {name}()."
        )


# ── 22. immutability: repository remains append/read only ─
def test_repository_is_append_only(db):
    repo = EventRepository(db)
    for name in ("update", "delete", "remove", "purge"):
        assert not hasattr(repo, name), (
            f"EventRepository must not expose {name}()."
        )


# ── 23. repository delegation ────────────────────────────
def test_repository_is_used(monkeypatch, service, agents):
    called = {"append": False}
    original = EventRepository.append

    def _spy(self, *args, **kwargs):
        called["append"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EventRepository, "append", _spy)
    _make_event(service, agents)
    assert called["append"] is True


# ── 24. no audit row for ordinary event creation ────────
def test_no_audit_mirroring_for_event_creation(service, agents, db):
    event = _make_event(service, agents)
    audit = AuditService(db)
    # Events are the stream; audit_logs are accountability. Ordinary event
    # creation must NOT be mirrored into audit_logs.
    assert audit.list_by_target("event", str(event["id"])) == []


# ── 25. mutation + atomicity (rollback) ──────────────────
def test_atomicity_rollback(db, agents):
    service = EventService(db)
    event_id = None
    with pytest.raises(ValueError), transaction(db):
        event_id = service.create(
            type="x", payload={}, producer_agent_id=_make_agent(agents)["id"]
        )["id"]
        raise ValueError("simulated failure")
    with pytest.raises(NotFoundError):
        service.get(event_id)


# ── 26. mutation + atomicity (commit) ────────────────────
def test_atomicity_commit(db, agents):
    service = EventService(db)
    event = None
    try:
        with transaction(db):
            event = service.create(
                type="x", payload={},
                producer_agent_id=_make_agent(agents)["id"],
            )
        assert service.get(event["id"]) is not None
    finally:
        if event is not None:
            with db.cursor() as cur:
                cur.execute("DELETE FROM events WHERE id = %s", (event["id"],))
            db.commit()


# ── 27. service does not commit ──────────────────────────
def test_service_does_not_commit(service, db, agents):
    _make_event(service, agents)
    assert db.info.transaction_status == TransactionStatus.INTRANS


# ── 28. service does not rollback ────────────────────────
def test_service_does_not_rollback(service, db, agents):
    _make_event(service, agents)
    assert db.info.transaction_status == TransactionStatus.INTRANS


# ── 29. service does not create connections ──────────────
def test_service_does_not_create_connections(monkeypatch, db, agents):
    calls = []

    def _forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("psycopg.connect must not be called by the service")

    import psycopg

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    service = EventService(db)
    _make_event(service, agents)
    assert calls == [], "EventService must not open a database connection."


# ── 30. caller-owned transaction remains active ─────────
def test_caller_transaction_stays_active(service, db, agents):
    with transaction(db):
        _make_event(service, agents)
        # The service must not have committed/rolled back the caller's txn.
        assert db.info.transaction_status == TransactionStatus.INTRANS