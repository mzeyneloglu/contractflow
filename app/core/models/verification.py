from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.models.common import ErrorCode, RunStatus, utc_now
from app.core.models.documents import SourceReference
from app.core.models.project import AuthType, SecretReference
from app.core.models.service import ResponseProfile


class FilterCondition(BaseModel):
    name: str
    value: str
    operator: Literal["eq"] = "eq"


class VerificationIntent(BaseModel):
    resource_hint: str
    filters: list[FilterCondition] = Field(default_factory=list)
    metric: Literal["count"] = "count"
    expected_value: int | None = None
    compare_api_with_database: bool = True
    ambiguities: list[str] = Field(default_factory=list)


class BusinessRule(BaseModel):
    text: str
    applicability: str = ""
    conditions: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)


class BusinessRuleBundle(BaseModel):
    rules: list[BusinessRule] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    def all_sources(self) -> list[SourceReference]:
        return [source for rule in self.rules for source in rule.sources]


class JoinType(StrEnum):
    INNER = "INNER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    FULL = "FULL"


class ColumnRef(BaseModel):
    table_alias: str
    column: str

    def render(self) -> str:
        return f"{self.table_alias}.{self.column}"


class JoinSpec(BaseModel):
    table: str
    alias: str
    join_type: JoinType = JoinType.INNER
    on: list[tuple[ColumnRef, ColumnRef]]


class PredicateOperator(StrEnum):
    EQ = "="
    NE = "<>"
    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="
    IS_NULL = "IS NULL"
    IS_NOT_NULL = "IS NOT NULL"
    IN = "IN"
    LIKE = "LIKE"


class Predicate(BaseModel):
    column: ColumnRef
    operator: PredicateOperator
    values: list[str | int] = Field(default_factory=list)
    or_group: int = 0
    reason: str = ""


class AggregateFunction(StrEnum):
    COUNT = "COUNT"
    SUM = "SUM"
    AVG = "AVG"
    MIN = "MIN"
    MAX = "MAX"


class Aggregate(BaseModel):
    function: AggregateFunction = AggregateFunction.COUNT
    column: ColumnRef | None = None
    distinct: bool = False


class SelectPlan(BaseModel):
    base_table: str
    base_alias: str = "t0"
    joins: list[JoinSpec] = Field(default_factory=list)
    predicates: list[Predicate] = Field(default_factory=list)
    aggregate: Aggregate = Field(default_factory=Aggregate)
    rationale: str = ""


class ScopeFilter(BaseModel):
    column: str
    operator: PredicateOperator
    values: list[str | int] = Field(default_factory=list)
    or_group: int = 0
    rule_text: str = ""
    verified_by: str = ""


class EntityMapping(BaseModel):
    operation_id: str
    base_table: str
    primary_key: list[str] = Field(default_factory=list)
    parameter_columns: dict[str, str] = Field(default_factory=dict)
    joins: list[JoinSpec] = Field(default_factory=list)
    scope_filters: list[ScopeFilter] = Field(default_factory=list)
    counting_unit: str = ""
    unaddressed_rules: list[str] = Field(default_factory=list)
    source: str = "agent"
    native_sql: str = ""
    code_references: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    confirmed: bool = False
    candidates: list[str] = Field(default_factory=list)


class HttpRequestSpec(BaseModel):
    method: Literal["GET"] = "GET"
    base_url: str
    path_template: str
    path_params: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str | list[str]] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    auth_type: AuthType = AuthType.NONE
    auth_username: str = ""
    auth_secret: SecretReference | None = None
    api_key_name: str = ""
    header_names: list[str] = Field(default_factory=list)
    headers_secret: SecretReference | None = None
    timeout_seconds: float = 30.0
    verify_tls: bool = True


class ApiObservation(BaseModel):
    operation_id: str
    curl: str
    status_code: int | None = None
    pages_fetched: int = 0
    reported_total: int | None = None
    fetched_count: int = 0
    unique_id_count: int | None = None
    duplicate_ids: list[str] = Field(default_factory=list)
    missing_id_count: int = 0
    complete: bool = False
    incomplete_reason: str = ""
    schema_errors: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0
    error_code: ErrorCode | None = None
    error_message: str = ""
    sample_ids: list[str] = Field(default_factory=list)
    response_profile: ResponseProfile | None = None

    @property
    def measured_count(self) -> int | None:
        if self.error_code or not self.complete:
            return None
        return self.unique_id_count if self.unique_id_count is not None else self.fetched_count


class DatabaseObservation(BaseModel):
    sql: str
    binds: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    column_types: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    error_code: ErrorCode | None = None
    error_message: str = ""

    @property
    def scalar(self) -> Decimal | None:
        if self.error_code or self.truncated or self.row_count != 1 or not self.rows or len(self.rows[0]) != 1:
            return None
        value = self.rows[0][0]
        if value is None:
            return None
        return Decimal(str(value))


class AssertionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class AssertionResult(BaseModel):
    name: str
    title: str
    status: AssertionStatus
    left_label: str
    left_value: str | None
    right_label: str
    right_value: str | None
    message: str


class ToolEvent(BaseModel):
    at: datetime = Field(default_factory=utc_now)
    agent: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    ok: bool = True
    idempotency_key: str = ""


class EvidenceReport(BaseModel):
    run_id: str
    project_id: str
    project_name: str
    environment: str
    user_request: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    model: str = ""
    prompt_version: str = ""
    openapi_hash: str = ""
    document_snapshot: list[str] = Field(default_factory=list)
    intent: VerificationIntent | None = None
    rules: BusinessRuleBundle | None = None
    mapping: EntityMapping | None = None
    api: ApiObservation | None = None
    database: DatabaseObservation | None = None
    implementation: DatabaseObservation | None = None
    implementation_source: dict[str, Any] = Field(default_factory=dict)
    code_chain: list[str] = Field(default_factory=list)
    assertions: list[AssertionResult] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)
    summary: str = ""
    hypotheses: list[str] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(default_factory=list)
