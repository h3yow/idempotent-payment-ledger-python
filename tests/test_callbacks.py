"""Duplicate, out-of-order and late channel callbacks must be safe."""
import logging

from app.models import PaymentStatus

log = logging.getLogger("test.callbacks")


def test_duplicate_callback_is_noop(service):
    _, body = service.create_payment("key-CB1", 100, "CNY")
    pid = body["payment_id"]
    service.mark_processing(pid)
    event = service.channel.emit(pid, True, 100, "CNY")

    first = service.handle_callback(event)
    second = service.handle_callback(event)
    log.info(
        "key=%s payment=%s status=%s first=%s second=%s basis=event_id dedup",
        "key-CB1", pid, service.get_payment(pid).status.value,
        first["applied"], second["reason"],
    )
    assert first["applied"] is True
    assert second["applied"] is False
    assert second["reason"] == "DUPLICATE_EVENT"
    assert len(service.ledger.entries_for(pid)) == 2
    assert service.ledger.net_balance() == 0


def test_out_of_order_callback_does_not_change_terminal_state(service):
    _, body = service.create_payment("key-CB2", 100, "CNY")
    pid = body["payment_id"]
    service.mark_processing(pid)
    # Channel emits success then failure; delivered in reverse order.
    success = service.channel.emit(pid, True, 100, "CNY")
    failure = service.channel.emit(pid, False, 100, "CNY", reason="late_decline")

    r1 = service.handle_callback(failure)   # arrives first -> FAILED terminal
    r2 = service.handle_callback(success)   # late success must not reopen
    log.info(
        "key=%s payment=%s status=%s r1=%s r2=%s basis=terminal state absorbs late events",
        "key-CB2", pid, service.get_payment(pid).status.value, r1, r2,
    )
    assert r1["applied"] is True
    assert r2["applied"] is False
    assert r2["reason"] == "ALREADY_TERMINAL"
    assert service.get_payment(pid).status is PaymentStatus.FAILED
    assert service.ledger.entries_for(pid) == []  # no funds moved
    assert service.ledger.net_balance() == 0


def test_late_callback_after_success_keeps_result(service):
    _, body = service.create_payment("key-CB3", 100, "CNY")
    pid = body["payment_id"]
    service.confirm(pid)  # succeeds synchronously
    late = service.channel.emit(pid, False, 100, "CNY", reason="late_decline")
    result = service.handle_callback(late)
    log.info(
        "key=%s payment=%s status=%s reason=%s basis=SUCCEEDED is terminal",
        "key-CB3", pid, service.get_payment(pid).status.value, result["reason"],
    )
    assert result["applied"] is False
    assert service.get_payment(pid).status is PaymentStatus.SUCCEEDED
    assert len(service.ledger.entries_for(pid)) == 2


def test_callback_amount_mismatch_not_applied(service):
    _, body = service.create_payment("key-CB4", 100, "CNY")
    pid = body["payment_id"]
    service.mark_processing(pid)
    bad = service.channel.emit(pid, True, 999, "CNY")
    result = service.handle_callback(bad)
    log.info(
        "key=%s payment=%s status=%s reason=%s basis=amount mismatch rejected, stays PROCESSING",
        "key-CB4", pid, service.get_payment(pid).status.value, result["reason"],
    )
    assert result["applied"] is False
    assert result["reason"] == "AMOUNT_MISMATCH"
    assert service.get_payment(pid).status is PaymentStatus.PROCESSING
    assert service.ledger.net_balance() == 0


def test_callback_for_unknown_payment(service):
    event = service.channel.emit("pay_ghost", True, 100, "CNY")
    result = service.handle_callback(event)
    log.info(
        "payment=pay_ghost reason=%s basis=no local intent for channel event",
        result["reason"],
    )
    assert result["applied"] is False
    assert result["reason"] == "UNKNOWN_PAYMENT"
