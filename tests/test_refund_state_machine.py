"""Refund state machine: ordering, expiry, terminal stickiness, conservation."""

import pytest

from app.errors import InvalidStateTransition
from app.models import RefundStatus
from app.payments import CLEARING_ACCOUNT, REVENUE_ACCOUNT
from tests.conftest import explain


def _refund(refunds, service, key="rsm-key", amount=100, pay_amount=None):
    pid = service.create_payment(key + "-pay", pay_amount or amount, "CNY")["payment_id"]
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    rid = refunds.create_refund(pid, key, amount, "CNY")["refund_id"]
    return pid, rid


def test_refund_success_posts_balanced_reversal(refunds, service, ledger):
    pid, rid = _refund(refunds, service, "rsm-ok", 300)
    refunds.apply_channel_result(rid, True)
    explain("rsm-ok", "SUCCEEDED", "refund posts revenue debit + clearing credit, balanced")
    assert refunds.get(rid).status is RefundStatus.SUCCEEDED
    # Payment: clearing -300 revenue +300; refund: revenue -300 clearing +300.
    assert ledger.balance(CLEARING_ACCOUNT) == 0
    assert ledger.balance(REVENUE_ACCOUNT) == 0


def test_refund_repeated_success_converges(refunds, service):
    pid, rid = _refund(refunds, service, "rsm-dup", 100)
    first = refunds.apply_channel_result(rid, True)
    again = refunds.apply_channel_result(rid, True)
    explain("rsm-dup", again.status.value, f"repeated success no-op, version {first.version}=={again.version}")
    assert again.version == first.version
    assert again.status is RefundStatus.SUCCEEDED


def test_refund_expire_from_created_and_retry_converges(refunds, service, ledger):
    pid, rid = _refund(refunds, service, "rsm-exp", 50)
    first = refunds.expire_refund(rid)
    again = refunds.expire_refund(rid)
    explain("rsm-exp", again.status.value, "repeated expire converges, no entries")
    assert first.status is again.status is RefundStatus.EXPIRED
    assert [e for e in ledger.entries() if e.refund_id == rid] == []


def test_refund_result_after_expire_rejected(refunds, service):
    pid, rid = _refund(refunds, service, "rsm-expthencb", 50)
    refunds.expire_refund(rid)
    with pytest.raises(InvalidStateTransition):
        refunds.apply_channel_result(rid, True)
    explain("rsm-expthencb", "EXPIRED", "channel success after EXPIRED rejected; terminal sticky")
    assert refunds.get(rid).status is RefundStatus.EXPIRED


def test_refund_contradiction_after_success_rejected(refunds, service, ledger):
    pid, rid = _refund(refunds, service, "rsm-contra", 80)
    refunds.apply_channel_result(rid, True)
    before = len(ledger.entries())
    with pytest.raises(InvalidStateTransition):
        refunds.apply_channel_result(rid, False)
    explain("rsm-contra", "SUCCEEDED", "failure callback after success rejected; entries unchanged")
    assert len(ledger.entries()) == before
    assert refunds.get(rid).status is RefundStatus.SUCCEEDED


def test_expired_refund_releases_capacity(refunds, service):
    pid, rid = _refund(refunds, service, "rsm-cap", 100)
    refunds.expire_refund(rid)
    new = refunds.create_refund(pid, "rsm-cap-new", 100, "CNY")
    explain("rsm-cap-new", new["status"], "EXPIRED refund released full capacity")
    assert new["status"] == RefundStatus.CREATED.value
