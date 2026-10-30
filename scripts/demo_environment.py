import argparse
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.connectors.openapi.parser import parse_catalog
from app.core.models.project import ApiProfile, AuthType, OracleConnectionProfile, ProjectProfile
from app.core.models.verification import EntityMapping, PredicateOperator, ScopeFilter
from app.main import create_app
from app.secrets.store import secret_key
from app.services.container import build_container
from tests.support.demo_repo import write_demo_repository
from tests.support.sim_api import SIMULATION_TOKEN
from tests.support.sim_api import create_app as create_service_app
from tests.support.simulation import create_database, openapi_document

DEMO_PROJECT = "Sipariş Servisi (DEMO verisi)"
CORPUS = Path(__file__).resolve().parent.parent / "tests" / "evaluation" / "corpus"
SCENARIOS = [
    "orders servisinde customerId'si 7 olan siparişlerin sayısı 12 olmalı. İş kurallarına göre API ile Oracle'ı karşılaştır.",
    "legacy servisinde customerId'si 7 olan siparişlerin sayısı 12 olmalı. İş kurallarına göre API ile Oracle'ı karşılaştır.",
]
REQUIRED_SCOPE_COLUMNS = ("STATUS", "DELETED_AT")
ANSWERS = {
    "tenant": "Bu serviste tek tenant var; TENANT_ID filtresi gerekmez.",
    "restored": "RESTORED_AT dolu kayıtlar DELETED_AT dolu olsa da sayılır; aynı or_group içinde DELETED_AT IS NULL ve RESTORED_AT IS NOT NULL.",
    "test": "Test müşterisi filtresi bu müşteri için gerekmez (not_applicable).",
}


def remembered_mapping(operation_id: str) -> EntityMapping:
    source = "siparis_servisi_kurallari.md"
    return EntityMapping(
        operation_id=operation_id,
        base_table="APP.ORDERS",
        primary_key=["ID"],
        parameter_columns={"customerId": "CUSTOMER_ID"},
        scope_filters=[
            ScopeFilter(column="t0.STATUS", operator=PredicateOperator.EQ, values=["A"], rule_text="Bir sipariş yalnızca STATUS kolonu A olduğunda aktif kabul edilir.", verified_by=f"{source} (Aktif Sipariş Tanımı)"),
            ScopeFilter(column="t0.DELETED_AT", operator=PredicateOperator.IS_NULL, values=[], rule_text="DELETED_AT kolonu dolu olan kayıtlar silinmiş kabul edilir ve adet hesabına dahil edilmez.", verified_by=f"{source} (Silinmiş Kayıtlar)"),
        ],
        counting_unit="Benzersiz sipariş (orderId)",
        confirmed=True,
    )


def seed_memory(container, project: ProjectProfile) -> None:
    catalog = container.openapi.get(project.openapi_document_id)
    for operation_id in ("listOrders", "listLegacyOrders"):
        if container.mappings.get(project.id, operation_id, catalog.content_hash) is None:
            container.mappings.save(project.id, remembered_mapping(operation_id), catalog.content_hash)


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def prepare(data_dir: Path, service_port: int) -> tuple[Settings, Path]:
    settings = Settings(data_dir=data_dir / "data", secret_backend="file", oracle_mcp_module="tests.support.sim_oracle_server")
    container = build_container(settings)
    database = data_dir / "demo_oracle.sqlite3"
    existing = next((project for project in container.projects.list() if project.name == DEMO_PROJECT), None)
    if existing is not None:
        seed_memory(container, existing)
        return settings, database
    create_database(database)
    repository = write_demo_repository(data_dir / "demo-repo")
    project = ProjectProfile(name=DEMO_PROJECT, environment="demo", documents_optional=False, code_paths=[display_path(repository)])
    project.oracle = OracleConnectionProfile(
        host="demo-oracle",
        service_name=database.stem,
        username="DEMO_READER",
        password_ref=container.secrets.put(secret_key(project.id, "oracle_password"), "demo"),
        schema_allowlist=["APP"],
        capture_runtime_sql=True,
        service_db_users=["SERVICE_APP"],
    )
    project.api = ApiProfile(base_url=f"http://127.0.0.1:{service_port}", auth_type=AuthType.BEARER, credential_ref=container.secrets.put(secret_key(project.id, "api_credential"), SIMULATION_TOKEN))
    project.openapi_document_id = container.openapi.save(project.id, "orders-openapi.json", parse_catalog(openapi_document()))
    container.projects.save(project)
    seed_memory(container, project)
    for path in sorted(CORPUS.glob("*.md")):
        container.indexer.submit(project, path.name, path.read_bytes())
    container.indexer.executor.submit(lambda: None).result(timeout=900)
    container.indexer.executor.shutdown(wait=True)
    return settings, database


def serve(data_dir: Path, port: int, service_port: int) -> None:
    settings, database = prepare(data_dir, service_port)
    service = uvicorn.Server(uvicorn.Config(create_service_app(database), host="127.0.0.1", port=service_port, log_level="warning"))
    threading.Thread(target=service.run, daemon=True).start()
    uvicorn.run(create_app(build_container(settings)), host="127.0.0.1", port=port, log_level="warning")


def decide(waiting: dict) -> dict:
    if waiting["kind"] == "mapping":
        sql = waiting["details"]["sql"].upper()
        missing = [column for column in REQUIRED_SCOPE_COLUMNS if column not in sql]
        if missing:
            return {"action": "reject", "text": f"İş kuralına göre {', '.join(missing)} koşulları SQL'de olmalı."}
        return {"action": "approve"}
    question = (waiting.get("question") or "").lower()
    return {"action": "answer", "text": next((text for key, text in ANSWERS.items() if key in question), "Belgedeki kuralları uygula.")}


def drive(port: int) -> None:
    base = f"http://127.0.0.1:{port}"
    project = next(item for item in httpx.get(f"{base}/api/projects").json() if item["name"] == DEMO_PROJECT)
    for request in SCENARIOS:
        run_id = httpx.post(f"{base}/api/projects/{project['id']}/runs", json={"request": request}).json()["run_id"]
        print("started", run_id, flush=True)
        for _ in range(8):
            record = wait(base, run_id)
            waiting = (record.get("payload") or {}).get("waiting")
            if record["status"] in {"PASS", "FAIL", "INCONCLUSIVE", "ERROR", "CANCELLED"} or not waiting:
                break
            httpx.post(f"{base}/api/runs/{run_id}/resume", json=decide(waiting))
        print("finished", run_id, record["status"], flush=True)


def wait(base: str, run_id: str) -> dict:
    while True:
        record = httpx.get(f"{base}/api/runs/{run_id}").json()
        if record["status"] != "RUNNING" and not record["active"]:
            return record
        time.sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser(description="ContractFlow demo ortamı: simüle Oracle, örnek sipariş servisi ve örnek Java repo'su (DEMO verisi)")
    parser.add_argument("command", choices=["serve", "drive"])
    parser.add_argument("--data", default="demo-data")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--service-port", type=int, default=8091)
    arguments = parser.parse_args()
    if arguments.command == "serve":
        serve(Path(arguments.data).resolve(), arguments.port, arguments.service_port)
    else:
        drive(arguments.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
