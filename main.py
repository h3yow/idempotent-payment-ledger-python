import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.channel import CallbackProcessor, SimulatedChannel
from app.errors import (
    CallbackMismatch,
    IdempotencyConflict,
    InvalidStateTransition,
    PaymentError,
    PaymentNotFound,
    RefundExceedsPayment,
    RefundNotFound,
    ValidationError,
)
from app.idempotency import IdempotencyStore
from app.ledger import Ledger
from app.models import ChannelEvent, ChannelEventType, ChannelRecord
from app.payments import CLEARING_ACCOUNT, PaymentService
from app.reconcile import reconcile
from app.refunds import RefundService

app = FastAPI(title="Idempotent Payment Ledger")

# Local in-memory infrastructure: no network, no credentials, no external services.
ledger = Ledger()
idempotency = IdempotencyStore()
payments = PaymentService(ledger, idempotency)
refunds = RefundService(payments, ledger)
channel = SimulatedChannel()
callbacks = CallbackProcessor(payments, refunds)


class CreatePaymentRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    amount: int = Field(gt=0)
    currency: str = Field(min_length=1)


class CreateRefundRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    amount: int = Field(gt=0)
    currency: str = Field(min_length=1)


class ChannelCallbackRequest(BaseModel):
    event_id: str = Field(min_length=1)
    payment_id: str = Field(min_length=1)
    event_type: ChannelEventType
    amount: int = Field(gt=0)
    sequence: int = 0
    refund_id: str | None = None
    currency: str = ""


class ChannelEmitRequest(BaseModel):
    payment_id: str = Field(min_length=1)
    amount: int = Field(gt=0)
    succeeded: bool = True


class ChannelEmitRefundRequest(BaseModel):
    refund_id: str = Field(min_length=1)
    payment_id: str = Field(min_length=1)
    amount: int = Field(gt=0)
    currency: str = ""
    succeeded: bool = True


_ERROR_STATUS = {
    IdempotencyConflict: 409,
    InvalidStateTransition: 409,
    PaymentNotFound: 404,
    RefundNotFound: 404,
    RefundExceedsPayment: 409,
    CallbackMismatch: 409,
    ValidationError: 422,
}


@app.exception_handler(PaymentError)
async def payment_error_handler(request: Request, exc: PaymentError) -> JSONResponse:
    status = next(
        (code for cls, code in _ERROR_STATUS.items() if isinstance(exc, cls)), 400
    )
    return JSONResponse(status_code=status, content={"error": exc.code, "detail": str(exc)})


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.post("/payments", status_code=201)
def create_payment(req: CreatePaymentRequest):
    return payments.create_payment(req.idempotency_key, req.amount, req.currency)


@app.get("/payments/{payment_id}")
def get_payment(payment_id: str):
    intent = payments.get(payment_id)
    return {
        "payment_id": intent.payment_id,
        "idempotency_key": intent.idempotency_key,
        "amount": intent.amount,
        "currency": intent.currency,
        "status": intent.status,
        "version": intent.version,
    }


@app.post("/payments/{payment_id}/confirm")
def confirm_payment(payment_id: str):
    intent = payments.confirm_payment(payment_id)
    return {"payment_id": intent.payment_id, "status": intent.status}


@app.post("/payments/{payment_id}/expire")
def expire_payment(payment_id: str):
    intent = payments.expire_payment(payment_id)
    return {"payment_id": intent.payment_id, "status": intent.status}


@app.post("/payments/{payment_id}/refunds", status_code=201)
def create_refund(payment_id: str, req: CreateRefundRequest):
    """Idempotently accept a (partial or full) refund for a succeeded payment."""
    return refunds.create_refund(
        payment_id, req.idempotency_key, req.amount, req.currency
    )


@app.get("/refunds/{refund_id}")
def get_refund(refund_id: str):
    refund = refunds.get(refund_id)
    return {
        "refund_id": refund.refund_id,
        "payment_id": refund.payment_id,
        "idempotency_key": refund.idempotency_key,
        "amount": refund.amount,
        "currency": refund.currency,
        "status": refund.status,
        "version": refund.version,
    }


@app.post("/channel/emit-refund", status_code=201)
def channel_emit_refund(req: ChannelEmitRefundRequest):
    """Simulated channel: settle a refund and emit a callback event."""
    event = channel.emit_refund(
        req.refund_id, req.payment_id, req.amount, req.currency, req.succeeded
    )
    return {"event_id": event.event_id, "sequence": event.sequence}


@app.post("/channel/emit", status_code=201)
def channel_emit(req: ChannelEmitRequest):
    """Simulated channel: settle a payment and emit a callback event."""
    event = channel.emit(req.payment_id, req.amount, req.succeeded)
    return {"event_id": event.event_id, "sequence": event.sequence}


@app.post("/callbacks")
def handle_callback(req: ChannelCallbackRequest):
    """Receive a channel callback; duplicate and out-of-order deliveries are safe."""
    return callbacks.handle(
        ChannelEvent(
            event_id=req.event_id,
            payment_id=req.payment_id,
            event_type=req.event_type,
            amount=req.amount,
            sequence=req.sequence,
            refund_id=req.refund_id,
            currency=req.currency,
        )
    )


@app.get("/ledger/entries")
def list_entries():
    return [
        {
            "entry_id": e.entry_id,
            "payment_id": e.payment_id,
            "refund_id": e.refund_id or None,
            "account": e.account,
            "direction": e.direction.value,
            "amount": e.amount,
            "memo": e.memo,
        }
        for e in ledger.entries()
    ]


@app.get("/reconcile")
def run_reconcile():
    """Reconcile the local ledger against the simulated channel statement."""
    local_outcomes = {**payments.terminal_outcomes(), **refunds.terminal_outcomes()}
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, local_outcomes)
    return {
        "ok": report.ok,
        "matched": report.matched,
        "diffs": [
            {
                "diff_type": d.diff_type.value,
                "payment_id": d.payment_id,
                "refund_id": d.refund_id,
                "ledger_amount": d.ledger_amount,
                "channel_amount": d.channel_amount,
                "detail": d.detail,
            }
            for d in report.diffs
        ],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
