import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.spring_boot.adapter import SpringBootAdapter
from app.agents.dispatcher import AgentTool
from app.agents.gate import GateFailure
from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.service import ParameterLocation, ResponseProfile, ServiceCatalog, ServiceOperation
from app.core.models.verification import HttpRequestSpec, VerificationIntent
from app.orchestration.context import RunContext
from app.rag.lexical import LexicalIndex

AGENT = "api"
CANDIDATE_LIMIT = 5


class OperationSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=200)


class OperationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str


class ApiOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str
    path_params: dict[str, str]
    query_params: dict[str, str]


def operation_view(operation: ServiceOperation, profile: ResponseProfile | None = None) -> dict[str, Any]:
    view = {
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path,
        "summary": operation.summary or operation.description[:160],
        "parameters": [
            {"name": parameter.name, "in": parameter.location.value, "required": parameter.required, "type": parameter.schema_type, "description": parameter.description[:120]}
            for parameter in operation.parameters
        ],
    }
    if profile is not None:
        view["response_profile"] = profile.model_dump(include={"items_path", "total_path", "id_path", "pagination", "page_param", "size_param"}, mode="json")
    return view


def rank_operations(catalog: ServiceCatalog, query: str, limit: int = CANDIDATE_LIMIT) -> list[ServiceOperation]:
    executable = [operation for operation in catalog.operations if operation.executable]
    if not executable:
        return []
    index = LexicalIndex([operation.operation_id for operation in executable], [operation.search_text() for operation in executable])
    ranked = [operation_id for operation_id, _ in index.search(query, limit)]
    by_id = {operation.operation_id: operation for operation in executable}
    ranked_operations = [by_id[operation_id] for operation_id in ranked]
    return ranked_operations or executable[:limit]


class CatalogTools:
    def __init__(self, catalog: ServiceCatalog, adapter: SpringBootAdapter):
        self.catalog = catalog
        self.adapter = adapter

    async def search_operations(self, query: str) -> dict[str, Any]:
        return {"ok": True, "operations": [operation_view(operation) for operation in rank_operations(self.catalog, query)]}

    async def describe_operation(self, operation_id: str) -> dict[str, Any]:
        operation = self.catalog.find(operation_id)
        if operation is None:
            return {"ok": False, "error_code": "NOT_FOUND", "message": "Operasyon katalogda yok"}
        return {"ok": True, **operation_view(operation, self.adapter.response_profile(operation))}

    def agent_tools(self) -> list[AgentTool]:
        return [
            AgentTool("search_operations", "OpenAPI kataloğunda okuma operasyonu arar", OperationSearchArgs, self.search_operations),
            AgentTool("describe_operation", "Operasyonun parametre ve yanıt yapısını getirir", OperationArgs, self.describe_operation),
        ]


def compact(text: str) -> str:
    return re.sub(r"[^0-9a-zçğıöşü]", "", text.casefold())


def deterministic_choice(catalog: ServiceCatalog, intent: VerificationIntent) -> ApiOutput | None:
    hint = compact(intent.resource_hint)
    if not hint or not intent.filters:
        return None
    filter_names = {condition.name.casefold(): condition for condition in intent.filters}
    matches = []
    for operation in catalog.operations:
        if not operation.executable or hint not in compact(f"{operation.path} {operation.operation_id}"):
            continue
        parameters = {parameter.name.casefold(): parameter for parameter in operation.parameters if parameter.location in {ParameterLocation.PATH, ParameterLocation.QUERY}}
        if set(filter_names) <= set(parameters):
            matches.append((operation, parameters))
    if len(matches) != 1:
        return None
    operation, parameters = matches[0]
    path_params = {parameters[name].name: condition.value for name, condition in filter_names.items() if parameters[name].location is ParameterLocation.PATH}
    query_params = {parameters[name].name: condition.value for name, condition in filter_names.items() if parameters[name].location is ParameterLocation.QUERY}
    return ApiOutput(operation_id=operation.operation_id, path_params=path_params, query_params=query_params)


def paging_names(profile: ResponseProfile) -> set[str]:
    return {profile.page_param, profile.size_param, "pageable", "sort"}


def apply_override(profile: ResponseProfile, override: dict[str, Any] | None) -> ResponseProfile:
    if not override:
        return profile
    return profile.model_copy(update={**override, "locked": True, "detected_by": "kullanıcı ayarı"})


