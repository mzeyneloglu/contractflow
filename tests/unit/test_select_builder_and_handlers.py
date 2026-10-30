from typing import Any

import pytest

from app.agents.gate import GateFailure
from app.agents.roles.oracle_agent import OracleOutput, RuleDecision, check_rule_decisions
from app.core.models.verification import Aggregate, BusinessRule, BusinessRuleBundle, ColumnRef, JoinSpec, Predicate, PredicateOperator, SelectPlan
from oracle_mcp.database import ColumnInfo, InMemoryGateway, TableInfo
from oracle_mcp.policy.validator import LazyObjectCatalog, PolicyViolation, SelectPolicy
from oracle_mcp.tools.builder import build_select
from oracle_mcp.tools.handlers import OracleToolHandlers


def plan(**overrides) -> SelectPlan:
    values = dict(
        base_table="APP.ORDERS",
        base_alias="o",
        joins=[JoinSpec(table="APP.CUSTOMERS", alias="c", on=[(ColumnRef(table_alias="c", column="ID"), ColumnRef(table_alias="o", column="CUSTOMER_ID"))])],
        predicates=[
            Predicate(column=ColumnRef(table_alias="o", column="STATUS"), operator=PredicateOperator.EQ, values=["A"]),
            Predicate(column=ColumnRef(table_alias="o", column="DELETED"), operator=PredicateOperator.IS_NULL),
            Predicate(column=ColumnRef(table_alias="c", column="ID"), operator=PredicateOperator.IN, values=[1, 2]),
        ],
        aggregate=Aggregate(column=ColumnRef(table_alias="o", column="ID"), distinct=True),
    )
    values.update(overrides)
    return SelectPlan(**values)


def test_builder_uses_binds_for_every_user_value():
    built = build_select(plan())
    assert "'A'" not in built.sql
    assert built.binds == {"p1": "A", "p2": 1, "p3": 2}
    assert "COUNT(DISTINCT O.ID)" in built.sql
    assert "INNER JOIN APP.CUSTOMERS C ON C.ID = O.CUSTOMER_ID" in built.sql
    assert "O.DELETED IS NULL" in built.sql
    assert "C.ID IN (:p2, :p3)" in built.sql


def test_or_groups_render_parenthesized_disjunction():
    grouped = plan(
        joins=[],
        predicates=[
            Predicate(column=ColumnRef(table_alias="o", column="STATUS"), operator=PredicateOperator.EQ, values=["A"]),
            Predicate(column=ColumnRef(table_alias="o", column="DELETED"), operator=PredicateOperator.IS_NULL, or_group=1),
            Predicate(column=ColumnRef(table_alias="o", column="RESTORED"), operator=PredicateOperator.IS_NOT_NULL, or_group=1),
        ],
    )
    built = build_select(grouped)
    assert built.sql.endswith("WHERE O.STATUS = :p1 AND (O.DELETED IS NULL OR O.RESTORED IS NOT NULL)")


@pytest.mark.parametrize(
    "bad",
    [
        dict(base_table="APP.ORDERS; DROP TABLE X"),
        dict(base_table="ORDERS"),
        dict(base_alias="o x"),
        dict(predicates=[Predicate(column=ColumnRef(table_alias="o", column="ID) OR (1=1"), operator=PredicateOperator.EQ, values=[1])]),
        dict(predicates=[Predicate(column=ColumnRef(table_alias="o", column="ID"), operator=PredicateOperator.EQ, values=[])]),
        dict(predicates=[Predicate(column=ColumnRef(table_alias="o", column="ID"), operator=PredicateOperator.IS_NULL, values=[1])]),
        dict(aggregate=Aggregate(distinct=True)),
    ],
)
def test_builder_rejects_injection_and_malformed_plans(bad):
    with pytest.raises(PolicyViolation):
        build_select(plan(**bad))


