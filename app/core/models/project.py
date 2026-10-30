from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.core.models.common import new_id, utc_now


class SecretReference(BaseModel):
    key: str

    def __str__(self) -> str:
        return f"secret://{self.key}"


class AuthType(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    API_KEY_HEADER = "api_key_header"
    API_KEY_QUERY = "api_key_query"


class OracleConnectionProfile(BaseModel):
    host: str = ""
    port: int = 1521
    service_name: str = ""
    username: str = ""
    password_ref: SecretReference | None = None
    schema_allowlist: list[str] = Field(default_factory=list)
    capture_runtime_sql: bool = False
    service_db_users: list[str] = Field(default_factory=list)

    @field_validator("schema_allowlist", "service_db_users")
    @classmethod
    def normalize_schemas(cls, schemas: list[str]) -> list[str]:
        return sorted({schema.strip().upper() for schema in schemas if schema.strip()})

    @property
    def is_complete(self) -> bool:
        return bool(self.host and self.service_name and self.username and self.password_ref and self.schema_allowlist)


class ApiProfile(BaseModel):
    base_url: str = ""
    auth_type: AuthType = AuthType.NONE
    username: str = ""
    credential_ref: SecretReference | None = None
    api_key_name: str = ""
    health_path: str = ""
    verify_tls: bool = True
    header_names: list[str] = Field(default_factory=list)
    headers_ref: SecretReference | None = None

    @property
    def is_complete(self) -> bool:
        if not self.base_url:
            return False
        if self.auth_type is AuthType.NONE:
            return True
        if self.auth_type is AuthType.BASIC:
            return bool(self.username and self.credential_ref)
        if self.auth_type in {AuthType.API_KEY_HEADER, AuthType.API_KEY_QUERY}:
            return bool(self.api_key_name and self.credential_ref)
        return self.credential_ref is not None


class ModelProfile(BaseModel):
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "gemma3:4b"
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dimension: int = 1024
    temperature: float = 0.1
    num_ctx: int = 8192
    num_predict: int = 2048
    request_timeout_seconds: int = 300
    execution_mode: str = "structured_actions"
    max_steps_per_agent: int = 6
    max_model_calls_per_run: int = 24
    max_output_repair_attempts: int = 1
    model_concurrency: int = 1


class ComponentState(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    PENDING = "pending"
    FAILED = "failed"


class ComponentStatus(BaseModel):
    state: ComponentState = ComponentState.UNKNOWN
    message: str = ""
    checked_at: datetime | None = None


class ProjectReadiness(BaseModel):
    ollama: ComponentStatus = Field(default_factory=ComponentStatus)
    oracle: ComponentStatus = Field(default_factory=ComponentStatus)
    api: ComponentStatus = Field(default_factory=ComponentStatus)
    knowledge_base: ComponentStatus = Field(default_factory=ComponentStatus)


class ProjectProfile(BaseModel):
    id: str = Field(default_factory=lambda: new_id("prj"))
    name: str
    environment: str = "local"
    oracle: OracleConnectionProfile = Field(default_factory=OracleConnectionProfile)
    api: ApiProfile = Field(default_factory=ApiProfile)
    model: ModelProfile = Field(default_factory=ModelProfile)
    readiness: ProjectReadiness = Field(default_factory=ProjectReadiness)
    openapi_document_id: str | None = None
    response_overrides: dict[str, dict] = Field(default_factory=dict)
    documents_optional: bool = False
    code_paths: list[str] = Field(default_factory=list)
    sql_log_path: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def is_draft(self) -> bool:
        return not (self.oracle.is_complete and self.api.is_complete and self.openapi_document_id)