def validate_request(
    output: ApiOutput,
    catalog: ServiceCatalog,
    intent: VerificationIntent,
    adapter: SpringBootAdapter,
    overrides: dict[str, dict[str, Any]] | None = None,
    fixed_header_names: list[str] | None = None,
) -> tuple[ServiceOperation, ResponseProfile]:
    fixed_header_names = fixed_header_names or []
    operation = catalog.find(output.operation_id)
    if operation is None:
        raise GateFailure(f"Katalogda olmayan operasyon seçildi: {output.operation_id}")
    if operation.method != "GET" or not operation.executable:
        raise GateFailure(f"Operasyon çalıştırılabilir bir GET değil: {operation.operation_id}")
    profile = apply_override(adapter.response_profile(operation), (overrides or {}).get(operation.operation_id))
    declared = {(parameter.name, parameter.location) for parameter in operation.parameters}
    allowed_values = {condition.value for condition in intent.filters}
    for location, values in ((ParameterLocation.PATH, output.path_params), (ParameterLocation.QUERY, output.query_params)):
        for name, value in values.items():
            if (name, location) not in declared:
                raise GateFailure(f"Operasyonda tanımlı olmayan {location.value} parametresi: {name}")
            if name in paging_names(profile):
                continue
            if value not in allowed_values:
                raise GateFailure(f"'{name}' için kullanıcı talebinde bulunmayan değer: {value}")
    supplied = set(output.path_params) | set(output.query_params) | paging_names(profile)
    missing = [parameter.name for parameter in operation.parameters if parameter.required and parameter.location in {ParameterLocation.PATH, ParameterLocation.QUERY} and parameter.name not in supplied]
    if missing:
        raise GateFailure(f"Zorunlu parametreler eksik: {', '.join(missing)}")
    configured_headers = {name.lower() for name in fixed_header_names}
    missing_headers = [parameter.name for parameter in operation.parameters if parameter.required and parameter.location is ParameterLocation.HEADER and parameter.name.lower() not in configured_headers]
    if missing_headers:
        raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"Operasyonun zorunlu header'ları proje ayarlarında tanımlı değil: {', '.join(missing_headers)}. Kurulum → API → Sabit header'lar alanına ekleyin")
    unmapped = [condition for condition in intent.filters if condition.value not in set(output.path_params.values()) | set(output.query_params.values())]
    if unmapped:
        raise GateFailure(f"Talepteki filtre API parametresine eşlenmedi: {', '.join(condition.name for condition in unmapped)}")
    return operation, profile


def request_spec(context: RunContext, operation: ServiceOperation, output: ApiOutput, profile: ResponseProfile) -> HttpRequestSpec:
    api = context.project.api
    query = {name: value for name, value in output.query_params.items() if name not in paging_names(profile)}
    return HttpRequestSpec(
        base_url=api.base_url,
        path_template=operation.path,
        path_params=output.path_params,
        query_params=query,
        auth_type=api.auth_type,
        auth_username=api.username,
        auth_secret=api.credential_ref,
        api_key_name=api.api_key_name,
        header_names=api.header_names,
        headers_secret=api.headers_ref,
        verify_tls=api.verify_tls,
    )


async def run_api_agent(context: RunContext, intent: VerificationIntent, rules_summary: list[str], clarifications: list[str], feedback: str = ""):
    direct = None if feedback else deterministic_choice(context.catalog, intent)
    if direct is not None:
        await context.emit({"type": "deterministic", "agent": AGENT, "message": f"Talepteki kaynak ve filtreler tek bir operasyonla birebir eşleşti: {direct.operation_id} (model çağrısı gerekmedi)"})
        return direct, None
    adapter = SpringBootAdapter()
    tools = CatalogTools(context.catalog, adapter)
    query = " ".join([intent.resource_hint] + [condition.name for condition in intent.filters])
    candidates = [operation_view(operation, adapter.response_profile(operation)) for operation in rank_operations(context.catalog, query)]
    task = {
        "dogrulama_niyeti": intent.model_dump(mode="json"),
        "aday_operasyonlar": candidates,
        "is_kurallari": rules_summary,
        "kullanici_aciklamalari": clarifications,
        "onceki_deneme_hatasi": feedback,
    }
    result = await context.runtime().run(AGENT, task, tools.agent_tools(), ApiOutput)
    if result.clarification:
        return None, result.clarification
    return result.output, None
