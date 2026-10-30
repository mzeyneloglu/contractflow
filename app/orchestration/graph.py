import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.gate import GateFailure
from app.agents.prompts import prompt_version
from app.agents.roles.analyst import run_analyst
from app.agents.roles.api_agent import request_spec, run_api_agent, validate_request
from app.agents.roles.business_rules import run_business_rules
from app.agents.roles.oracle_agent import build_mapping, plan_from_mapping, run_oracle_agent
from app.code.analyzer import CodeEvidence, analyze_operation
from app.code.repository import load_index
from app.code.runtime_capture import CapturedStatement, correlated_log_statements, rank_statements, trace_headers, trace_identifiers
from app.code.translation import CandidateRejected, count_wrapper, evidence_rules, runtime_candidate
from app.agents.grounding import catalog_suggestions, catalog_vocabulary, ground_options, schema_vocabulary
from app.agents.roles.planner import CAPABILITY_MESSAGE, run_planner
from app.agents.runtime import RunCancelled
from app.adapters.spring_boot.adapter import SpringBootAdapter
from app.connectors.http.executor import HttpExecutor
from app.core.comparison.engine import compare_counts, interpret
from app.core.models.common import ContractFlowError, ErrorCode, RunStatus, utc_now
from app.core.models.service import ResponseProfile
from app.core.models.verification import (
    ApiObservation,
    AssertionResult,
    BusinessRuleBundle,
    DatabaseObservation,
    EntityMapping,
    EvidenceReport,
    HttpRequestSpec,
    ToolEvent,
    VerificationIntent,
)
from app.model_providers.ollama import check_ollama
from app.orchestration.context import RunContext
from app.rag.knowledge import KnowledgeSnapshot

MAX_AGENT_RETRIES = 2
OTEL_WAIT_ATTEMPTS = 3
SOURCE_LABELS = {"otel": "OpenTelemetry", "log": "SQL logu", "vsql": "V$SQL", "code": "kaynak kod"}


def independent_rules(bundle: BusinessRuleBundle) -> BusinessRuleBundle:
    return BusinessRuleBundle(
        rules=[rule for rule in bundle.rules if not any(source.document_id == "code" for source in rule.sources)],
        unresolved=bundle.unresolved,
        conflicts=bundle.conflicts,
    )
WAITING_STATUSES = {status.value for status in RunStatus if status.is_waiting}
FINAL_STATUSES = {status.value for status in RunStatus if status.is_terminal}
RETRYABLE_HTTP = {ErrorCode.CONNECTION_REFUSED, ErrorCode.TIMEOUT, ErrorCode.DNS_FAILURE, ErrorCode.TLS_FAILURE}
SEQUENCE = ["preflight", "planner", "rules", "api", "code", "oracle", "gate", "execute", "compare", "analyst", "report"]


class RunState(TypedDict, total=False):
    run_id: str
    project_id: str
    user_request: str
    status: str
    error: str
    waiting: dict[str, Any] | None
    route_to: str
    clarifications: list[str]
    continue_without_documents: bool
    snapshot: dict[str, Any]
    intent: dict[str, Any]
    rules: dict[str, Any]
    api_choice: dict[str, Any]
    api_feedback: str
    api_attempts: int
    request_spec: dict[str, Any]
    response_profile: dict[str, Any]
    operation_id: str
    oracle_feedback: str
    oracle_attempts: int
    mapping: dict[str, Any]
    code_evidence: dict[str, Any]
    code_sql: dict[str, Any]
    implementation: dict[str, Any]
    implementation_observation: dict[str, Any] | None
    select_plan: dict[str, Any]
    sql: str
    binds: dict[str, Any]
    api_observation: dict[str, Any]
    db_observation: dict[str, Any]
    assertions: list[dict[str, Any]]
    coverage: list[str]
    interpretation: str
    final_status: str
    analysis: dict[str, Any]
    model_calls: int
    started_at: str


def waiting(status: RunStatus, kind: str, node: str, message: str, question: str = "", options: list[str] | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": status.value,
        "waiting": {"kind": kind, "node": node, "status": status.value, "message": message, "question": question, "options": options or [], "details": details or {}},
    }


