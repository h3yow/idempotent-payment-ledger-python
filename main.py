import uvicorn
from fastapi import FastAPI

from app.api import build_service, register_error_handlers
from app.api import router as payment_router


def create_app() -> FastAPI:
    app = FastAPI(title="Idempotent Payment Ledger")
    app.state.payment_service = build_service()
    app.include_router(payment_router)
    register_error_handlers(app)

    @app.get("/")
    def read_root():
        return {"Hello": "World"}

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
