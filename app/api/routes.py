import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.project import ModelProfile
from app.core.models.verification import EvidenceReport
from app.core.report_markdown import render_markdown
from app.orchestration.runner import RunManager
from app.services.setup import ApiForm, OracleForm, ResponseOverrideForm, SetupService

router = APIRouter(prefix="/api")
STREAM_INTERVAL_SECONDS = 0.7


class ProjectForm(BaseModel):
    name: str
    environment: str = "local"


class RunForm(BaseModel):
    request: str


class ResumeForm(BaseModel):
    action: str = "retry"
    text: str = ""


def setup_service(request: Request) -> SetupService:
    return request.app.state.setup


def run_manager(request: Request) -> RunManager:
    return request.app.state.runs


def project_view(service: SetupService, project_id: str) -> dict[str, Any]:
    project = service.refresh_knowledge_status(service.project(project_id))
    data = project.model_dump(mode="json")
    data["is_draft"] = project.is_draft
    data["oracle"]["password_set"] = project.oracle.password_ref is not None
    data["api"]["credential_set"] = project.api.credential_ref is not None
    data["oracle"].pop("password_ref", None)
    data["api"].pop("credential_ref", None)
    data["api"].pop("headers_ref", None)
    return data


@router.get("/projects")
def list_projects(service: SetupService = Depends(setup_service)) -> list[dict[str, Any]]:
    return [project_view(service, project.id) for project in service.container.projects.list()]


