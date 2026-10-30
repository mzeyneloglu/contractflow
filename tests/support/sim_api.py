import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException

from tests.support.simulation import active_orders, service_orders

SIMULATION_TOKEN = "sim-token"


def create_app(database: Path, missing_ids: int = 0) -> FastAPI:
    app = FastAPI(title="ContractFlow test simülasyon servisi")

    @app.get("/orders")
    def list_orders(customerId: int, page: int = 0, size: int = 20, authorization: str = Header(default="")):
        if authorization != f"Bearer {SIMULATION_TOKEN}":
            raise HTTPException(status_code=401)
        ids = active_orders(database, customerId)
        visible = ids[: len(ids) - missing_ids] if missing_ids else ids
        return page_of(visible, page, size)

    @app.get("/orders/legacy")
    def list_legacy_orders(customerId: int, page: int = 0, size: int = 20, authorization: str = Header(default="")):
        if authorization != f"Bearer {SIMULATION_TOKEN}":
            raise HTTPException(status_code=401)
        return page_of(service_orders(database, "listLegacyOrders", customerId), page, size)

    return app


def page_of(ids: list[int], page: int, size: int) -> dict:
    start = page * size
    chunk = ids[start : start + size]
    return {"content": [{"orderId": order_id, "status": "A"} for order_id in chunk], "totalElements": len(ids), "number": page, "size": size, "last": start + size >= len(ids)}


class BackgroundServer:
    def __init__(self, app: FastAPI, port: int):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "BackgroundServer":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
