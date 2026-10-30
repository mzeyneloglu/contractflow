import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.dispatcher import AgentTool
from app.agents.gate import GateFailure
from app.core.models.verification import (
    Aggregate,
    BusinessRuleBundle,
    ColumnRef,
    EntityMapping,
    JoinSpec,
    JoinType,
    Predicate,
    PredicateOperator,
    ScopeFilter,
    SelectPlan,
    VerificationIntent,
)
from app.orchestration.context import RunContext

AGENT = "oracle"
BASE_ALIAS = "t0"
SEED_TABLES = 3
NUMERIC_TYPES = {"NUMBER", "INTEGER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE"}


class KeywordArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=2, max_length=100)


class TableArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str = Field(min_length=1, max_length=130)


class ParameterColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_parameter: str
    column: str


class ScopeFilterOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_alias: str = BASE_ALIAS
    column: str
    operator: Literal["=", "<>", "IN", "IS NULL", "IS NOT NULL"]
    values: list[str]
    rule_index: int
    or_group: int = 0


class RuleDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_index: int
    decision: Literal["filter", "not_applicable", "unclear"]
    note: str = Field(max_length=160)


class JoinOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    alias: str
    join_type: Literal["INNER", "LEFT"]
    base_column: str
    joined_column: str


class OracleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_table: str
    primary_key_column: str
    parameter_columns: list[ParameterColumn]
    scope_filters: list[ScopeFilterOutput]
    joins: list[JoinOutput]
    rule_decisions: list[RuleDecision]
    counting_unit: str = Field(max_length=160)


class SchemaTools:
    def __init__(self, context: RunContext):
        self.context = context

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.context.oracle.call(tool, arguments)
        if tool == "describe_table" and result.get("ok"):
            self.context.table_cache[result["table"]] = result
        return result

    async def search_schema(self, keyword: str) -> dict[str, Any]:
        return await self._call("search_schema", {"keyword": keyword})

    async def describe_table(self, table: str) -> dict[str, Any]:
        return await self._call("describe_table", {"table": table})

    async def get_relationships(self, table: str) -> dict[str, Any]:
        return await self._call("get_relationships", {"table": table})

    def agent_tools(self) -> list[AgentTool]:
        return [
            AgentTool("search_schema", "İzinli şemalarda tablo/kolon/açıklama araması", KeywordArgs, self.search_schema),
            AgentTool("describe_table", "Tablonun kolonları, tipleri ve PK bilgisi", TableArgs, self.describe_table),
            AgentTool("get_relationships", "Tablonun PK/FK ilişkileri", TableArgs, self.get_relationships),
        ]


