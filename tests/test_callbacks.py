"""Channel callbacks: duplicates, out-of-order delivery, late arrivals."""

from app.models import PaymentStatus
from app.payments import CLEARING_ACCOUNT
from tests.conftest import explain


def _confirmed(service, key, amount):
    pid = service.create_payment(key, amount, "CNY")["payment_id"]
    service.confirm_payment(pid)
    return pid


def test_duplicate_callback_applied_once(service, ledger, channel, processor):
    pid = _confirmed(service, "cb-dup", 100)
    event = channel.emit(pid, 100, succeeded=True)
    channel.duplicate(event)
    channel.duplicate(event)
    results = processor.handle_many(channel.events())
    actions = [r["action"] for r in results]
    explain("cb-dup", service.get(pid).status.value, f"3 identical events => actions={actions}")
    assert actions == ["applied", "duplicate_ignored", "duplicate_ignored"]
    assert ledger.net_amount(pid, CLEARING_ACCOUNT) == -100


def test_out_of_order_callbacks_converge(service, ledger, channel, processor):
    pids = [_confirmed(service, f"cb-ooo-{i}", 100 + i) for i in range(5)]
    for pid in pids:
        channel.emit(pid, service.get(pid).amount, succeeded=True)
    shuffled = channel.shuffled_events(seed=42)
    assert [e.payment_id for e in shuffled] != pids  # genuinely out of order
    results = processor.handle_many(shuffled)
    explain("cb-ooo-*", "SUCCEEDED", f"shuffled order {[e.event_id for e in shuffled]} all applied")
    assert all(r["action"] == "applied" for r in results)
    assert all(service.get(p).status is PaymentStatus.SUCCEEDED for p in pids)


def test_late_callback_does_not_change_terminal_result(service, channel, processor):
    pid = _confirmed(service, "cb-late", 200)
    fail = channel.emit(pid, 200, succeeded=False)
    assert processor.handle(fail)["action"] == "applied"
    late = channel.emit(pid, 200, succeeded=True)  # late contradicting success
    result = processor.handle(late)
    explain("cb-late", service.get(pid).status.value, f"late success after FAILED => action={result['action']}")
    assert result["action"] == "rejected"
    assert service.get(pid).status is PaymentStatus.FAILED


def test_one_bad_event_does_not_block_others(service, channel, processor):
    good = _confirmed(service, "cb-good", 150)
    evt_good = channel.emit(good, 150, succeeded=True)
    evt_orphan = channel.emit("pay_unknown", 999, succeeded=True)
    results = processor.handle_many([evt_orphan, evt_good])
    explain("cb-good", service.get(good).status.value, f"orphan event action={results[0]['action']} does not block good event")
    assert results[0]["action"] == "rejected"
    assert results[1]["action"] == "applied"
    assert service.get(good).status is PaymentStatus.SUCCEEDED