class FakeGateway(InMemoryGateway):
    def __init__(self):
        self.executed: list[tuple[str, dict[str, Any]]] = []
        orders = TableInfo("APP", "ORDERS", "TABLE", "Siparişler", [ColumnInfo("ID", "NUMBER", False), ColumnInfo("STATUS", "VARCHAR2", True, "Durum"), ColumnInfo("DELETED", "NUMBER", True), ColumnInfo("CUSTOMER_ID", "NUMBER", True)], ["ID"])
        customers = TableInfo("APP", "CUSTOMERS", "TABLE", "Müşteriler", [ColumnInfo("ID", "NUMBER", False)], ["ID"])
        orders.foreign_keys.append({"constraint": "FK1", "table": "APP.ORDERS", "columns": ["CUSTOMER_ID"], "references_table": "APP.CUSTOMERS", "references_columns": ["ID"]})
        super().__init__({"APP.ORDERS": orders, "APP.CUSTOMERS": customers})

    def execute(self, sql, binds):
        self.executed.append((sql, binds))
        return {"columns": ["RESULT_VALUE"], "column_types": ["DB_TYPE_NUMBER"], "rows": [["3"]], "row_count": 1, "truncated": False, "elapsed_ms": 1}

    def server_version(self):
        return "19.0.0"


@pytest.fixture
def handlers():
    return OracleToolHandlers(FakeGateway(), ["APP"])


def test_execute_revalidates_on_server_side_even_without_prior_validate(handlers):
    result = handlers.execute_select("DELETE FROM APP.ORDERS", {})
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_VIOLATION"
    assert handlers.gateway.executed == []


def test_execute_runs_valid_select(handlers):
    result = handlers.execute_select("SELECT COUNT(*) FROM APP.ORDERS o WHERE o.STATUS = :s", {"s": "A"})
    assert result["ok"] is True
    assert result["rows"] == [["3"]]


def test_build_select_validates_columns_against_schema(handlers):
    bad_plan = plan(predicates=[Predicate(column=ColumnRef(table_alias="o", column="INVENTED"), operator=PredicateOperator.EQ, values=["x"])])
    result = handlers.build_select(bad_plan.model_dump(mode="json"))
    assert result["ok"] is False
    assert result["error_code"] == "UNKNOWN_OBJECT"


def test_build_select_returns_sql_without_executing(handlers):
    result = handlers.build_select(plan(predicates=[]).model_dump(mode="json"))
    assert result["ok"] is True
    assert handlers.gateway.executed == []


def test_search_and_describe(handlers):
    assert handlers.search_schema("durum")["matches"][0]["table"] == "APP.ORDERS"
    described = handlers.describe_table("APP.ORDERS")
    assert described["primary_key"] == ["ID"]
    assert handlers.describe_table("OTHER.X")["ok"] is False


def test_relationships_include_incoming_and_outgoing(handlers):
    result = handlers.get_relationships("APP.CUSTOMERS")
    assert result["incoming"][0]["table"] == "APP.ORDERS"


def test_list_tables_rejects_foreign_schema(handlers):
    assert handlers.list_tables("SYS")["ok"] is False


def test_rule_decisions_strict_rejects_then_lenient_surfaces_unaddressed():
    rules = BusinessRuleBundle(rules=[BusinessRule(text="A kuralı"), BusinessRule(text="B kuralı"), BusinessRule(text="C kuralı")])
    output = OracleOutput(
        base_table="APP.ORDERS",
        primary_key_column="ID",
        parameter_columns=[],
        scope_filters=[],
        joins=[],
        rule_decisions=[RuleDecision(rule_index=0, decision="filter", note=""), RuleDecision(rule_index=1, decision="not_applicable", note="")],
        counting_unit="sipariş",
    )
    with pytest.raises(GateFailure):
        check_rule_decisions(output, rules, strict=True)
    assert check_rule_decisions(output, rules, strict=False) == ["C kuralı", "A kuralı"]


def test_lazy_catalog_loads_columns_only_for_referenced_tables():
    loaded: list[str] = []

    def loader(key: str) -> set[str]:
        loaded.append(key)
        return {"ID", "STATUS"}

    catalog = LazyObjectCatalog({"APP.ORDERS", "APP.HUGE_1", "APP.HUGE_2"}, loader)
    assert SelectPolicy(["APP"], catalog).validate("SELECT COUNT(*) FROM APP.ORDERS o WHERE o.STATUS = :s", {"s": "A"}).ok
    assert not SelectPolicy(["APP"], catalog).validate("SELECT o.MISSING FROM APP.ORDERS o", {}).ok
    assert loaded == ["APP.ORDERS"]
