import logging

import pytest

from app.channel import CallbackProcessor, SimulatedChannel
from app.idempotency import IdempotencyStore
from app.ledger import Ledger
from app.payments import PaymentService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [TEST] %(message)s", force=True
)
log = logging.getLogger("payment-tests")


@pytest.fixture()
def ledger():
    return Ledger()


@pytest.fixture()
def service(ledger):
    return PaymentService(ledger, IdempotencyStore())


@pytest.fixture()
def channel():
    return SimulatedChannel()


@pytest.fixture()
def processor(service):
    return CallbackProcessor(service)


def explain(key: str, status: str, rationale: str) -> None:
    """Log idempotency key, status and the判定依据 for each assertion."""
    log.info("idempotency_key=%s status=%s rationale=%s", key, status, rationale)
