import json
from datetime import datetime
from typing import Any

from app.core.models.common import RunStatus, new_id, utc_now
from app.core.models.documents import DocumentRecord, DocumentStatus
from app.core.models.project import ProjectProfile
from app.core.models.service import ServiceCatalog
from app.core.models.verification import EntityMapping
from app.persistence.database import Database, to_json


class ProjectRepository:
    def __init__(self, database: Database):
        self.database = database

    def save(self, project: ProjectProfile) -> ProjectProfile:
        project.updated_at = utc_now()
        self.database.execute(
            "INSERT INTO projects (id, name, payload, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, payload=excluded.payload, updated_at=excluded.updated_at",
            (project.id, project.name, project.model_dump_json(), project.updated_at.isoformat()),
        )
        return project

    def get(self, project_id: str) -> ProjectProfile | None:
        row = self.database.fetch_one("SELECT payload FROM projects WHERE id = ?", (project_id,))
        return ProjectProfile.model_validate_json(row["payload"]) if row else None

    def list(self) -> list[ProjectProfile]:
        rows = self.database.fetch_all("SELECT payload FROM projects ORDER BY updated_at DESC")
        return [ProjectProfile.model_validate_json(row["payload"]) for row in rows]


class OpenApiRepository:
    def __init__(self, database: Database):
        self.database = database

    def save(self, project_id: str, filename: str, catalog: ServiceCatalog) -> str:
        document_id = new_id("oas")
        self.database.execute(
            "INSERT INTO openapi_documents (id, project_id, filename, content_hash, catalog, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (document_id, project_id, filename, catalog.content_hash, catalog.model_dump_json(), utc_now().isoformat()),
        )
        return document_id

    def get(self, document_id: str) -> ServiceCatalog | None:
        row = self.database.fetch_one("SELECT catalog FROM openapi_documents WHERE id = ?", (document_id,))
        return ServiceCatalog.model_validate_json(row["catalog"]) if row else None


class DocumentRepository:
    def __init__(self, database: Database):
        self.database = database

    def save(self, document: DocumentRecord) -> DocumentRecord:
        document.updated_at = utc_now()
        self.database.execute(
            "INSERT INTO documents (id, project_id, status, payload, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, payload=excluded.payload, updated_at=excluded.updated_at",
            (document.id, document.project_id, document.status.value, document.model_dump_json(), document.updated_at.isoformat()),
        )
        return document

    def get(self, document_id: str) -> DocumentRecord | None:
        row = self.database.fetch_one("SELECT payload FROM documents WHERE id = ?", (document_id,))
        return DocumentRecord.model_validate_json(row["payload"]) if row else None

    def list_for_project(self, project_id: str, include_superseded: bool = False) -> list[DocumentRecord]:
        rows = self.database.fetch_all("SELECT payload FROM documents WHERE project_id = ? ORDER BY updated_at", (project_id,))
        documents = [DocumentRecord.model_validate_json(row["payload"]) for row in rows]
        if include_superseded:
            return documents
        return [document for document in documents if document.status is not DocumentStatus.SUPERSEDED]

    def find_by_filename(self, project_id: str, filename: str) -> list[DocumentRecord]:
        return [document for document in self.list_for_project(project_id) if document.filename == filename]

    def list_unfinished(self) -> list[DocumentRecord]:
        unfinished = (DocumentStatus.QUEUED.value, DocumentStatus.PARSING.value, DocumentStatus.EMBEDDING.value)
        rows = self.database.fetch_all("SELECT payload FROM documents WHERE status IN (?, ?, ?)", unfinished)
        return [DocumentRecord.model_validate_json(row["payload"]) for row in rows]


class IndexGenerationRepository:
    def __init__(self, database: Database):
        self.database = database

    def active(self, project_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM index_generations WHERE project_id = ? AND active = 1", (project_id,))
        return dict(row) if row else None

    def find(self, project_id: str, embedding_model: str, embedding_digest: str, dimension: int) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            "SELECT * FROM index_generations WHERE project_id = ? AND embedding_model = ? AND embedding_digest = ? AND dimension = ?",
            (project_id, embedding_model, embedding_digest, dimension),
        )
        return dict(row) if row else None

    def create(self, project_id: str, embedding_model: str, embedding_digest: str, dimension: int) -> str:
        generation = new_id("gen")
        self.database.execute(
            "INSERT INTO index_generations (project_id, generation, embedding_model, embedding_digest, dimension, active, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (project_id, generation, embedding_model, embedding_digest, dimension, utc_now().isoformat()),
        )
        return generation

    def activate(self, project_id: str, generation: str) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE index_generations SET active = 0 WHERE project_id = ?", (project_id,))
            connection.execute("UPDATE index_generations SET active = 1 WHERE project_id = ? AND generation = ?", (project_id, generation))


class RunRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, run_id: str, project_id: str, user_request: str) -> None:
        now = utc_now().isoformat()
        self.database.execute(
            "INSERT INTO runs (id, project_id, status, user_request, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, project_id, RunStatus.RUNNING.value, user_request, "{}", now, now),
        )

    def update(self, run_id: str, status: RunStatus, payload: dict[str, Any] | None = None) -> None:
        if payload is None:
            self.database.execute("UPDATE runs SET status = ?, updated_at = ? WHERE id = ?", (status.value, utc_now().isoformat(), run_id))
            return
        self.database.execute(
            "UPDATE runs SET status = ?, payload = ?, updated_at = ? WHERE id = ?",
            (status.value, to_json(payload), utc_now().isoformat(), run_id),
        )

    def get(self, run_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not row:
            return None
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        return record

    def list_for_project(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT id, project_id, status, user_request, created_at, updated_at FROM runs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        return [dict(row) for row in rows]

    def list_active(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all("SELECT id, project_id, status FROM runs WHERE status = ?", (RunStatus.RUNNING.value,))
        return [dict(row) for row in rows]

    def stage_durations(self, project_id: str, limit: int = 10) -> dict[str, float]:
        runs = self.database.fetch_all("SELECT id FROM runs WHERE project_id = ? AND status IN ('PASS', 'FAIL', 'INCONCLUSIVE') ORDER BY created_at DESC LIMIT ?", (project_id, limit))
        totals: dict[str, list[float]] = {}
        for run in runs:
            started: dict[str, datetime] = {}
            for event in self.events(run["id"]):
                if event.get("type") == "node_started":
                    started[event["node"]] = datetime.fromisoformat(event["at"])
                elif event.get("type") == "node_finished" and event.get("node") in started:
                    totals.setdefault(event["node"], []).append((datetime.fromisoformat(event["at"]) - started.pop(event["node"])).total_seconds())
        return {node: round(sum(values) / len(values), 1) for node, values in totals.items()}

    def add_event(self, run_id: str, event: dict[str, Any]) -> None:
        self.database.execute("INSERT INTO run_events (run_id, payload, created_at) VALUES (?, ?, ?)", (run_id, to_json(event), utc_now().isoformat()))

    def events(self, run_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        rows = self.database.fetch_all("SELECT id, payload FROM run_events WHERE run_id = ? AND id > ? ORDER BY id", (run_id, after_id))
        return [{"id": row["id"], **json.loads(row["payload"])} for row in rows]


class OperationResultRepository:
    def __init__(self, database: Database):
        self.database = database

    def get(self, run_id: str, key: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT payload FROM operation_results WHERE run_id = ? AND idempotency_key = ?", (run_id, key))
        return json.loads(row["payload"]) if row else None

    def put(self, run_id: str, key: str, payload: dict[str, Any]) -> None:
        self.database.execute(
            "INSERT OR REPLACE INTO operation_results (run_id, idempotency_key, payload, created_at) VALUES (?, ?, ?, ?)",
            (run_id, key, to_json(payload), utc_now().isoformat()),
        )


class MappingRepository:
    def __init__(self, database: Database):
        self.database = database

    def save(self, project_id: str, mapping: EntityMapping, source_version: str) -> None:
        self.database.execute(
            "INSERT OR REPLACE INTO mappings (project_id, operation_id, payload, source_version, updated_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, mapping.operation_id, mapping.model_dump_json(), source_version, utc_now().isoformat()),
        )

    def get(self, project_id: str, operation_id: str, source_version: str) -> EntityMapping | None:
        row = self.database.fetch_one(
            "SELECT payload FROM mappings WHERE project_id = ? AND operation_id = ? AND source_version = ?",
            (project_id, operation_id, source_version),
        )
        return EntityMapping.model_validate_json(row["payload"]) if row else None

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.database.fetch_all("SELECT operation_id, payload, source_version, updated_at FROM mappings WHERE project_id = ?", (project_id,))
        return [dict(row) for row in rows]

    def delete(self, project_id: str, operation_id: str) -> None:
        self.database.execute("DELETE FROM mappings WHERE project_id = ? AND operation_id = ?", (project_id, operation_id))
