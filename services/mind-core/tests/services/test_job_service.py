"""Phase 4.6 tests for JobService.

Run against the isolated ``mind_test_db`` via the shared ``db`` fixture
(which rolls back after each test). They exercise the job business
boundary: creation, reads, lifecycle transitions (started/finished/failed/
retry), listing by task/status, pagination, validation, NotFoundError
semantics, audit integration, and transaction ownership (the service
never commits or rolls back itself).

All identifiers are unique per test so the suite is self-isolating and
never relies on a clean persistent DB or global truncation.
"""
from __future__ import annotations

import uuid

import pytest
from psycopg.pq import TransactionStatus

from app.db.transaction import transaction
from app.repositories.agents import AgentRepository
from app.repositories.jobs import JobRepository
from app.repositories.tasks import TaskRepository
from app.repositories.tools import ToolRepository
from app.services import (
    AuditService,
    JobService,
    NotFoundError,
    ValidationError,
)

MISSING = "00000000-0000-0000-0000-000000000000"
ACTOR = "actor-33333333-3333-3333-3333-333333333333"


@pytest.fixture
def service(db):
    return JobService(db)


@pytest.fixture
def agents(db):
    return AgentRepository(db)


@pytest.fixture
def tasks(db):
    return TaskRepository(db)


@pytest.fixture
def tools(db):
    return ToolRepository(db)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _make_agent(agents) -> dict:
    return agents.create(owner=_uid("owner"), purpose="p")


def _make_task(tasks, agents) -> dict:
    return tasks.create(
        title=_uid("task"),
        agent_id=_make_agent(agents)["id"],
        status="pending",
    )


def _make_tool(tools) -> dict:
    return tools.create(
        name=_uid("tool"),
        description="d",
        requires_capability=None,
    )


def _make_job(service, agents, tasks=None, tools=None, **overrides) -> dict:
    payload: dict = {"actor_id": ACTOR}
    if "agent_id" not in overrides and "task_id" not in overrides:
        payload["agent_id"] = _make_agent(agents)["id"]
    if tasks is not None and "task_id" not in overrides:
        payload["task_id"] = _make_task(tasks, agents)["id"]
    if tools is not None and "tool_id" not in overrides:
        payload["tool_id"] = _make_tool(tools)["id"]
    payload.update(overrides)
    return service.create(**payload)


# ── 1. create success ───────────────────────────────────
def test_create_job(service, agents):
    agent = _make_agent(agents)
    job = service.create(actor_id=ACTOR, agent_id=agent["id"])
    assert job["id"] is not None
    assert job["status"] == "queued"
    assert job["retry_count"] == 0
    assert job["agent_id"] == agent["id"]
    assert job["result"] is None
    assert job["error"] is None


# ── 2. create with all links ────────────────────────────
def test_create_job_with_links(service, agents, tasks, tools):
    agent = _make_agent(agents)
    task = _make_task(tasks, agents)
    tool = _make_tool(tools)
    job = service.create(
        actor_id=ACTOR,
        task_id=task["id"],
        agent_id=agent["id"],
        tool_id=tool["id"],
        status="running",
    )
    assert job["task_id"] == task["id"]
    assert job["agent_id"] == agent["id"]
    assert job["tool_id"] == tool["id"]
    assert job["status"] == "running"


# ── 3. create validation ────────────────────────────────
def test_create_validation(service, agents):
    with pytest.raises(ValidationError):
        service.create(actor_id=ACTOR, status="bogus")
    with pytest.raises(ValidationError):
        service.create(actor_id=ACTOR, actor_type="daemon")
    with pytest.raises(ValidationError):
        service.create(actor_id=ACTOR, agent_id="not-a-uuid")
    with pytest.raises(ValidationError):
        service.create(actor_id=ACTOR, task_id="not-a-uuid")
    with pytest.raises(ValidationError):
        service.create(actor_id=ACTOR, tool_id="not-a-uuid")


# ── 4. create nonexistent FK rejected ────────────────────
def test_create_nonexistent_links(service, agents):
    with pytest.raises(NotFoundError):
        service.create(actor_id=ACTOR, agent_id=MISSING)
    with pytest.raises(NotFoundError):
        service.create(actor_id=ACTOR, task_id=MISSING)
    with pytest.raises(NotFoundError):
        service.create(actor_id=ACTOR, tool_id=MISSING)


# ── 5. get success ──────────────────────────────────────
def test_get_existing_job(service, agents):
    job = _make_job(service, agents)
    fetched = service.get(job["id"])
    assert fetched["id"] == job["id"]


# ── 6. get not found ────────────────────────────────────
def test_get_nonexistent_job(service):
    with pytest.raises(NotFoundError):
        service.get(MISSING)
    with pytest.raises(ValidationError):
        service.get("not-a-uuid")


# ── 7. mark_started success ─────────────────────────────
def test_mark_started(service, agents):
    job = _make_job(service, agents)
    updated = service.mark_started(job["id"], actor_id=ACTOR)
    assert updated["status"] == "running"
    assert updated["started_at"] is not None
    assert service.get(job["id"])["status"] == "running"


