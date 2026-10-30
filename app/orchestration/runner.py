import asyncio
import logging
import threading
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.agents.runtime import ModelBudget
from app.connectors.oracle_mcp.client import OracleMcpConnector
from app.core.models.common import ContractFlowError, ErrorCode, RunStatus, new_id, utc_now
from app.model_providers.ollama import OllamaModelProvider
from app.orchestration.context import RunContext
from app.orchestration.graph import build_graph, build_report
from app.services.container import Container

logger = logging.getLogger(__name__)


class RunManager:
    def __init__(self, container: Container):
        self.container = container
        self._cancelled: set[str] = set()
        self._active: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def start(self, project_id: str, user_request: str) -> str:
        project = self.container.projects.get(project_id)
        if project is None:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Proje bulunamadı")
        if not user_request.strip():
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Doğrulama talebi boş olamaz")
        run_id = new_id("run")
        self.container.runs.create(run_id, project_id, user_request.strip())
        initial = {"run_id": run_id, "project_id": project_id, "user_request": user_request.strip(), "status": RunStatus.RUNNING.value, "clarifications": [], "started_at": utc_now().isoformat(), "model_calls": 0}
        self._spawn(run_id, initial)
        return run_id

    def resume(self, run_id: str, answer: dict[str, Any]) -> None:
        record = self._require_run(run_id)
        if RunStatus(record["status"]).is_terminal:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Tamamlanmış çalışma devam ettirilemez")
        if self.is_active(run_id):
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Çalışma zaten devam ediyor")
        self.container.runs.update(run_id, RunStatus.RUNNING)
        self._spawn(run_id, Command(resume=answer))

    def cancel(self, run_id: str) -> None:
        record = self._require_run(run_id)
        if RunStatus(record["status"]).is_terminal:
            return
        self._cancelled.add(run_id)
        if not self.is_active(run_id):
            self.container.runs.update(run_id, RunStatus.RUNNING)
            self._spawn(run_id, Command(resume={"action": "cancel"}))

    def recover_interrupted(self) -> int:
        recovered = 0
        for record in self.container.runs.list_active():
            if not self.is_active(record["id"]):
                self.container.runs.add_event(record["id"], {"type": "recovered", "message": "Uygulama yeniden başladı; çalışma son checkpoint'ten sürdürülüyor"})
                self._spawn(record["id"], None)
                recovered += 1
        return recovered

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            thread = self._active.get(run_id)
            return thread is not None and thread.is_alive()

    def _require_run(self, run_id: str) -> dict[str, Any]:
        record = self.container.runs.get(run_id)
        if record is None:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Çalışma bulunamadı")
        return record

    def _spawn(self, run_id: str, graph_input: Any) -> None:
        thread = threading.Thread(target=self._thread_main, args=(run_id, graph_input), name=f"run-{run_id}", daemon=True)
        with self._lock:
            self._active[run_id] = thread
        thread.start()

    def _thread_main(self, run_id: str, graph_input: Any) -> None:
        try:
            asyncio.run(self._drive(run_id, graph_input))
        except Exception as error:
            logger.exception("Çalışma beklenmedik şekilde durdu: %s", run_id)
            self.container.runs.add_event(run_id, {"type": "run_error", "message": f"{type(error).__name__}: {str(error)[:300]}"})
            self.container.runs.update(run_id, RunStatus.ERROR, {"error": f"Beklenmeyen hata: {type(error).__name__}"})

    async def _drive(self, run_id: str, graph_input: Any) -> None:
        record = self._require_run(run_id)
        project = self.container.projects.get(record["project_id"])
        catalog = self.container.openapi.get(project.openapi_document_id) if project.openapi_document_id else None
        config = {"configurable": {"thread_id": run_id}}

        async def emit(event: dict[str, Any]) -> None:
            self.container.runs.add_event(run_id, {**event, "at": utc_now().isoformat()})

        context = RunContext(
            run_id=run_id,
            project=project,
            catalog=catalog,
            container=self.container,
            provider=OllamaModelProvider(project.model),
            budget=ModelBudget(project.model.max_model_calls_per_run),
            emit=emit,
            cancelled=lambda: run_id in self._cancelled,
        )
        async with AsyncExitStack() as stack:
            saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(self.container.settings.checkpoint_path)))
            if project.oracle.is_complete:
                try:
                    context.oracle = await stack.enter_async_context(OracleMcpConnector(project.oracle, self.container.settings))
                    await emit({"type": "mcp_connected", "tools": context.oracle.discovered_tools})
                except Exception as error:
                    await emit({"type": "mcp_error", "message": f"oracle_mcp başlatılamadı: {type(error).__name__}"})
            graph = build_graph(context).compile(checkpointer=saver)
            await graph.ainvoke(graph_input, config)
            snapshot = await graph.aget_state(config)
        await self._persist(run_id, snapshot, context, record)

    async def _persist(self, run_id: str, snapshot, context: RunContext, record: dict[str, Any]) -> None:
        values = dict(snapshot.values)
        status = RunStatus(values.get("status", RunStatus.ERROR.value))
        if snapshot.next and status.is_waiting:
            self.container.runs.update(run_id, status, {"waiting": values.get("waiting"), "state": public_state(values)})
            await context.emit({"type": "run_waiting", "status": status.value, "message": (values.get("waiting") or {}).get("message", "")})
            return
        started = datetime.fromisoformat(values.get("started_at") or record["created_at"])
        report = build_report(values, context, started, self.container.runs.events(run_id))
        self.container.runs.update(run_id, report.status, {"report": report.model_dump(mode="json")})
        self._cancelled.discard(run_id)
        await context.emit({"type": "run_finished", "status": report.status.value})


def public_state(values: dict[str, Any]) -> dict[str, Any]:
    keys = ("intent", "rules", "operation_id", "mapping", "sql", "binds", "api_observation", "db_observation", "snapshot", "clarifications", "code_evidence", "code_sql", "implementation")
    return {key: values.get(key) for key in keys if values.get(key) is not None}
