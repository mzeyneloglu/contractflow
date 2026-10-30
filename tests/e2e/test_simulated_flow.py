import json
import socket
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.connectors.openapi.parser import parse_catalog
from app.core.models.common import RunStatus
from app.core.models.documents import DocumentStatus
from app.core.models.project import ApiProfile, AuthType, OracleConnectionProfile, ProjectProfile
from app.orchestration.runner import RunManager
from app.rag.embeddings import installed_models
from app.secrets.store import secret_key
from app.services.container import build_container
from tests.support.sim_api import SIMULATION_TOKEN, BackgroundServer, create_app
from tests.support.simulation import create_database, openapi_document

CORPUS = Path(__file__).resolve().parent.parent / "evaluation" / "corpus"
RESULTS = Path(__file__).resolve().parent.parent.parent / "docs" / "evaluation_results"
RUN_TIMEOUT_SECONDS = 1200
MAX_INTERACTIONS = 5
REQUIRED_SCOPE_COLUMNS = ("STATUS", "DELETED_AT")
CLARIFICATION_ANSWERS = {
    "tenant": "Bu serviste tek tenant var; TENANT_ID filtresi gerekmez (not_applicable).",
    "restored": "Evet, RESTORED_AT dolu kayıtlar DELETED_AT dolu olsa da sayılır: aynı or_group içinde DELETED_AT IS NULL ve RESTORED_AT IS NOT NULL.",
    "customer_type": "Test müşterileri hariç tutulur: APP.CUSTOMERS tablosunu t1 alias'ıyla ekle (t1.ID = t0.CUSTOMER_ID) ve t1.CUSTOMER_TYPE <> 'TEST' filtresi uygula.",
    "test": "Test müşterileri hariç tutulur: APP.CUSTOMERS tablosunu t1 alias'ıyla ekle (t1.ID = t0.CUSTOMER_ID) ve t1.CUSTOMER_TYPE <> 'TEST' filtresi uygula.",
    "sayfa": "Sayfalama sayımı etkilemez (not_applicable).",
    "tekilleştir": "Sistem sayımı COUNT(DISTINCT ID) ile yapıyor; sayma birimi kuralı için ayrı filtre gerekmez (not_applicable).",
    "benzersiz": "Sistem sayımı COUNT(DISTINCT ID) ile yapıyor; sayma birimi kuralı için ayrı filtre gerekmez (not_applicable).",
}


def ollama_ready() -> bool:
    try:
        models = installed_models("http://127.0.0.1:11434", timeout=3)
    except Exception:
        return False
    return {"gemma3:4b", "qwen3-embedding:0.6b"} <= set(models)


pytestmark = [pytest.mark.ollama, pytest.mark.skipif(not ollama_ready(), reason="BLOCKED: yerel Ollama modelleri yok")]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_settled(container, run_id: str) -> dict:
    deadline = time.time() + RUN_TIMEOUT_SECONDS
    while time.time() < deadline:
        record = container.runs.get(run_id)
        if record["status"] != RunStatus.RUNNING.value:
            return record
        time.sleep(1)
    raise TimeoutError(run_id)


def human_decision(waiting: dict) -> tuple[dict, str]:
    if waiting["kind"] == "mapping":
        sql = waiting["details"]["sql"].upper()
        missing = [column for column in REQUIRED_SCOPE_COLUMNS if column not in sql]
        if missing:
            note = f"Aktif sipariş ve silinme kuralları SQL'de yok: {', '.join(missing)} koşulları eklenmeli."
            return {"action": "reject", "text": note}, f"Eşleştirme reddedildi: {note}"
        return {"action": "approve"}, "Eşleştirme onaylandı"
    question = waiting.get("question", "").lower()
    answer = next((text for keyword, text in CLARIFICATION_ANSWERS.items() if keyword in question), "Belgedeki kuralları uygula; belirsiz kural sayımı etkilemiyorsa not_applicable kabul et.")
    return {"action": "answer", "text": answer}, f"Soru: {waiting.get('question')} → Yanıt: {answer}"


@pytest.fixture
def simulated_project(tmp_path):
    database = create_database(tmp_path / "simulation.sqlite3")
    settings = Settings(data_dir=tmp_path / "data", secret_backend="file", oracle_mcp_module="tests.support.sim_oracle_server")
    container = build_container(settings)
    port = free_port()
    project = ProjectProfile(name="SIMULATION siparişler")
    project.oracle = OracleConnectionProfile(host="simulation", service_name=str(database), username="sim", password_ref=container.secrets.put(secret_key(project.id, "oracle_password"), "sim"), schema_allowlist=["APP"])
    project.api = ApiProfile(base_url=f"http://127.0.0.1:{port}", auth_type=AuthType.BEARER, credential_ref=container.secrets.put(secret_key(project.id, "api_credential"), SIMULATION_TOKEN))
    project.openapi_document_id = container.openapi.save(project.id, "openapi.json", parse_catalog(openapi_document()))
    container.projects.save(project)
    for path in sorted(CORPUS.glob("*.md")):
        container.indexer.submit(project, path.name, path.read_bytes())
    container.indexer.executor.submit(lambda: None).result(timeout=600)
    assert all(document.status is DocumentStatus.READY for document in container.documents.list_for_project(project.id))
    return container, project, database, port


def test_simulated_end_to_end_with_human_in_the_loop(simulated_project):
    container, project, database, port = simulated_project
    manager = RunManager(container)
    interactions: list[str] = []
    started = time.time()
    with BackgroundServer(create_app(database), port):
        run_id = manager.start(project.id, "orders servisinde customerId'si 7 olan siparişlerin sayısı 12 olmalı. İş kurallarına göre API ile Oracle'ı karşılaştır.")
        record = wait_until_settled(container, run_id)
        while RunStatus(record["status"]).is_waiting and len(interactions) < MAX_INTERACTIONS:
            answer, note = human_decision(record["payload"]["waiting"])
            interactions.append(note)
            manager.resume(run_id, answer)
            record = wait_until_settled(container, run_id)
    report = record["payload"].get("report", {})
    summary = {
        "label": "SIMULATION — Oracle yerine SQLite, hedef servis yerine test servisi; ajanlar gerçek gemma3:4b",
        "status": record["status"],
        "duration_seconds": round(time.time() - started),
        "interactions": interactions,
        "assertions": [(item["name"], item["status"], item["left_value"], item["right_value"]) for item in report.get("assertions", [])],
        "sql": (report.get("database") or {}).get("sql"),
        "binds": (report.get("database") or {}).get("binds"),
        "rules": [rule["text"] for rule in (report.get("rules") or {}).get("rules", [])],
        "events": [{key: value for key, value in event.items() if key in {"type", "agent", "tool", "message", "question", "status"}} for event in container.runs.events(run_id) if event["type"] not in {"node_started", "node_finished"}],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "simulation_e2e.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SIMULATION SUMMARY", json.dumps({key: summary[key] for key in ("status", "duration_seconds", "interactions", "assertions", "sql", "binds")}, ensure_ascii=False, indent=2))
    assert record["status"] == RunStatus.PASS.value
