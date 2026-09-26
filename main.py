import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.channel import CallbackProcessor, SimulatedChannel
from app.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    PaymentError,
    PaymentNotFound,
    ValidationError,
)
from app.idempotency import IdempotencyStore
from app.ledger import Ledger
from app.models import ChannelEvent, ChannelEventType, ChannelRecord
from app.payments import CLEARING_ACCOUNT, PaymentService
from app.reconcile import reconcile

app = FastAPI(title="Idempotent Payment Ledger")

# Local in-memory infrastructure: no network, no credentials, no external services.
ledger = Ledger()
idempotency = IdempotencyStore()
payments = PaymentService(ledger, idempotency)
channel = SimulatedChannel()
callbacks = CallbackProcessor(payments)


class CreatePaymentRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    amount: int = Field(gt=0)
    currency: str = Field(min_length=1)


class ChannelCallbackRequest(BaseModel):
    event_id: str = Field(min_length=1)
    payment_id: str = Field(min_length=1)
    event_type: ChannelEventType
    amount: int = Field(gt=0)
    sequence: int = 0


class ChannelEmitRequest(BaseModel):
    payment_id: str = Field(min_length=1)
    amount: int = Field(gt=0)
    succeeded: bool = True


_ERROR_STATUS = {
    IdempotencyConflict: 409,
    InvalidStateTransition: 409,
    PaymentNotFound: 404,
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
        )
    )


@app.get("/ledger/entries")
def list_entries():
    return [
        {
            "entry_id": e.entry_id,
            "payment_id": e.payment_id,
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
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT)
    return {
        "ok": report.ok,
        "matched": report.matched,
        "diffs": [
            {
                "diff_type": d.diff_type.value,
                "payment_id": d.payment_id,
                "ledger_amount": d.ledger_amount,
                "channel_amount": d.channel_amount,
                "detail": d.detail,
            }
            for d in report.diffs
        ],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
