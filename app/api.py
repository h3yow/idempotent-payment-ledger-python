"""FastAPI routes wiring the payment service."""
from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .channel import LocalChannel
from .errors import PaymentError
from .idempotency import IdempotencyStore
from .ledger import Ledger
from .models import ChannelEvent, ChannelEventType
from .reconcile import reconcile
from .service import PaymentService

router = APIRouter()


class CreatePaymentRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    amount: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


class CallbackRequest(BaseModel):
    event_id: str
    payment_id: str
    event_type: ChannelEventType
    amount: int
    currency: str
    reason: str | None = None


def get_service(request: Request) -> PaymentService:
    return request.app.state.payment_service


@router.get("/payments/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/payments", status_code=201)
def create_payment(body: CreatePaymentRequest, request: Request):
    svc = get_service(request)
    status_code, payload = svc.create_payment(
        body.idempotency_key, body.amount, body.currency
    )
    return JSONResponse(status_code=status_code, content=payload)


@router.post("/payments/{payment_id}/confirm")
def confirm_payment(payment_id: str, request: Request, succeed: bool = True):
    svc = get_service(request)
    intent = svc.confirm(payment_id, succeed=succeed)
    return svc._render(intent)


@router.post("/payments/{payment_id}/expire")
def expire_payment(payment_id: str, request: Request):
    svc = get_service(request)
    intent = svc.expire(payment_id)
    return svc._render(intent)


@router.post("/callbacks")
def receive_callback(body: CallbackRequest, request: Request):
    svc = get_service(request)
    return svc.handle_callback(
        ChannelEvent(
            event_id=body.event_id,
            payment_id=body.payment_id,
            event_type=body.event_type,
            amount=body.amount,
            currency=body.currency,
            reason=body.reason,
        )
    )


@router.get("/payments/{payment_id}")
def get_payment(payment_id: str, request: Request):
    svc = get_service(request)
    return svc._render(svc.get_payment(payment_id))


@router.get("/payments")
def list_payments(request: Request):
    svc = get_service(request)
    return [svc._render(i) for i in svc.list_payments()]


@router.get("/ledger/balance")
def ledger_balance(request: Request):
    svc = get_service(request)
    return {"net_balance": svc.ledger.net_balance()}


@router.get("/reconcile")
def run_reconcile(request: Request):
    svc = get_service(request)
    diffs = reconcile(svc.list_payments(), svc.channel.statement())
    return {
        "diff_count": len(diffs),
        "diffs": [
            {
                "kind": d.kind.value,
                "payment_id": d.payment_id,
                "detail": d.detail,
                "local_amount": d.local_amount,
                "channel_amount": d.channel_amount,
            }
            for d in diffs
        ],
    }


def build_service() -> PaymentService:
    return PaymentService(Ledger(), IdempotencyStore(), LocalChannel())


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PaymentError)
    async def payment_error_handler(request: Request, exc: PaymentError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