# ── 8. mark_started validation ──────────────────────────
def test_mark_started_validation(service, agents):
    job = _make_job(service, agents)
    with pytest.raises(ValidationError):
        service.mark_started("not-a-uuid", actor_id=ACTOR)
    with pytest.raises(ValidationError):
        service.mark_started(job["id"], actor_id=ACTOR, actor_type="daemon")
    with pytest.raises(NotFoundError):
        service.mark_started(MISSING, actor_id=ACTOR)


# ── 9. mark_finished success ────────────────────────────
def test_mark_finished(service, agents):
    job = _make_job(service, agents)
    result = {"ok": True, "value": 42}
    updated = service.mark_finished(job["id"], result, actor_id=ACTOR)
    assert updated["status"] == "succeeded"
    assert updated["finished_at"] is not None
    assert updated["result"] == result
    assert service.get(job["id"])["result"] == result


# ── 10. mark_finished with None result ──────────────────
def test_mark_finished_none_result(service, agents):
    job = _make_job(service, agents)
    updated = service.mark_finished(job["id"], None, actor_id=ACTOR)
    assert updated["status"] == "succeeded"
    assert updated["result"] is None


# ── 11. mark_finished validation ────────────────────────
def test_mark_finished_validation(service, agents):
    with pytest.raises(ValidationError):
        service.mark_finished("not-a-uuid", {}, actor_id=ACTOR)
    with pytest.raises(NotFoundError):
        service.mark_finished(MISSING, {}, actor_id=ACTOR)


# ── 12. mark_failed success ─────────────────────────────
def test_mark_failed(service, agents):
    job = _make_job(service, agents)
    updated = service.mark_failed(
        job["id"], "boom", actor_id=ACTOR
    )
    assert updated["status"] == "failed"
    assert updated["finished_at"] is not None
    assert updated["error"] == "boom"
    assert service.get(job["id"])["error"] == "boom"


# ── 13. mark_failed with None error ─────────────────────
def test_mark_failed_none_error(service, agents):
    job = _make_job(service, agents)
    updated = service.mark_failed(job["id"], None, actor_id=ACTOR)
    assert updated["status"] == "failed"
    assert updated["error"] is None


# ── 14. mark_failed validation ──────────────────────────
def test_mark_failed_validation(service, agents):
    job = _make_job(service, agents)
    with pytest.raises(ValidationError):
        service.mark_failed(job["id"], 123, actor_id=ACTOR)
    with pytest.raises(ValidationError):
        service.mark_failed("not-a-uuid", "e", actor_id=ACTOR)
    with pytest.raises(NotFoundError):
        service.mark_failed(MISSING, "e", actor_id=ACTOR)


# ── 15. increment_retry success ─────────────────────────
def test_increment_retry(service, agents):
    job = _make_job(service, agents)
    updated = service.increment_retry(job["id"], actor_id=ACTOR)
    assert updated["retry_count"] == 1
    assert service.get(job["id"])["retry_count"] == 1
    service.increment_retry(job["id"], actor_id=ACTOR)
    assert service.get(job["id"])["retry_count"] == 2


# ── 16. increment_retry validation ──────────────────────
def test_increment_retry_validation(service, agents):
    with pytest.raises(ValidationError):
        service.increment_retry("not-a-uuid", actor_id=ACTOR)
    with pytest.raises(NotFoundError):
        service.increment_retry(MISSING, actor_id=ACTOR)


# ── 17. list_by_task ────────────────────────────────────
def test_list_by_task(service, agents, tasks):
    task = _make_task(tasks, agents)
    _make_job(service, agents, tasks=tasks, task_id=task["id"])
    _make_job(service, agents, tasks=tasks, task_id=task["id"])
    _make_job(service, agents, tasks=tasks)  # unrelated
    rows = service.list_by_task(task["id"])
    assert len(rows) == 2
    assert all(r["task_id"] == task["id"] for r in rows)


# ── 18. list_by_task validation ─────────────────────────
def test_list_by_task_validation(service):
    with pytest.raises(ValidationError):
        service.list_by_task("not-a-uuid")


# ── 19. list_by_status ──────────────────────────────────
def test_list_by_status(service, agents):
    _make_job(service, agents, status="running")
    _make_job(service, agents, status="running")
    _make_job(service, agents, status="queued")
    rows = service.list_by_status("running")
    assert len(rows) == 2
    assert all(r["status"] == "running" for r in rows)
    with pytest.raises(ValidationError):
        service.list_by_status("bogus")


# ── 20. pagination limit ────────────────────────────────
def test_pagination_limit(service, agents):
    for _ in range(5):
        _make_job(service, agents)
    rows = service.list_by_status("queued", limit=2)
    assert len(rows) == 2


