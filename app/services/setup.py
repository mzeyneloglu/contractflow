from typing import Any

from pydantic import BaseModel, Field

from app.adapters.spring_boot.adapter import SpringBootAdapter
from pathlib import Path

from app.code.repository import load_index
from app.connectors.http.executor import HttpExecutor
from app.connectors.http.request import parse_header_lines
from app.connectors.oracle_mcp.client import OracleMcpConnector
from app.connectors.openapi.parser import parse_catalog
from app.core.models.common import ContractFlowError, ErrorCode, utc_now
from app.core.models.documents import DocumentRecord, DocumentStatus
from app.core.models.project import ApiProfile, AuthType, ComponentState, ComponentStatus, ModelProfile, OracleConnectionProfile, ProjectProfile, SecretReference
from app.core.models.service import PaginationStrategy
from app.core.models.verification import HttpRequestSpec
from app.model_providers.ollama import check_ollama
from app.model_providers.probe import probe_capabilities
from app.secrets.store import secret_key
from app.services.container import Container


class OracleForm(BaseModel):
    host: str = ""
    port: int = 1521
    service_name: str = ""
    username: str = ""
    password: str = ""
    schema_allowlist: list[str] = Field(default_factory=list)
    capture_runtime_sql: bool = False
    service_db_users: list[str] = Field(default_factory=list)


class ResponseOverrideForm(BaseModel):
    items_path: str = ""
    total_path: str = ""
    id_path: str = "id"
    pagination: PaginationStrategy = PaginationStrategy.PAGE_NUMBER
    page_param: str = "page"
    size_param: str = "size"
    first_page: int = 0
    page_size: int = Field(default=100, ge=1, le=1000)


class HeaderRow(BaseModel):
    name: str = ""
    value: str = ""


class ApiForm(BaseModel):
    base_url: str = ""
    auth_type: AuthType = AuthType.NONE
    username: str = ""
    credential: str = ""
    api_key_name: str = ""
    health_path: str = ""
    verify_tls: bool = True
    headers: list[HeaderRow] | None = None


def status(state: ComponentState, message: str) -> ComponentStatus:
    return ComponentStatus(state=state, message=message, checked_at=utc_now())


