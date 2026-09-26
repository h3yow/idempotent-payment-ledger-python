"""Deterministic state transitions: illegal jumps, repeats, timeout retries."""

import pytest

from app.errors import InvalidStateTransition
from app.models import PaymentStatus
from app.payments import CLEARING_ACCOUNT, REVENUE_ACCOUNT
from tests.conftest import explain


def _created(service, key="sm-key", amount=100):
    return service.create_payment(key, amount, "CNY")["payment_id"]


def test_happy_path_created_confirmed_succeeded(service, ledger):
    pid = _created(service)
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    intent = service.get(pid)
    explain("sm-key", intent.status.value, "CREATED->CONFIRMED->SUCCEEDED is the legal success path")
    assert intent.status is PaymentStatus.SUCCEEDED
    assert ledger.balance(CLEARING_ACCOUNT) + ledger.balance(REVENUE_ACCOUNT) == 0


def test_illegal_jump_created_to_succeeded_rejected(service, ledger):
    pid = _created(service, "sm-jump")
    with pytest.raises(InvalidStateTransition):
        service.apply_channel_result(pid, True)
    explain("sm-jump", service.get(pid).status.value, "channel result before confirm is an illegal jump; no entries posted")
    assert service.get(pid).status is PaymentStatus.CREATED
    assert ledger.entries() == []


def test_repeated_confirm_converges_safely(service):
    pid = _created(service, "sm-confirm")
    first = service.confirm_payment(pid)
    again = service.confirm_payment(pid)
    explain("sm-confirm", again.status.value, "repeated confirm is a safe no-op, version stays put")
    assert again.status is PaymentStatus.CONFIRMED
    assert again.version == first.version


def test_confirm_on_terminal_state_rejected(service):
    pid = _created(service, "sm-term-confirm")
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    with pytest.raises(InvalidStateTransition):
        service.confirm_payment(pid)
    explain("sm-term-confirm", "SUCCEEDED", "confirm on terminal state is rejected")


def test_expire_only_from_created_and_retry_converges(service):
    pid = _created(service, "sm-expire")
    service.expire_payment(pid)
    again = service.expire_payment(pid)  # timeout retry converges
    explain("sm-expire", again.status.value, "repeated expire on EXPIRED converges without side effects")
    assert again.status is PaymentStatus.EXPIRED

    pid2 = _created(service, "sm-expire-confirmed")
    service.confirm_payment(pid2)
    with pytest.raises(InvalidStateTransition):
        service.expire_payment(pid2)
    explain("sm-expire-confirmed", "CONFIRMED", "expire after confirm is an illegal transition")


def test_failed_path_posts_nothing_and_stays_conserved(service, ledger):
    pid = _created(service, "sm-fail", 250)
    service.confirm_payment(pid)
    service.apply_channel_result(pid, False)
    explain("sm-fail", "FAILED", "failure path posts no entries; ledger total stays zero")
    assert service.get(pid).status is PaymentStatus.FAILED
    assert ledger.entries() == []
    assert ledger.balance(CLEARING_ACCOUNT) == 0
    assert ledger.balance(REVENUE_ACCOUNT) == 0


def test_terminal_state_is_sticky_against_contradicting_callback(service, ledger):
    pid = _created(service, "sm-sticky", 300)
    service.confirm_payment(pid)
    service.apply_channel_result(pid, False)
    with pytest.raises(InvalidStateTransition):
        service.apply_channel_result(pid, True)
    explain("sm-sticky", "FAILED", "contradicting success callback cannot overturn terminal FAILED")
    assert service.get(pid).status is PaymentStatus.FAILED
    assert ledger.entries() == []


def test_no_half_finished_records_on_rejected_transition(service, ledger):
    pid = _created(service, "sm-atomic", 400)
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    before = len(ledger.entries())
    with pytest.raises(InvalidStateTransition):
        service.apply_channel_result(pid, False)
    explain("sm-atomic", "SUCCEEDED", "rejected transition leaves ledger untouched (atomic)")
    assert len(ledger.entries()) == before
