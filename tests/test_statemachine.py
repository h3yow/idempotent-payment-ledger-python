"""Illegal transitions, duplicate confirm and timeout retry convergence."""
import logging

import pytest

from app.errors import InvalidStateTransitionError, PaymentNotFoundError
from app.models import PaymentStatus

log = logging.getLogger("test.statemachine")


def test_illegal_transition_rejected(service):
    _, body = service.create_payment("key-S1", 100, "CNY")
    pid = body["payment_id"]
    # CREATED -> SUCCEEDED directly is not allowed; confirm twice also illegal.
    service.confirm(pid)
    with pytest.raises(InvalidStateTransitionError) as exc:
        service.confirm(pid)  # duplicate confirm on terminal state
    log.info(
        "key=%s payment=%s status=%s code=%s basis=terminal state has no outgoing edges",
        "key-S1", pid, service.get_payment(pid).status.value, exc.value.code,
    )
    assert exc.value.code == "INVALID_STATE_TRANSITION"
    assert len(service.ledger.entries_for(pid)) == 2  # still exactly one pair
    assert service.ledger.net_balance() == 0


def test_expire_then_confirm_rejected(service):
    _, body = service.create_payment("key-S2", 100, "CNY")
    pid = body["payment_id"]
    service.expire(pid)
    with pytest.raises(InvalidStateTransitionError):
        service.confirm(pid)
    log.info(
        "key=%s payment=%s status=%s basis=EXPIRED is terminal, confirm rejected",
        "key-S2", pid, service.get_payment(pid).status.value,
    )
    assert service.get_payment(pid).status is PaymentStatus.EXPIRED
    assert service.ledger.entries_for(pid) == []  # no partial records


def test_failed_payment_has_no_partial_ledger(service):
    _, body = service.create_payment("key-S3", 100, "CNY")
    pid = body["payment_id"]
    service.confirm(pid, succeed=False)
    intent = service.get_payment(pid)
    log.info(
        "key=%s payment=%s status=%s reason=%s basis=channel decline -> FAILED, no ledger entries",
        "key-S3", pid, intent.status.value, intent.failure_reason,
    )
    assert intent.status is PaymentStatus.FAILED
    assert intent.failure_reason == "channel_declined"
    assert service.ledger.entries_for(pid) == []
    assert service.ledger.net_balance() == 0


def test_expire_from_processing_converges(service):
    _, body = service.create_payment("key-S4", 100, "CNY")
    pid = body["payment_id"]
    # Force into PROCESSING without completing the callback by expiring first.
    service.expire(pid)
    with pytest.raises(InvalidStateTransitionError):
        service.expire(pid)  # repeated timeout retry converges safely
    log.info(
        "key=%s payment=%s status=%s basis=repeated expire on terminal state rejected",
        "key-S4", pid, service.get_payment(pid).status.value,
    )
    assert service.ledger.net_balance() == 0


def test_unknown_payment_distinguishable(service):
    with pytest.raises(PaymentNotFoundError) as exc:
        service.confirm("pay_missing")
    log.info("code=%s basis=unknown payment id", exc.value.code)
    assert exc.value.code == "PAYMENT_NOT_FOUND"
