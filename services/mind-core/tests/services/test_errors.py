"""Phase 4.1 tests for the service-layer exception hierarchy.

No database required. These verify the hierarchy, inheritance, and normal
Python exception behavior (including ``raise ... from`` cause chaining).
"""
from __future__ import annotations

import pytest

from app.services import (
    AuthorizationError,
    NotFoundError,
    PolicyViolationError,
    ServiceError,
    ValidationError,
)


def test_hierarchy_inherits_service_error():
    assert issubclass(ValidationError, ServiceError)
    assert issubclass(NotFoundError, ServiceError)
    assert issubclass(AuthorizationError, ServiceError)
    assert issubclass(PolicyViolationError, ServiceError)


def test_service_error_inherits_exception():
    assert issubclass(ServiceError, Exception)


def test_distinct_leaf_errors_are_not_each_other():
    leaves = [ValidationError, NotFoundError, AuthorizationError, PolicyViolationError]
    for a in leaves:
        for b in leaves:
            if a is not b:
                assert not issubclass(a, b)


def test_exception_can_be_raised_and_caught():
    with pytest.raises(ValidationError):
        raise ValidationError("bad input")


def test_caught_by_base_class():
    with pytest.raises(ServiceError):
        raise NotFoundError("missing")


def test_preserves_original_cause():
    original = ValueError("boom")
    wrapped = AuthorizationError("denied")
    try:
        try:
            raise original
        except ValueError as exc:
            raise wrapped from exc
    except ServiceError as caught:
        assert caught.__cause__ is original
        assert isinstance(caught.__cause__, ValueError)


def test_exceptions_are_instantiable_without_args():
    # No required fields; usable with just a message or none.
    assert str(ServiceError()) == ""
    assert str(PolicyViolationError("nope")) == "nope"


def test_imports_expose_expected_names():
    from app import services

    for name in [
        "BaseService",
        "ServiceError",
        "ValidationError",
        "NotFoundError",
        "AuthorizationError",
        "PolicyViolationError",
    ]:
        assert hasattr(services, name)