class SetupService:
    def __init__(self, container: Container):
        self.container = container

    def project(self, project_id: str) -> ProjectProfile:
        project = self.container.projects.get(project_id)
        if project is None:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Proje bulunamadı")
        return project

    def create_project(self, name: str, environment: str) -> ProjectProfile:
        if not name.strip():
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Proje adı boş olamaz")
        project = ProjectProfile(name=name.strip(), environment=environment.strip() or "local")
        project.model.base_url = self.container.settings.ollama_url
        project.model.model = self.container.settings.decision_model
        project.model.embedding_model = self.container.settings.embedding_model
        return self.container.projects.save(project)

    def update_oracle(self, project_id: str, form: OracleForm) -> ProjectProfile:
        project = self.project(project_id)
        password_ref = project.oracle.password_ref
        if form.password:
            password_ref = self.container.secrets.put(secret_key(project.id, "oracle_password"), form.password)
        project.oracle = OracleConnectionProfile(
            host=form.host.strip(),
            port=form.port,
            service_name=form.service_name.strip(),
            username=form.username.strip(),
            password_ref=password_ref,
            schema_allowlist=form.schema_allowlist,
            capture_runtime_sql=form.capture_runtime_sql,
            service_db_users=form.service_db_users,
        )
        project.readiness.oracle = status(ComponentState.PENDING, "Bağlantı test edilmedi")
        return self.container.projects.save(project)

    async def test_oracle(self, project_id: str) -> ProjectProfile:
        project = self.project(project_id)
        if not project.oracle.is_complete:
            project.readiness.oracle = status(ComponentState.FAILED, "Host, service name, kullanıcı, parola ve en az bir şema gerekli")
            return self.container.projects.save(project)
        async with OracleMcpConnector(project.oracle, self.container.settings) as connector:
            ping = await connector.call("ping")
            tables = await connector.call("list_tables") if ping.get("ok") else ping
        if ping.get("ok") and tables.get("ok"):
            message = f"Oracle {ping['server_version']}; {len(tables['tables'])} izinli tablo/view; MCP araçları: {len(connector.discovered_tools)}"
            project.readiness.oracle = status(ComponentState.READY, message)
        else:
            project.readiness.oracle = status(ComponentState.FAILED, f"{tables.get('error_code')}: {tables.get('message', '')}")
        return self.container.projects.save(project)

    async def schema_tables(self, project_id: str) -> list[dict[str, Any]]:
        project = self.project(project_id)
        async with OracleMcpConnector(project.oracle, self.container.settings) as connector:
            result = await connector.call("list_tables")
        if not result.get("ok"):
            raise ContractFlowError(ErrorCode(result.get("error_code", "DATABASE_ERROR")), result.get("message", ""))
        return result["tables"]

    def update_api(self, project_id: str, form: ApiForm) -> ProjectProfile:
        project = self.project(project_id)
        credential_ref = project.api.credential_ref if form.auth_type is project.api.auth_type else None
        if form.credential:
            credential_ref = self.container.secrets.put(secret_key(project.id, "api_credential"), form.credential)
        header_names, headers_ref = project.api.header_names, project.api.headers_ref
        if form.headers is not None:
            header_names, headers_ref = self._store_headers(project, form.headers)
        project.api = ApiProfile(
            header_names=header_names,
            headers_ref=headers_ref,
            base_url=form.base_url.strip().rstrip("/"),
            auth_type=form.auth_type,
            username=form.username.strip(),
            credential_ref=credential_ref if form.auth_type is not AuthType.NONE else None,
            api_key_name=form.api_key_name.strip(),
            health_path=form.health_path.strip(),
            verify_tls=form.verify_tls,
        )
        project.readiness.api = status(ComponentState.PENDING, "Servis kontrol edilmedi")
        return self.container.projects.save(project)

    def _store_headers(self, project: ProjectProfile, rows: list[HeaderRow]) -> tuple[list[str], SecretReference | None]:
        existing = parse_header_lines(self.container.secrets.get(project.api.headers_ref)) if project.api.headers_ref else {}
        merged: dict[str, str] = {}
        for row in rows:
            name = row.name.strip()
            if not name:
                continue
            value = row.value.strip() or existing.get(name, "")
            if not value:
                raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"'{name}' header'ı için değer gerekli")
            merged[name] = value
        if not merged:
            if project.api.headers_ref:
                self.container.secrets.delete(project.api.headers_ref)
            return [], None
        reference = self.container.secrets.put(secret_key(project.id, "api_headers"), "\n".join(f"{name}: {value}" for name, value in merged.items()))
        return list(merged), reference

    async def test_api(self, project_id: str) -> ProjectProfile:
        project = self.project(project_id)
        if not project.api.is_complete:
            project.readiness.api = status(ComponentState.FAILED, "Base URL ve seçilen kimlik doğrulama bilgileri gerekli")
            return self.container.projects.save(project)
        spec = HttpRequestSpec(
            base_url=project.api.base_url,
            path_template=project.api.health_path or "/",
            auth_type=project.api.auth_type,
            auth_username=project.api.username,
            auth_secret=project.api.credential_ref,
            api_key_name=project.api.api_key_name,
            header_names=project.api.header_names,
            headers_secret=project.api.headers_ref,
            verify_tls=project.api.verify_tls,
            timeout_seconds=10,
        )
        failure = await HttpExecutor(self.container.secrets.get).probe(spec)
        if failure is None or (failure.code is ErrorCode.NOT_FOUND and not project.api.health_path):
            project.readiness.api = status(ComponentState.READY, f"Servis erişilebilir: {project.api.base_url}")
        else:
            project.readiness.api = status(ComponentState.FAILED, f"{failure.code.value}: {failure.message}")
        return self.container.projects.save(project)

    def upload_openapi(self, project_id: str, filename: str, content: bytes) -> dict[str, Any]:
        project = self.project(project_id)
        catalog = parse_catalog(content)
        project.openapi_document_id = self.container.openapi.save(project.id, filename, catalog)
        self.container.projects.save(project)
        return self.catalog_preview(project)

    def catalog_preview(self, project: ProjectProfile) -> dict[str, Any]:
        catalog = self.container.openapi.get(project.openapi_document_id) if project.openapi_document_id else None
        if catalog is None:
            return {"loaded": False, "operations": []}
        adapter = SpringBootAdapter()
        operations = []
        for operation in catalog.operations:
            profile = adapter.response_profile(operation) if operation.executable else None
            operations.append(
                {
                    "operation_id": operation.operation_id,
                    "method": operation.method,
                    "path": operation.path,
                    "summary": operation.summary,
                    "executable": operation.executable,
                    "parameters": [f"{parameter.name} ({parameter.location.value}{', zorunlu' if parameter.required else ''})" for parameter in operation.parameters],
                    "response_profile": profile.model_dump(include={"items_path", "total_path", "id_path", "pagination"}, mode="json") if profile else None,
                    "unsupported": operation.unsupported_features,
                }
            )
        header_sets = [
            {parameter.name for parameter in operation.parameters if parameter.required and parameter.location.value == "header"}
            for operation in catalog.operations
            if operation.executable
        ]
        required_headers = sorted(set.intersection(*header_sets) if header_sets else set(), key=str.lower)
        return {
            "loaded": True,
            "required_headers": required_headers,
            "title": catalog.title,
            "version": catalog.source_version,
            "servers": catalog.servers,
            "warnings": catalog.warnings,
            "security_schemes": [scheme.model_dump(mode="json") for scheme in catalog.security_schemes],
            "operations": operations,
        }

    def set_response_override(self, project_id: str, operation_id: str, form: ResponseOverrideForm | None) -> ProjectProfile:
        project = self.project(project_id)
        if form is None:
            project.response_overrides.pop(operation_id, None)
        else:
            catalog = self.container.openapi.get(project.openapi_document_id) if project.openapi_document_id else None
            if catalog is None or catalog.find(operation_id) is None:
                raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"Operasyon katalogda yok: {operation_id}")
            project.response_overrides[operation_id] = form.model_dump(mode="json")
        return self.container.projects.save(project)

    def set_code_paths(self, project_id: str, paths: list[str]) -> dict[str, Any]:
        project = self.project(project_id)
        cleaned = [path.strip().strip('"') for path in paths if path.strip()]
        missing = [path for path in cleaned if not Path(path).is_dir()]
        if missing:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"Klasör bulunamadı: {', '.join(missing)}")
        project.code_paths = cleaned
        self.container.projects.save(project)
        index = load_index(cleaned) if cleaned else None
        return {"paths": cleaned, "files": len(index.files) if index else 0}

    def set_sql_log_path(self, project_id: str, path: str) -> ProjectProfile:
        project = self.project(project_id)
        cleaned = path.strip().strip('"')
        if cleaned and not Path(cleaned).is_file():
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"Log dosyası bulunamadı: {cleaned}")
        project.sql_log_path = cleaned
        return self.container.projects.save(project)

    def set_documents_optional(self, project_id: str, optional: bool) -> ProjectProfile:
        project = self.project(project_id)
        project.documents_optional = optional
        return self.container.projects.save(project)

    def example_requests(self, project_id: str, limit: int = 4) -> list[str]:
        project = self.project(project_id)
        examples: list[str] = []
        for run in self.container.runs.list_for_project(project_id, limit=30):
            request = " ".join(run["user_request"].split())
            if any(character.isdigit() for character in request) and request not in examples:
                examples.append(request)
        catalog = self.container.openapi.get(project.openapi_document_id) if project.openapi_document_id else None
        for operation in catalog.operations if catalog else []:
            if len(examples) >= limit:
                break
            if not operation.executable:
                continue
            for parameter in operation.parameters:
                value = parameter.example if parameter.example is not None else parameter.default if parameter.default is not None else (parameter.enum[0] if parameter.enum else None)
                if parameter.location.value == "query" and parameter.required and value is not None:
                    resource = next((segment for segment in reversed(operation.path.split("/")) if segment and not segment.startswith("{")), operation.operation_id)
                    examples.append(f"{resource} servisinde {parameter.name}'si {value} olan kayıtları API ile Oracle'da karşılaştır.")
                    break
        return examples[:limit]

    def upload_documents(self, project_id: str, files: list[tuple[str, bytes]]) -> list[DocumentRecord]:
        project = self.project(project_id)
        return [self.container.indexer.submit(project, filename, content) for filename, content in files]

    def documents(self, project_id: str) -> list[DocumentRecord]:
        return self.container.documents.list_for_project(project_id, include_superseded=True)

    def update_model(self, project_id: str, profile: ModelProfile) -> ProjectProfile:
        project = self.project(project_id)
        project.model = profile
        project.readiness.ollama = status(ComponentState.PENDING, "Model kontrol edilmedi")
        return self.container.projects.save(project)

    async def probe_model(self, project_id: str) -> ProjectProfile:
        project = self.project(project_id)
        try:
            missing = check_ollama(project.model)
            if missing:
                project.readiness.ollama = status(ComponentState.FAILED, "Eksik modeller: " + "; ".join(missing))
            else:
                result = await probe_capabilities(project.model)
                project.readiness.ollama = status(ComponentState.READY, f"{result.decision_model}: yapılandırılmış aksiyon ✓; {result.embedding_model}: {result.embedding_dimension} boyut ✓")
        except ContractFlowError as error:
            project.readiness.ollama = status(ComponentState.FAILED, f"{error.code.value}: {error.message}")
        return self.container.projects.save(project)

    def refresh_knowledge_status(self, project: ProjectProfile) -> ProjectProfile:
        documents = self.container.documents.list_for_project(project.id)
        ready = [document for document in documents if document.status is DocumentStatus.READY]
        busy = [document for document in documents if document.status in {DocumentStatus.QUEUED, DocumentStatus.PARSING, DocumentStatus.EMBEDDING}]
        if not documents and project.documents_optional:
            project.readiness.knowledge_base = status(ComponentState.READY, "İsteğe bağlı: belgesiz çalışılır")
        elif busy:
            project.readiness.knowledge_base = status(ComponentState.PENDING, f"{len(busy)} belge indeksleniyor")
        elif ready:
            project.readiness.knowledge_base = status(ComponentState.READY, f"{len(ready)} belge hazır")
        else:
            project.readiness.knowledge_base = status(ComponentState.FAILED if documents else ComponentState.UNKNOWN, "Hazır belge yok")
        return project
