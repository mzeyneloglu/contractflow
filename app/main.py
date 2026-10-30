import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.routes import router
from app.config import PROJECT_ROOT, get_settings
from app.core.models.common import ContractFlowError, ErrorCode
from app.orchestration.runner import RunManager
from app.services.container import Container, get_container
from app.services.setup import SetupService

UI_DIR = PROJECT_ROOT / "app" / "ui"
templates = Jinja2Templates(directory=str(UI_DIR / "templates"))
ERROR_STATUS = {ErrorCode.CONFIGURATION_ERROR: 400, ErrorCode.UNSUPPORTED_SPEC: 422}


def create_app(container: Container | None = None) -> FastAPI:
    container = container or get_container()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container
        app.state.setup = SetupService(container)
        app.state.runs = RunManager(container)
        container.indexer.resume_unfinished()
        app.state.runs.recover_interrupted()
        yield
        container.indexer.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="ContractFlow", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(UI_DIR / "static")), name="static")
    app.include_router(router)

    @app.exception_handler(ContractFlowError)
    async def contractflow_error(request: Request, error: ContractFlowError) -> JSONResponse:
        return JSONResponse({"error_code": error.code.value, "message": error.message}, status_code=ERROR_STATUS.get(error.code, 409))

    @app.post("/v1/traces")
    async def otlp_traces(request: Request) -> JSONResponse:
        if "json" not in request.headers.get("content-type", ""):
            return JSONResponse({"message": "Yalnızca OTLP/HTTP JSON desteklenir: OTEL_EXPORTER_OTLP_PROTOCOL=http/json"}, status_code=415)
        stored = container.otel.ingest(await request.json())
        return JSONResponse({"partialSuccess": {}, "stored": stored})

    @app.get("/", response_class=HTMLResponse)
    def single_page(request: Request):
        return templates.TemplateResponse(request, "app.html", {})

    @app.get("/projects/{project_id}/{view}")
    def legacy_project_view(project_id: str, view: str):
        return RedirectResponse(f"/#/projects/{project_id}/{view}")

    @app.get("/runs/{run_id}")
    def legacy_run_view(run_id: str):
        return RedirectResponse(f"/#/runs/{run_id}")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("ContractFlow yalnızca localhost'a bind olur (CONTRACTFLOW_HOST=127.0.0.1)")
    uvicorn.run(create_app(), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