def split_words(text: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [word for word in re.split(r"[^A-Za-z0-9ÇĞİÖŞÜçğıöşü]+", spaced) if len(word) >= 3]


async def seed_schema(context: RunContext, intent: VerificationIntent, api_parameters: list[str], preferred_tables: list[str] | None = None) -> list[dict[str, Any]]:
    keywords = list(dict.fromkeys(split_words(intent.resource_hint) + api_parameters))[:4]
    scores: dict[str, int] = {table: 1000 for table in preferred_tables or []}
    for keyword in keywords:
        result = await context.oracle_call(AGENT, "search_schema", {"keyword": keyword})
        for match in result.get("matches", []) if result.get("ok") else []:
            scores[match["table"]] = scores.get(match["table"], 0) + match["score"]
    top_tables = sorted(scores, key=lambda table: -scores[table])[:SEED_TABLES]
    described = []
    for table in top_tables:
        result = await context.oracle_call(AGENT, "describe_table", {"table": table})
        if result.get("ok"):
            described.append({key: result[key] for key in ("table", "comment", "primary_key", "columns")})
    return described


async def table_info(context: RunContext, table: str) -> dict[str, Any]:
    key = table.upper()
    if key not in context.table_cache:
        result = await context.oracle_call(AGENT, "describe_table", {"table": key})
        if not result.get("ok"):
            raise GateFailure(f"Tablo doğrulanamadı: {table} ({result.get('message', '')})")
    return context.table_cache[key]


def column_types(info: dict[str, Any]) -> dict[str, str]:
    return {column["name"].upper(): column["type"].upper() for column in info["columns"]}


def require_column(info: dict[str, Any], column: str) -> str:
    name = column.upper()
    if name not in column_types(info):
        raise GateFailure(f"Kolon şemada yok: {info['table']}.{name}")
    return name


def typed_value(value: str, data_type: str) -> str | int:
    if data_type in NUMERIC_TYPES and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return value


def check_rule_decisions(output: OracleOutput, rules: BusinessRuleBundle, strict: bool = True) -> list[str]:
    decisions = {decision.rule_index: decision for decision in output.rule_decisions}
    filtered_rules = {item.rule_index for item in output.scope_filters}
    unaddressed = [rules.rules[index].text for index in range(len(rules.rules)) if index not in decisions and index not in filtered_rules]
    unclear = [f"{index}: {decision.note}" for index, decision in decisions.items() if decision.decision == "unclear"]
    if unclear:
        raise GateFailure(f"Kolon eşlemesi belirsiz kurallar: {'; '.join(unclear)}")
    filtered = {item.rule_index for item in output.scope_filters}
    uncovered = [index for index, decision in decisions.items() if decision.decision == "filter" and index not in filtered and index < len(rules.rules)]
    if uncovered and not strict:
        return unaddressed + [rules.rules[index].text for index in uncovered]
    if uncovered:
        raise GateFailure(
            f"'filter' kararı verilen kurallar için scope_filters eksik: {uncovered}. "
            f"Verilen scope_filters rule_index değerleri: {sorted(filtered)}. Her 'filter' kuralı için en az bir scope_filters öğesi yaz "
            "veya kural sayımı etkilemiyorsa kararı 'not_applicable' yap."
        )
    return unaddressed


async def build_mapping(
    context: RunContext,
    output: OracleOutput,
    operation_id: str,
    api_values: dict[str, str],
    rules: BusinessRuleBundle,
    strict: bool = True,
) -> tuple[SelectPlan, EntityMapping]:
    base = await table_info(context, output.base_table)
    base_types = column_types(base)
    aliases = {BASE_ALIAS: base}
    joins: list[JoinSpec] = []
    for join in output.joins:
        joined = await table_info(context, join.table)
        if not re.fullmatch(r"t[1-9]", join.alias):
            raise GateFailure(f"JOIN alias t1..t9 biçiminde olmalı: {join.alias}")
        joins.append(
            JoinSpec(
                table=joined["table"],
                alias=join.alias,
                join_type=JoinType(join.join_type),
                on=[(ColumnRef(table_alias=join.alias, column=require_column(joined, join.joined_column)), ColumnRef(table_alias=BASE_ALIAS, column=require_column(base, join.base_column)))],
            )
        )
        aliases[join.alias] = joined
    unaddressed = check_rule_decisions(output, rules, strict)
    predicates: list[Predicate] = []
    mapped = {item.api_parameter: require_column(base, item.column) for item in output.parameter_columns}
    unmapped = sorted(set(api_values) - set(mapped))
    if unmapped:
        raise GateFailure(f"API filtreleri kolona eşlenmedi: {', '.join(unmapped)}")
    for parameter, column in mapped.items():
        if parameter not in api_values:
            raise GateFailure(f"API isteğinde olmayan parametre eşlendi: {parameter}")
        predicates.append(Predicate(column=ColumnRef(table_alias=BASE_ALIAS, column=column), operator=PredicateOperator.EQ, values=[typed_value(api_values[parameter], base_types[column])], reason=f"API parametresi {parameter}"))
    scope_filters: list[ScopeFilter] = []
    for item in output.scope_filters:
        if not 0 <= item.rule_index < len(rules.rules):
            raise GateFailure(f"Kapsam filtresi geçerli bir kurala bağlı değil: {item.column}")
        rule = rules.rules[item.rule_index]
        info = aliases.get(item.table_alias)
        if info is None:
            raise GateFailure(f"Bilinmeyen tablo alias'ı: {item.table_alias}")
        column = require_column(info, item.column)
        rule_text = " ".join([rule.text, *rule.conditions, *rule.exceptions, *(source.excerpt for source in rule.sources)])
        ungrounded = [value for value in item.values if value not in rule_text]
        if ungrounded:
            raise GateFailure(f"{column} filtresindeki değerler kural metninde geçmiyor: {', '.join(ungrounded)}")
        operator = PredicateOperator(item.operator)
        values = [] if operator in {PredicateOperator.IS_NULL, PredicateOperator.IS_NOT_NULL} else [typed_value(value, column_types(info)[column]) for value in item.values]
        predicates.append(Predicate(column=ColumnRef(table_alias=item.table_alias, column=column), operator=operator, values=values, or_group=item.or_group, reason=rule.text[:200]))
        scope_filters.append(ScopeFilter(column=f"{item.table_alias}.{column}", operator=operator, values=values, or_group=item.or_group, rule_text=rule.text[:300], verified_by=rule.sources[0].label()))
    primary_key = require_column(base, output.primary_key_column)
    plan = SelectPlan(
        base_table=base["table"],
        base_alias=BASE_ALIAS,
        joins=joins,
        predicates=predicates,
        aggregate=Aggregate(column=ColumnRef(table_alias=BASE_ALIAS, column=primary_key), distinct=True),
        rationale=output.counting_unit,
    )
    mapping = EntityMapping(
        operation_id=operation_id,
        base_table=base["table"],
        primary_key=[primary_key],
        parameter_columns=mapped,
        joins=joins,
        scope_filters=scope_filters,
        counting_unit=output.counting_unit,
        unaddressed_rules=unaddressed,
        confirmed=False,
    )
    return plan, mapping


async def run_oracle_agent(
    context: RunContext,
    intent: VerificationIntent,
    rules: BusinessRuleBundle,
    operation_id: str,
    api_values: dict[str, str],
    clarifications: list[str],
    feedback: str = "",
    preferred_tables: list[str] | None = None,
    code_tools: list[AgentTool] | None = None,
):
    seeded = await seed_schema(context, intent, list(api_values), preferred_tables)
    task = {
        "dogrulama_niyeti": intent.model_dump(mode="json"),
        "api_operasyonu": operation_id,
        "api_filtre_degerleri": api_values,
        "is_kurallari": [{"rule_index": index, "text": rule.text, "conditions": rule.conditions, "exceptions": rule.exceptions} for index, rule in enumerate(rules.rules)],
        "aday_tablolar": seeded,
        "kullanici_aciklamalari": clarifications,
        "onceki_deneme_hatasi": feedback,
    }
    result = await context.runtime().run(AGENT, task, SchemaTools(context).agent_tools() + (code_tools or []), OracleOutput)
    if result.clarification:
        return None, result.clarification
    return result.output, None


def split_column(qualified: str) -> ColumnRef:
    alias, _, column = qualified.partition(".")
    return ColumnRef(table_alias=alias or BASE_ALIAS, column=column or alias)


async def plan_from_mapping(context: RunContext, mapping: EntityMapping, api_values: dict[str, str]) -> SelectPlan:
    base = await table_info(context, mapping.base_table)
    for join in mapping.joins:
        await table_info(context, join.table)
    base_types = column_types(base)
    missing = sorted(set(api_values) - set(mapping.parameter_columns))
    if missing:
        raise GateFailure(f"Kayıtlı eşleştirmede olmayan API parametreleri: {', '.join(missing)}")
    predicates = [
        Predicate(
            column=ColumnRef(table_alias=BASE_ALIAS, column=require_column(base, column)),
            operator=PredicateOperator.EQ,
            values=[typed_value(api_values[parameter], base_types[column.upper()])],
            reason=f"API parametresi {parameter}",
        )
        for parameter, column in mapping.parameter_columns.items()
        if parameter in api_values
    ]
    predicates.extend(Predicate(column=split_column(item.column), operator=item.operator, values=item.values, or_group=item.or_group, reason=item.rule_text[:200]) for item in mapping.scope_filters)
    return SelectPlan(
        base_table=mapping.base_table,
        base_alias=BASE_ALIAS,
        joins=mapping.joins,
        predicates=predicates,
        aggregate=Aggregate(column=ColumnRef(table_alias=BASE_ALIAS, column=require_column(base, mapping.primary_key[0])), distinct=True),
        rationale=mapping.counting_unit,
    )