def failed(message: str) -> dict[str, Any]:
    return {"status": RunStatus.ERROR.value, "error": message}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:20]


def api_values_of(state: RunState) -> dict[str, str]:
    choice = state.get("api_choice") or {}
    profile = ResponseProfile.model_validate(state["response_profile"])
    reserved = {profile.page_param, profile.size_param, "pageable", "sort"}
    values = {**choice.get("path_params", {}), **choice.get("query_params", {})}
    return {name: value for name, value in values.items() if name not in reserved}


class VerificationGraph:
    def __init__(self, context: RunContext):
        self.context = context

    async def _guarded(self, name: str, state: RunState, step) -> dict[str, Any]:
        self.context.budget.used = state.get("model_calls", 0)
        await self.context.emit({"type": "node_started", "node": name})
        try:
            if self.context.cancelled():
                raise RunCancelled()
            update = await step(state)
        except RunCancelled:
            update = {"status": RunStatus.CANCELLED.value, "error": "Kullanıcı çalışmayı iptal etti"}
        except ContractFlowError as error:
            if error.code is ErrorCode.MODEL_UNAVAILABLE:
                update = waiting(RunStatus.WAITING_FOR_OLLAMA, "ollama", name, error.message)
            else:
                update = failed(f"{error.code.value}: {error.message}")
        update.setdefault("status", RunStatus.RUNNING.value)
        update.setdefault("route_to", "")
        update["model_calls"] = self.context.budget.used
        await self.context.emit({"type": "node_finished", "node": name, "status": update["status"]})
        return update

    def node(self, name: str, step):
        async def run(state: RunState) -> dict[str, Any]:
            return await self._guarded(name, state, step)

        return run

    async def _agent_question(self, node: str, message: str, clarification, vocabulary: list[str]) -> dict[str, Any]:
        options = ground_options(clarification.options, vocabulary)
        hidden = [option for option in clarification.options if option not in options]
        if hidden:
            await self.context.emit({"type": "options_rejected", "agent": node, "message": f"Kaynakta karşılığı olmayan seçenekler gösterilmedi: {', '.join(hidden)}"})
        return waiting(RunStatus.NEEDS_CLARIFICATION, "clarification", node, message, clarification.question, options)

    async def preflight(self, state: RunState) -> dict[str, Any]:
        context = self.context
        missing = check_ollama(context.project.model)
        if missing:
            return waiting(RunStatus.WAITING_FOR_OLLAMA, "ollama", "preflight", "Gerekli Ollama modelleri kurulu değil", details={"commands": missing})
        if context.catalog is None:
            return failed("Projeye OpenAPI/Swagger sözleşmesi yüklenmemiş")
        snapshot = context.container.knowledge.snapshot(context.project.id)
        if snapshot.is_empty and not (state.get("continue_without_documents") or context.project.documents_optional):
            return waiting(RunStatus.WAITING_FOR_DOCUMENTS, "documents", "preflight", "Hazır durumda iş kuralı belgesi yok", options=["Tekrar kontrol et", "Belgesiz devam et"])
        if context.oracle is None:
            return failed("Oracle MCP süreci başlatılamadı")
        ping = await context.oracle_call("preflight", "ping")
        if not ping.get("ok"):
            return failed(f"Oracle bağlantısı kurulamadı: {ping.get('error_code')} {ping.get('message', '')}")
        return {
            "snapshot": {
                **snapshot.model_dump(mode="json"),
                "openapi_hash": context.catalog.content_hash,
                "model": context.project.model.model,
                "embedding_model": context.project.model.embedding_model,
                "prompt_version": prompt_version(),
                "oracle_version": ping.get("server_version", ""),
            }
        }

    async def planner(self, state: RunState) -> dict[str, Any]:
        decision = await run_planner(self.context, state["user_request"], state.get("clarifications", []))
        if decision.off_topic:
            await self.context.emit({"type": "off_topic", "agent": "planner", "message": "Talep bir adet doğrulaması değil; araç çağrılmadı"})
            return {"status": RunStatus.INCONCLUSIVE.value, "interpretation": CAPABILITY_MESSAGE, "coverage": ["Talep bir doğrulama talebi olarak sınıflandırılmadı; API ve Oracle sorgulanmadı"]}
        if decision.intent is None:
            return waiting(RunStatus.NEEDS_CLARIFICATION, "clarification", "planner", "Talepte doğrulama için gereken bilgi eksik", decision.question, decision.options)
        return {"intent": decision.intent.model_dump(mode="json")}

    async def rules(self, state: RunState) -> dict[str, Any]:
        self.context.snapshot = KnowledgeSnapshot.model_validate(state["snapshot"])
        if self.context.snapshot.is_empty:
            return {"rules": BusinessRuleBundle(unresolved=["İş kuralı belgesi olmadan devam edildi"]).model_dump(mode="json")}
        intent = VerificationIntent.model_validate(state["intent"])
        bundle, clarification = await run_business_rules(self.context, intent, state.get("clarifications", []))
        if clarification:
            return await self._agent_question("rules", "İş kuralı ajanı açıklama istiyor", clarification, catalog_vocabulary(self.context.catalog))
        return {"rules": bundle.model_dump(mode="json")}

    async def api(self, state: RunState) -> dict[str, Any]:
        intent = VerificationIntent.model_validate(state["intent"])
        rules = BusinessRuleBundle.model_validate(state["rules"])
        output, clarification = await run_api_agent(self.context, intent, [rule.text for rule in rules.rules], state.get("clarifications", []), state.get("api_feedback", ""))
        if clarification:
            return await self._agent_question("api", "API ajanı açıklama istiyor", clarification, catalog_vocabulary(self.context.catalog))
        try:
            operation, profile = validate_request(output, self.context.catalog, intent, SpringBootAdapter(), self.context.project.response_overrides, self.context.project.api.header_names)
        except GateFailure as failure:
            attempts = state.get("api_attempts", 0) + 1
            await self.context.emit({"type": "gate_rejected", "agent": "api", "message": str(failure)})
            if attempts < MAX_AGENT_RETRIES:
                return {"api_feedback": str(failure), "api_attempts": attempts, "route_to": "api"}
            return waiting(RunStatus.NEEDS_CLARIFICATION, "clarification", "api", "API isteği doğrulanamadı", f"{failure}. Hangi endpoint/parametre kullanılmalı?", catalog_suggestions(self.context.catalog, intent.resource_hint))
        spec = request_spec(self.context, operation, output, profile)
        spec.headers = {**spec.headers, **trace_headers(self.context.run_id)}
        return {"api_choice": output.model_dump(mode="json"), "operation_id": operation.operation_id, "request_spec": spec.model_dump(mode="json"), "response_profile": profile.model_dump(mode="json"), "api_feedback": "", "route_to": ""}

    async def code(self, state: RunState) -> dict[str, Any]:
        context = self.context
        if not context.project.code_paths:
            return {}
        operation = context.catalog.find(state["operation_id"])
        await context.emit({"type": "tool_call", "agent": "code", "tool": "code.trace_endpoint", "arguments": {"method": operation.method, "path": operation.path}, "reason": "Endpoint'in handler → servis → veri erişimi zincirini kaynak koddan çıkar"})
        index = await asyncio.to_thread(load_index, context.project.code_paths)
        evidence = await asyncio.to_thread(analyze_operation, index, operation.path, operation.method)
        summary = f"handler: {evidence.route.label() if evidence.route else 'bulunamadı'}; {len(evidence.chain)} metot, {len(evidence.queries)} sorgu, {len(evidence.derived_queries)} türetilmiş sorgu, {len(evidence.entities)} entity"
        await context.emit({"type": "tool_result", "agent": "code", "tool": "code.trace_endpoint", "ok": evidence.route is not None, "summary": summary})
        for warning in evidence.warnings:
            await context.emit({"type": "code_warning", "agent": "code", "message": warning})
        rules = BusinessRuleBundle.model_validate(state["rules"])
        rules.rules.extend(evidence_rules(evidence))
        if evidence.procedures:
            rules.unresolved.append("Servis stored procedure kullanıyor: " + ", ".join(location.label() for location in evidence.procedures))
        return {"code_evidence": evidence.model_dump(mode="json"), "rules": rules.model_dump(mode="json"), "code_sql": await self._code_sql(evidence, api_values_of(state))}

    async def _code_sql(self, evidence: CodeEvidence, api_values: dict[str, str]) -> dict[str, Any]:
        executable = [query for query in evidence.queries if query.native]
        if not executable:
            return {}
        listed = await self.context.oracle_call("code", "list_tables")
        schemas_by_table: dict[str, set[str]] = {}
        for table in listed.get("tables", []) if listed.get("ok") else []:
            schema, _, name = table["table"].partition(".")
            schemas_by_table.setdefault(name, set()).add(schema)
        known = {name: next(iter(schemas)) for name, schemas in schemas_by_table.items() if len(schemas) == 1}
        for query in executable:
            try:
                candidate = count_wrapper(query, api_values, known)
            except CandidateRejected as rejection:
                await self.context.emit({"type": "code_warning", "agent": "code", "message": f"{query.location.label()}: {rejection}"})
                continue
            validation = await self.context.oracle_call("code", "validate_select", {"sql": candidate.sql, "binds": candidate.binds})
            if validation.get("ok"):
                return {"sql": candidate.sql, "binds": candidate.binds, "notes": candidate.notes, "tables": validation.get("tables", []), "reference": query.location.label(), "kind": query.kind}
            await self.context.emit({"type": "code_warning", "agent": "code", "message": f"{query.location.label()}: {validation.get('message', '')}"})
        return {}

    async def oracle(self, state: RunState) -> dict[str, Any]:
        context = self.context
        api_values = api_values_of(state)
        remembered = context.container.mappings.get(context.project.id, state["operation_id"], context.catalog.content_hash)
        evidence = CodeEvidence.model_validate(state["code_evidence"]) if state.get("code_evidence") else None
        preferred = evidence.tables() if evidence else []
        if evidence:
            await context.emit({"type": "independence", "agent": "oracle", "message": "İş kuralı sorgusu koddan bağımsız kurulur: koddan yalnızca temel tablo adayları alınır, servis SQL'i ve kod kuralları gösterilmez"})
        try:
            if remembered and remembered.confirmed and remembered.source != "code" and not state.get("oracle_feedback"):
                await context.emit({"type": "memory", "agent": "oracle", "message": f"Doğrulanmış eşleştirme hafızadan kullanıldı: {remembered.base_table}"})
                plan, mapping = await plan_from_mapping(context, remembered, api_values), remembered
            else:
                intent = VerificationIntent.model_validate(state["intent"])
                rules = independent_rules(BusinessRuleBundle.model_validate(state["rules"]))
                output, clarification = await run_oracle_agent(context, intent, rules, state["operation_id"], api_values, state.get("clarifications", []), state.get("oracle_feedback", ""), preferred)
                if clarification:
                    return await self._agent_question("oracle", "Oracle ajanı açıklama istiyor", clarification, schema_vocabulary(context.table_cache))
                plan, mapping = await build_mapping(context, output, state["operation_id"], api_values, rules, strict=state.get("oracle_attempts", 0) == 0)
            compiled = await context.oracle_call("oracle", "build_select", {"plan": plan.model_dump(mode="json")})
            if not compiled.get("ok"):
                raise GateFailure(f"SQL derlenemedi: {compiled.get('error_code')} {compiled.get('message', '')}")
        except GateFailure as failure:
            attempts = state.get("oracle_attempts", 0) + 1
            await context.emit({"type": "gate_rejected", "agent": "oracle", "message": str(failure)})
            if attempts < MAX_AGENT_RETRIES:
                return {"oracle_feedback": str(failure), "oracle_attempts": attempts, "route_to": "oracle"}
            return waiting(RunStatus.NEEDS_CLARIFICATION, "clarification", "oracle", "Veritabanı eşleştirmesi doğrulanamadı", f"{failure}. Hangi tablo/kolon kullanılmalı?")
        return {"select_plan": plan.model_dump(mode="json"), "mapping": mapping.model_dump(mode="json"), "sql": compiled["sql"], "binds": compiled["binds"], "oracle_feedback": "", "route_to": ""}

    async def gate(self, state: RunState) -> dict[str, Any]:
        mapping = EntityMapping.model_validate(state["mapping"])
        if mapping.confirmed:
            return {}
        details = {
            "mapping": mapping.model_dump(mode="json"),
            "sql": state["sql"],
            "binds": state["binds"],
            "implementation": state.get("code_sql") or {},
            "request": HttpRequestSpec.model_validate(state["request_spec"]).model_dump(mode="json", exclude={"auth_secret"}),
        }
        return waiting(
            RunStatus.NEEDS_CLARIFICATION,
            "mapping",
            "gate",
            "Endpoint–tablo eşleştirmesi ilk kez kullanılıyor; karşılaştırmadan önce onayınız gerekiyor",
            "Eşleştirme ve SQL doğru mu?",
            ["Onayla ve kaydet", "Reddet"],
            details,
        )

    async def execute(self, state: RunState) -> dict[str, Any]:
        window_start = await self._database_time()
        api_observation, db_observation = await asyncio.gather(self._fetch_api(state), self._query_database(state))
        update = {"api_observation": api_observation.model_dump(mode="json"), "db_observation": db_observation.model_dump(mode="json")}
        if api_observation.error_code is None:
            implementation, observation = await self._implementation(state, window_start)
            update |= {"implementation": implementation, "implementation_observation": observation.model_dump(mode="json") if observation else None}
        if api_observation.error_code in RETRYABLE_HTTP:
            return {**update, **waiting(RunStatus.WAITING_FOR_SERVICE, "service", "execute", api_observation.error_message, options=["Tekrar kontrol et"])}
        if api_observation.error_code is ErrorCode.AUTH_REQUIRED:
            return {**update, **waiting(RunStatus.AUTH_REQUIRED, "auth", "execute", api_observation.error_message + ". Proje ayarlarından kimlik bilgisini güncelleyip tekrar deneyin", options=["Tekrar dene"])}
        return update

    async def _database_time(self) -> str:
        if not self.context.project.oracle.capture_runtime_sql:
            return ""
        result = await self.context.oracle_call("execute", "database_time")
        return result.get("time", "") if result.get("ok") else ""

    async def _captured_statements(self, state: RunState, window_start: str) -> list[CapturedStatement]:
        context = self.context
        trace_id, _ = trace_identifiers(context.run_id)
        statements: list[CapturedStatement] = []
        for attempt in range(OTEL_WAIT_ATTEMPTS):
            spans = context.container.otel.statements(trace_id)
            if spans:
                statements.extend(CapturedStatement("otel", sql, f"OpenTelemetry trace {trace_id[:12]}…") for sql in spans)
                break
            if attempt + 1 < OTEL_WAIT_ATTEMPTS and context.container.otel.has_any():
                await asyncio.sleep(1)
        if context.project.sql_log_path:
            statements.extend(CapturedStatement("log", sql, f"{Path(context.project.sql_log_path).name} (trace {trace_id[:12]}…)") for sql in correlated_log_statements(context.project.sql_log_path, trace_id))
        if window_start:
            captured = await context.oracle_call("execute", "recent_service_sql", {"since": window_start})
            for item in captured.get("statements", []) if captured.get("ok") else []:
                statements.append(CapturedStatement("vsql", item["sql"], f"V$SQL {item['sql_id']} ({item['schema']}, {item['executions']} çalışma)", item.get("binds", []), item.get("executions", 0)))
        return statements

    async def _implementation(self, state: RunState, window_start: str) -> tuple[dict[str, Any], DatabaseObservation | None]:
        context = self.context
        listed = await context.oracle_call("execute", "list_tables")
        tables = [item["table"] for item in listed.get("tables", [])] if listed.get("ok") else []
        schemas_by_table: dict[str, set[str]] = {}
        for table in tables:
            schema, _, name = table.partition(".")
            schemas_by_table.setdefault(name, set()).add(schema)
        known = {name: next(iter(schemas)) for name, schemas in schemas_by_table.items() if len(schemas) == 1}
        api_values = list(api_values_of(state).values())
        rejected: list[str] = []
        for statement in rank_statements(await self._captured_statements(state, window_start), set(known)):
            try:
                sql, binds, notes = runtime_candidate(statement.sql, api_values, known, statement.binds)
            except CandidateRejected as rejection:
                rejected.append(f"{statement.reference}: {rejection}")
                continue
            observation = await self._run_select(sql, binds, f"impl:{statement.source}")
            if observation.error_code is None:
                await context.emit({"type": "runtime_sql", "agent": "execute", "message": f"Servisin çalıştırdığı sorgu yakalandı ({SOURCE_LABELS[statement.source]}): {statement.reference}"})
                return {"source": statement.source, "reference": statement.reference, "raw_sql": statement.sql[:4000], "sql": sql, "binds": binds, "notes": notes}, observation
            rejected.append(f"{statement.reference}: {observation.error_code} {observation.error_message}")
        code_sql = state.get("code_sql") or {}
        if code_sql:
            observation = await self._run_select(code_sql["sql"], code_sql["binds"], "impl:code")
            return {"source": "code", "reference": code_sql["reference"], "raw_sql": "", "sql": code_sql["sql"], "binds": code_sql["binds"], "notes": code_sql.get("notes", []) + rejected}, observation
        for message in rejected[:5]:
            await context.emit({"type": "code_warning", "agent": "execute", "message": message})
        return {"source": "", "reference": "", "notes": rejected or ["Servisin çalıştırdığı sorgu yakalanamadı: kod klasörü, V$SQL yakalama, SQL logu veya OpenTelemetry yapılandırılmamış"]}, None

    async def _run_select(self, sql: str, binds: dict[str, Any], cache_prefix: str) -> DatabaseObservation:
        key = f"{cache_prefix}:{digest([sql, binds])}"
        cached = self.context.container.operation_results.get(self.context.run_id, key)
        if cached:
            return DatabaseObservation.model_validate(cached)
        result = await self.context.oracle_call("execute", "execute_select", {"sql": sql, "binds": binds})
        if not result.get("ok"):
            return DatabaseObservation(sql=sql, binds=binds, error_code=ErrorCode(result.get("error_code", "DATABASE_ERROR")), error_message=result.get("message", ""))
        observation = DatabaseObservation(sql=sql, binds=binds, columns=result["columns"], column_types=result["column_types"], rows=result["rows"], row_count=result["row_count"], truncated=result["truncated"], elapsed_ms=result["elapsed_ms"])
        self.context.container.operation_results.put(self.context.run_id, key, observation.model_dump(mode="json"))
        return observation

    async def _fetch_api(self, state: RunState) -> ApiObservation:
        context = self.context
        spec = HttpRequestSpec.model_validate(state["request_spec"])
        profile = ResponseProfile.model_validate(state["response_profile"])
        key = f"api:{digest([state['request_spec'], state['response_profile']])}"
        cached = context.container.operation_results.get(context.run_id, key)
        if cached:
            await context.emit({"type": "replay", "agent": "api", "message": "Tamamlanmış API gözlemi checkpoint'ten kullanıldı"})
            return ApiObservation.model_validate(cached)
        operation = context.catalog.find(state["operation_id"])
        await context.emit({"type": "tool_call", "agent": "api", "tool": "http.fetch_all_pages", "arguments": {"operation_id": operation.operation_id, "path": operation.path, "query": spec.query_params, "path_params": spec.path_params}})
        executor = HttpExecutor(context.container.secrets.get)
        observation = await executor.fetch_all(spec, operation, profile, context.catalog.source_version)
        await context.emit({"type": "tool_result", "agent": "api", "tool": "http.fetch_all_pages", "ok": observation.error_code is None, "summary": observation.error_message or f"HTTP {observation.status_code}, {observation.pages_fetched} sayfa, {observation.unique_id_count} benzersiz ID"})
        if observation.error_code is None:
            context.container.operation_results.put(context.run_id, key, observation.model_dump(mode="json"))
        return observation

    async def _query_database(self, state: RunState) -> DatabaseObservation:
        context = self.context
        key = f"db:{digest([state['sql'], state['binds']])}"
        cached = context.container.operation_results.get(context.run_id, key)
        if cached:
            await context.emit({"type": "replay", "agent": "oracle", "message": "Tamamlanmış Oracle gözlemi checkpoint'ten kullanıldı"})
            return DatabaseObservation.model_validate(cached)
        result = await context.oracle_call("oracle", "execute_select", {"sql": state["sql"], "binds": state["binds"]})
        if not result.get("ok"):
            return DatabaseObservation(sql=state["sql"], binds=state["binds"], error_code=ErrorCode(result.get("error_code", "DATABASE_ERROR")), error_message=result.get("message", ""))
        observation = DatabaseObservation(
            sql=state["sql"],
            binds=state["binds"],
            columns=result["columns"],
            column_types=result["column_types"],
            rows=result["rows"],
            row_count=result["row_count"],
            truncated=result["truncated"],
            elapsed_ms=result["elapsed_ms"],
        )
        context.container.operation_results.put(context.run_id, key, observation.model_dump(mode="json"))
        return observation

    async def compare(self, state: RunState) -> dict[str, Any]:
        intent = VerificationIntent.model_validate(state["intent"])
        implementation = DatabaseObservation.model_validate(state["implementation_observation"]) if state.get("implementation_observation") else None
        outcome = compare_counts(intent.expected_value, ApiObservation.model_validate(state["api_observation"]), DatabaseObservation.model_validate(state["db_observation"]), implementation)
        rules = BusinessRuleBundle.model_validate(state["rules"])
        coverage = list(outcome.coverage_notes)
        if rules.unresolved:
            coverage.append("Çözülemeyen iş kuralı konuları: " + "; ".join(rules.unresolved[:5]))
        if rules.conflicts:
            coverage.append("Çelişkili kurallar: " + "; ".join(rules.conflicts[:5]))
        mapping = EntityMapping.model_validate(state["mapping"])
        if mapping.unaddressed_rules:
            coverage.append("SQL'e yansıtılmayan kurallar (kullanıcı onayıyla): " + "; ".join(mapping.unaddressed_rules[:5]))
        status = outcome.status
        if rules.conflicts and status in {RunStatus.PASS, RunStatus.FAIL}:
            status = RunStatus.INCONCLUSIVE
            coverage.append("Kaynak önceliği tanımsız çelişki nedeniyle hüküm INCONCLUSIVE")
        await self.context.emit({"type": "comparison", "status": status.value, "interpretation": interpret(outcome)})
        return {"assertions": [assertion.model_dump(mode="json") for assertion in outcome.assertions], "coverage": coverage, "interpretation": interpret(outcome), "status": RunStatus.RUNNING.value, "final_status": status.value}

    async def analyst(self, state: RunState) -> dict[str, Any]:
        assertions = [AssertionResult.model_validate(item) for item in state["assertions"]]
        rules = BusinessRuleBundle.model_validate(state["rules"])
        try:
            analysis = await run_analyst(self.context, state["final_status"], state["interpretation"], assertions, state.get("coverage", []), rules)
            return {"analysis": analysis.model_dump(mode="json")}
        except ContractFlowError as error:
            return {"analysis": {"summary": state["interpretation"], "hypotheses": [], "open_questions": [f"Analiz ajanı çalışamadı: {error.message}"]}}

    async def report(self, state: RunState) -> dict[str, Any]:
        status = state.get("final_status") if state.get("status") == RunStatus.RUNNING.value else state.get("status")
        return {"status": status or RunStatus.ERROR.value}

    async def wait(self, state: RunState) -> dict[str, Any]:
        pending = state["waiting"]
        answer = interrupt(pending) or {}
        action = answer.get("action", "retry")
        text = str(answer.get("text", "")).strip()
        update: dict[str, Any] = {"status": RunStatus.RUNNING.value, "waiting": None, "route_to": pending["node"]}
        if action == "cancel":
            return {**update, "status": RunStatus.CANCELLED.value, "error": "Kullanıcı çalışmayı iptal etti", "route_to": "report"}
        if pending["kind"] == "documents" and action == "continue_without_documents":
            return {**update, "continue_without_documents": True}
        if pending["kind"] == "clarification" and text:
            return {**update, "clarifications": state.get("clarifications", []) + [f"Soru: {pending['question']} Yanıt: {text}"], "api_attempts": 0, "oracle_attempts": 0}
        if pending["kind"] == "mapping":
            if action == "approve":
                mapping = EntityMapping.model_validate(state["mapping"])
                mapping.confirmed = True
                self.context.container.mappings.save(self.context.project.id, mapping, self.context.catalog.content_hash)
                await self.context.emit({"type": "memory", "agent": "gate", "message": "Eşleştirme onaylandı ve proje hafızasına kaydedildi"})
                return {**update, "mapping": mapping.model_dump(mode="json"), "route_to": "execute"}
            feedback = f"Kullanıcı eşleştirmeyi reddetti: {text or 'gerekçe verilmedi'}"
            return {**update, "oracle_feedback": feedback, "oracle_attempts": 0, "route_to": "oracle"}
        return update


