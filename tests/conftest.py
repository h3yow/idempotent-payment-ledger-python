import logging

import pytest

from app.channel import LocalChannel
from app.idempotency import IdempotencyStore
from app.ledger import Ledger
from app.service import PaymentService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@pytest.fixture
def service() -> PaymentService:
    return PaymentService(Ledger(), IdempotencyStore(), LocalChannel())