# ── 21. pagination offset (deterministic) ───────────────
def test_pagination_offset_deterministic(service, agents):
    # All jobs share the default scheduled_at = NULL, so the id DESC
    # tiebreaker is what makes the slices disjoint and deterministic.
    for _ in range(5):
        _make_job(service, agents, status="queued")
    first = service.list_by_status("queued", limit=2, offset=0)
    second = service.list_by_status("queued", limit=2, offset=2)
    assert len(second) == 2
    first_ids = {r["id"] for r in first}
    second_ids = {r["id"] for r in second}
    assert first_ids.isdisjoint(second_ids)

# ── 22. invalid pagination ──────────────────────────────
def test_invalid_pagination(service, agents):
    with pytest.raises(ValidationError):
        service.list_by_status("queued", limit=0)
    with pytest.raises(ValidationError):
        service.list_by_status("queued", limit=101)
    with pytest.raises(ValidationError):
        service.list_by_status("queued", limit=-1)
    with pytest.raises(ValidationError):
        service.list_by_status("queued", offset=-1)
    with pytest.raises(ValidationError):
        service.list_by_status("queued", limit=True)


# ── 23. audit record creation ───────────────────────────
def test_mutation_produces_audit_record(service, agents, db):
    job = _make_job(service, agents)
    audit = AuditService(db)
    rows = audit.list_by_target("job", str(job["id"]))
    assert len(rows) == 1
    assert rows[0]["action"] == "job.create"
    assert rows[0]["outcome"] == "success"
    assert rows[0]["actor_id"] == ACTOR


# ── 24. lifecycle transitions each audited ──────────────
def test_lifecycle_audited(service, agents, db):
    job = _make_job(service, agents)
    service.mark_started(job["id"], actor_id=ACTOR)
    service.mark_finished(job["id"], {"ok": True}, actor_id=ACTOR)
    audit = AuditService(db)
    rows = audit.list_by_target("job", str(job["id"]))
    actions = {r["action"] for r in rows}
    assert "job.create" in actions
    assert "job.mark_started" in actions
    assert "job.mark_finished" in actions


# ── 25. audit context redacts result/error ──────────────
def test_audit_context_redacts_payload(service, agents, db):
    job = _make_job(service, agents)
    service.mark_finished(
        job["id"], {"secret": "x"}, actor_id=ACTOR
    )
    service.mark_failed(job["id"], "secret-error", actor_id=ACTOR)
    audit = AuditService(db)
    rows = audit.list_by_target("job", str(job["id"]))
    for r in rows:
        assert "secret" not in str(r["context"])


# ── 26. mutation + audit atomicity (rollback) ───────────
def test_audit_atomicity_rollback(db, agents):
    service = JobService(db)
    audit = AuditService(db)
    job_id = None
    with pytest.raises(ValueError), transaction(db):
        job = service.create(actor_id=ACTOR, agent_id=_make_agent(agents)["id"])
        job_id = job["id"]
        raise ValueError("simulated failure")
    with pytest.raises(NotFoundError):
        service.get(job_id)
    assert audit.list_by_target("job", str(job_id)) == []

# ── 27. mutation + audit atomicity (commit) ─────────────
def test_audit_atomicity_commit(db, agents):
    service = JobService(db)
    audit = AuditService(db)
    job = None
    try:
        with transaction(db):
            job = service.create(
                actor_id=ACTOR, agent_id=_make_agent(agents)["id"]
            )
        assert service.get(job["id"]) is not None
        rows = audit.list_by_target("job", str(job["id"]))
        assert len(rows) == 1
    finally:
        if job is not None:
            with db.cursor() as cur:
                cur.execute(
                    "DELETE FROM audit_logs WHERE target_id = %s",
                    (str(job["id"]),),
                )
                cur.execute("DELETE FROM jobs WHERE id = %s", (job["id"],))
            db.commit()


# ── 28. service does not commit ─────────────────────────
def test_service_does_not_commit(service, db, agents):
    _make_job(service, agents)
    assert db.info.transaction_status == TransactionStatus.INTRANS


# ── 29. service does not rollback ───────────────────────
def test_service_does_not_rollback(service, db, agents):
    _make_job(service, agents)
    assert db.info.transaction_status == TransactionStatus.INTRANS


# ── 30. service does not create connections ─────────────
def test_service_does_not_create_connections(monkeypatch, db, agents):
    calls = []

    def _forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("psycopg.connect must not be called by the service")

    import psycopg

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    service = JobService(db)
    _make_job(service, agents)
    assert calls == [], "JobService must not open a database connection."


# ── 31. repository delegation ───────────────────────────
def test_repository_is_used(monkeypatch, service, agents):
    called = {"create": False}
    original = JobRepository.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(JobRepository, "create", _spy)
    _make_job(service, agents)
    assert called["create"] is True


# ── 32. no job-lifecycle capability policy enforced ─────
def test_no_job_capability_policy_enforced(service, agents):
    # The current capability catalog defines no job capability, so job
    # operations are unconditionally callable at the service layer. This
    # documents the boundary rather than inventing policy.
    job = _make_job(service, agents)
    assert job["id"] is not None
    updated = service.mark_started(job["id"], actor_id=ACTOR)
    assert updated["status"] == "running"