@router.post("/projects")
def create_project(form: ProjectForm, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    return project_view(service, service.create_project(form.name, form.environment).id)


@router.get("/projects/{project_id}")
def get_project(project_id: str, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    return project_view(service, project_id)


@router.put("/projects/{project_id}/oracle")
def update_oracle(project_id: str, form: OracleForm, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    service.update_oracle(project_id, form)
    return project_view(service, project_id)


@router.post("/projects/{project_id}/oracle/test")
async def test_oracle(project_id: str, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    await service.test_oracle(project_id)
    return project_view(service, project_id)


@router.get("/projects/{project_id}/oracle/tables")
async def oracle_tables(project_id: str, service: SetupService = Depends(setup_service)) -> list[dict[str, Any]]:
    return await service.schema_tables(project_id)


@router.put("/projects/{project_id}/api-profile")
def update_api(project_id: str, form: ApiForm, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    service.update_api(project_id, form)
    return project_view(service, project_id)


@router.post("/projects/{project_id}/api-profile/test")
async def test_api(project_id: str, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    await service.test_api(project_id)
    return project_view(service, project_id)


@router.post("/projects/{project_id}/openapi")
async def upload_openapi(project_id: str, file: UploadFile = File(...), service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    return service.upload_openapi(project_id, file.filename or "openapi.json", await file.read())


@router.get("/projects/{project_id}/operations")
def operations(project_id: str, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    return service.catalog_preview(service.project(project_id))


@router.post("/projects/{project_id}/documents")
async def upload_documents(project_id: str, files: list[UploadFile] = File(...), service: SetupService = Depends(setup_service)) -> list[dict[str, Any]]:
    payload = [(file.filename or "belge", await file.read()) for file in files]
    return [document.model_dump(mode="json", exclude={"stored_path"}) for document in service.upload_documents(project_id, payload)]


class CodePathsForm(BaseModel):
    paths: list[str]


@router.put("/projects/{project_id}/code-paths")
def set_code_paths(project_id: str, form: CodePathsForm, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    return service.set_code_paths(project_id, form.paths)


class SqlLogForm(BaseModel):
    path: str = ""


@router.put("/projects/{project_id}/sql-log")
def set_sql_log(project_id: str, form: SqlLogForm, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    service.set_sql_log_path(project_id, form.path)
    return project_view(service, project_id)


@router.get("/projects/{project_id}/stage-stats")
def stage_stats(project_id: str, service: SetupService = Depends(setup_service)) -> dict[str, float]:
    return service.container.runs.stage_durations(project_id)


class DocumentsOptionalForm(BaseModel):
    optional: bool


@router.put("/projects/{project_id}/documents-optional")
def set_documents_optional(project_id: str, form: DocumentsOptionalForm, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    service.set_documents_optional(project_id, form.optional)
    return project_view(service, project_id)


@router.get("/projects/{project_id}/documents")
def list_documents(project_id: str, service: SetupService = Depends(setup_service)) -> list[dict[str, Any]]:
    return [document.model_dump(mode="json", exclude={"stored_path"}) for document in service.documents(project_id)]


@router.put("/projects/{project_id}/model")
def update_model(project_id: str, profile: ModelProfile, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    service.update_model(project_id, profile)
    return project_view(service, project_id)


@router.post("/projects/{project_id}/model/probe")
async def probe_model(project_id: str, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    await service.probe_model(project_id)
    return project_view(service, project_id)


@router.put("/projects/{project_id}/response-profiles/{operation_id}")
def set_response_profile(project_id: str, operation_id: str, form: ResponseOverrideForm, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    service.set_response_override(project_id, operation_id, form)
    return project_view(service, project_id)


@router.delete("/projects/{project_id}/response-profiles/{operation_id}")
def clear_response_profile(project_id: str, operation_id: str, service: SetupService = Depends(setup_service)) -> dict[str, Any]:
    service.set_response_override(project_id, operation_id, None)
    return project_view(service, project_id)


@router.get("/projects/{project_id}/examples")
def example_requests(project_id: str, service: SetupService = Depends(setup_service)) -> list[str]:
    return service.example_requests(project_id)


@router.get("/projects/{project_id}/mappings")
def mappings(project_id: str, service: SetupService = Depends(setup_service)) -> list[dict[str, Any]]:
    return service.container.mappings.list_for_project(project_id)


@router.delete("/projects/{project_id}/mappings/{operation_id}")
def delete_mapping(project_id: str, operation_id: str, service: SetupService = Depends(setup_service)) -> dict[str, str]:
    service.container.mappings.delete(project_id, operation_id)
    return {"deleted": operation_id}


@router.get("/projects/{project_id}/runs")
def project_runs(project_id: str, service: SetupService = Depends(setup_service)) -> list[dict[str, Any]]:
    return service.container.runs.list_for_project(project_id)


@router.post("/projects/{project_id}/runs")
def start_run(project_id: str, form: RunForm, manager: RunManager = Depends(run_manager)) -> dict[str, str]:
    return {"run_id": manager.start(project_id, form.request)}


@router.get("/runs/{run_id}")
def get_run(run_id: str, manager: RunManager = Depends(run_manager)) -> dict[str, Any]:
    record = manager.container.runs.get(run_id)
    if record is None:
        raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Çalışma bulunamadı")
    record["active"] = manager.is_active(run_id)
    return record


@router.get("/runs/{run_id}/events")
def run_events(run_id: str, after: int = 0, manager: RunManager = Depends(run_manager)) -> list[dict[str, Any]]:
    return manager.container.runs.events(run_id, after)


def sse_message(event: str, data: dict[str, Any], identifier: int | None = None) -> str:
    prefix = f"id: {identifier}\n" if identifier is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request, after: int = 0, manager: RunManager = Depends(run_manager)) -> StreamingResponse:
    if manager.container.runs.get(run_id) is None:
        raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Çalışma bulunamadı")

    async def events():
        last, status = after, None
        while not await request.is_disconnected():
            batch = manager.container.runs.events(run_id, last)
            for event in batch:
                last = event["id"]
                yield sse_message("run_event", event, last)
            record = manager.container.runs.get(run_id)
            active = manager.is_active(run_id)
            if record["status"] != status or batch:
                status = record["status"]
                yield sse_message("run_status", {"status": status, "active": active})
            if not active and not batch and record["status"] != "RUNNING":
                return
            await asyncio.sleep(STREAM_INTERVAL_SECONDS)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: str, form: ResumeForm, manager: RunManager = Depends(run_manager)) -> dict[str, str]:
    manager.resume(run_id, form.model_dump())
    return {"run_id": run_id}


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, manager: RunManager = Depends(run_manager)) -> dict[str, str]:
    manager.cancel(run_id)
    return {"run_id": run_id}


@router.post("/runs/{run_id}/rerun")
def rerun(run_id: str, manager: RunManager = Depends(run_manager)) -> dict[str, str]:
    record = manager.container.runs.get(run_id)
    if record is None:
        raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Çalışma bulunamadı")
    return {"run_id": manager.start(record["project_id"], record["user_request"])}


def stored_report(manager: RunManager, run_id: str) -> EvidenceReport:
    record = manager.container.runs.get(run_id)
    if record is None or "report" not in record["payload"]:
        raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Bu çalışma için rapor henüz yok")
    return EvidenceReport.model_validate(record["payload"]["report"])


@router.get("/runs/{run_id}/report.json")
def report_json(run_id: str, manager: RunManager = Depends(run_manager)) -> JSONResponse:
    report = stored_report(manager, run_id)
    return JSONResponse(report.model_dump(mode="json"), headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})


@router.get("/runs/{run_id}/report.md")
def report_markdown(run_id: str, manager: RunManager = Depends(run_manager)) -> PlainTextResponse:
    report = stored_report(manager, run_id)
    return PlainTextResponse(render_markdown(report), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'})