def route_after(name: str):
    following = SEQUENCE[SEQUENCE.index(name) + 1] if name != "report" else END

    def route(state: RunState) -> str:
        status = state.get("status", RunStatus.RUNNING.value)
        if status in FINAL_STATUSES:
            return "report"
        if status in WAITING_STATUSES:
            return "wait"
        if state.get("route_to") == name:
            return name
        return following

    return route


def route_after_wait(state: RunState) -> str:
    return state.get("route_to") or "report"


def build_graph(context: RunContext) -> StateGraph:
    graph_steps = VerificationGraph(context)
    graph = StateGraph(RunState)
    for name in SEQUENCE[:-1]:
        graph.add_node(name, graph_steps.node(name, getattr(graph_steps, name)))
    graph.add_node("report", graph_steps.report)
    graph.add_node("wait", graph_steps.wait)
    graph.add_edge(START, "preflight")
    for name in SEQUENCE[:-1]:
        targets = {target: target for target in {name, SEQUENCE[SEQUENCE.index(name) + 1], "wait", "report"}}
        graph.add_conditional_edges(name, route_after(name), targets)
    graph.add_conditional_edges("wait", route_after_wait, {name: name for name in SEQUENCE})
    graph.add_edge("report", END)
    return graph


def build_report(state: dict[str, Any], context: RunContext, started_at, events: list[dict[str, Any]]) -> EvidenceReport:
    snapshot = state.get("snapshot") or {}
    analysis = state.get("analysis") or {}
    tool_events = [
        ToolEvent(agent=event.get("agent", ""), tool=event.get("tool", ""), arguments=event.get("arguments", {}), summary=event.get("summary", event.get("reason", "")), ok=event.get("ok", True), idempotency_key=event.get("idempotency_key", ""))
        for event in events
        if event.get("type") in {"tool_call", "tool_result"}
    ]
    return EvidenceReport(
        run_id=context.run_id,
        project_id=context.project.id,
        project_name=context.project.name,
        environment=context.project.environment,
        user_request=state.get("user_request", ""),
        status=RunStatus(state.get("status", RunStatus.ERROR.value)),
        started_at=started_at,
        finished_at=utc_now(),
        model=snapshot.get("model", context.project.model.model),
        prompt_version=snapshot.get("prompt_version", prompt_version()),
        openapi_hash=snapshot.get("openapi_hash", ""),
        document_snapshot=snapshot.get("labels", []),
        intent=VerificationIntent.model_validate(state["intent"]) if state.get("intent") else None,
        rules=BusinessRuleBundle.model_validate(state["rules"]) if state.get("rules") else None,
        mapping=EntityMapping.model_validate(state["mapping"]) if state.get("mapping") else None,
        api=ApiObservation.model_validate(state["api_observation"]) if state.get("api_observation") else None,
        database=DatabaseObservation.model_validate(state["db_observation"]) if state.get("db_observation") else None,
        implementation=DatabaseObservation.model_validate(state["implementation_observation"]) if state.get("implementation_observation") else None,
        implementation_source=state.get("implementation") or {},
        code_chain=[location["file"] + ":" + str(location["line"]) + " " + location.get("symbol", "") for location in (state.get("code_evidence") or {}).get("chain", [])],
        assertions=[AssertionResult.model_validate(item) for item in state.get("assertions", [])],
        coverage_notes=state.get("coverage", []) + ([state["error"]] if state.get("error") else []),
        summary=analysis.get("summary") or state.get("interpretation") or state.get("error", ""),
        hypotheses=analysis.get("hypotheses", []),
        tool_events=tool_events,
    )

