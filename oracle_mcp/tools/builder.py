import re
from dataclasses import dataclass, field

from app.core.models.common import ErrorCode
from app.core.models.verification import ColumnRef, Predicate, PredicateOperator, SelectPlan
from oracle_mcp.policy.validator import PolicyViolation

IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
VALUELESS_OPERATORS = {PredicateOperator.IS_NULL, PredicateOperator.IS_NOT_NULL}


@dataclass
class BuiltSelect:
    sql: str
    binds: dict[str, str | int] = field(default_factory=dict)


def identifier(value: str) -> str:
    if not IDENTIFIER.match(value or ""):
        raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Geçersiz tanımlayıcı: {value!r}")
    return value.upper()


def qualified_table(value: str) -> str:
    parts = value.split(".")
    if len(parts) != 2:
        raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Tablo SCHEMA.TABLE biçiminde olmalı: {value!r}")
    return ".".join(identifier(part) for part in parts)


def column(reference: ColumnRef) -> str:
    return f"{identifier(reference.table_alias)}.{identifier(reference.column)}"


class SelectBuilder:
    def __init__(self) -> None:
        self.binds: dict[str, str | int] = {}

    def build(self, plan: SelectPlan) -> BuiltSelect:
        self.binds = {}
        clauses = [f"SELECT {self._aggregate(plan)} AS RESULT_VALUE", f"FROM {qualified_table(plan.base_table)} {identifier(plan.base_alias)}"]
        clauses.extend(self._join(join) for join in plan.joins)
        if plan.predicates:
            clauses.append("WHERE " + " AND ".join(self._conditions(plan.predicates)))
        return BuiltSelect(sql="\n".join(clauses), binds=dict(self.binds))

    def _aggregate(self, plan: SelectPlan) -> str:
        aggregate = plan.aggregate
        if aggregate.column is None:
            if aggregate.function.value != "COUNT" or aggregate.distinct:
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, "Kolonsuz agregasyon yalnızca COUNT(*) olabilir")
            return "COUNT(*)"
        distinct = "DISTINCT " if aggregate.distinct else ""
        return f"{aggregate.function.value}({distinct}{column(aggregate.column)})"

    def _join(self, join) -> str:
        if not join.on:
            raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"JOIN koşulu eksik: {join.table}")
        conditions = " AND ".join(f"{column(left)} = {column(right)}" for left, right in join.on)
        return f"{join.join_type.value} JOIN {qualified_table(join.table)} {identifier(join.alias)} ON {conditions}"

    def _conditions(self, predicates: list[Predicate]) -> list[str]:
        slots: list[str | int] = []
        groups: dict[int, list[str]] = {}
        for predicate in predicates:
            rendered = self._predicate(predicate)
            if not predicate.or_group:
                slots.append(rendered)
                continue
            if predicate.or_group not in groups:
                groups[predicate.or_group] = []
                slots.append(predicate.or_group)
            groups[predicate.or_group].append(rendered)
        return [slot if isinstance(slot, str) else f"({' OR '.join(groups[slot])})" for slot in slots]

    def _bind(self, value: str | int) -> str:
        name = f"p{len(self.binds) + 1}"
        self.binds[name] = value
        return f":{name}"

    def _predicate(self, predicate: Predicate) -> str:
        target = column(predicate.column)
        operator = predicate.operator
        if operator in VALUELESS_OPERATORS:
            if predicate.values:
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"{operator.value} değer almaz")
            return f"{target} {operator.value}"
        if not predicate.values:
            raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"{target} için değer eksik")
        if operator is PredicateOperator.IN:
            placeholders = ", ".join(self._bind(value) for value in predicate.values)
            return f"{target} IN ({placeholders})"
        if len(predicate.values) != 1:
            raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"{operator.value} tek değer alır")
        return f"{target} {operator.value} {self._bind(predicate.values[0])}"


def build_select(plan: SelectPlan) -> BuiltSelect:
    return SelectBuilder().build(plan)
