import re
from typing import Any, Protocol

from pydantic import ValidationError

from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.verification import SelectPlan
from oracle_mcp.database import DatabaseFailure, TableInfo
from oracle_mcp.policy.validator import LazyObjectCatalog, ObjectCatalog, PolicyViolation, SelectPolicy
from oracle_mcp.tools.builder import build_select

SEARCH_RESULT_LIMIT = 25
re_timestamp = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


class Gateway(Protocol):
    def tables(self, refresh: bool = False) -> dict[str, TableInfo]: ...

    def table_detail(self, schema: str, table: str) -> TableInfo: ...

    def incoming_foreign_keys(self, schema: str, table: str) -> list[dict[str, Any]]: ...

    def search_columns(self, schema: str, term: str) -> list[tuple[str, str]]: ...

    def database_time(self) -> str: ...

    def recent_statements(self, since: str) -> list[dict[str, Any]]: ...

    def object_catalog(self) -> ObjectCatalog | LazyObjectCatalog: ...

    def execute(self, sql: str, binds: dict[str, Any]) -> dict[str, Any]: ...

    def server_version(self) -> str: ...


def failure(code: ErrorCode, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code.value, "message": message}


def guarded(handler):
    def wrapper(*args, **kwargs) -> dict[str, Any]:
        try:
            return {"ok": True, **handler(*args, **kwargs)}
        except PolicyViolation as violation:
            return failure(violation.code, violation.message)
        except DatabaseFailure as database_failure:
            return failure(database_failure.code, database_failure.message)
        except ValidationError as error:
            return failure(ErrorCode.POLICY_VIOLATION, f"Geçersiz argüman: {error.errors()[0]['msg']}")
        except ContractFlowError as error:
            return failure(error.code, error.message)
        except Exception as error:
            return failure(ErrorCode.DATABASE_ERROR, f"Beklenmeyen sunucu hatası: {type(error).__name__}")

    wrapper.__name__ = handler.__name__
    return wrapper


def table_summary(table: TableInfo) -> dict[str, Any]:
    summary = {"table": table.key, "type": table.object_type, "comment": table.comment}
    if table.columns:
        summary["column_count"] = len(table.columns)
    return summary


class OracleToolHandlers:
    def __init__(self, gateway: Gateway, allowed_schemas: list[str]):
        self.gateway = gateway
        self.allowed_schemas = [schema.upper() for schema in allowed_schemas]

    def _validate(self, sql: str, binds: dict[str, Any]):
        structural = SelectPolicy(self.allowed_schemas).validate(sql, binds)
        if not structural.ok:
            raise PolicyViolation(structural.error_code, structural.message)
        validation = SelectPolicy(self.allowed_schemas, self.gateway.object_catalog()).validate(sql, binds)
        if not validation.ok:
            raise PolicyViolation(validation.error_code, validation.message)
        return validation

    @guarded
    def ping(self) -> dict[str, Any]:
        return {"server_version": self.gateway.server_version(), "schemas": self.allowed_schemas}

    @guarded
    def database_time(self) -> dict[str, Any]:
        return {"time": self.gateway.database_time()}

    @guarded
    def recent_service_sql(self, since: str) -> dict[str, Any]:
        if not re_timestamp.fullmatch(since):
            raise PolicyViolation(ErrorCode.POLICY_VIOLATION, "since YYYY-MM-DDTHH:MM:SS biçiminde olmalı")
        return {"statements": self.gateway.recent_statements(since)}

    @guarded
    def list_schemas(self) -> dict[str, Any]:
        return {"schemas": self.allowed_schemas}

    @guarded
    def list_tables(self, schema: str = "") -> dict[str, Any]:
        tables = self.gateway.tables().values()
        if schema:
            if schema.upper() not in self.allowed_schemas:
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Şema izinli listede değil: {schema}")
            tables = [table for table in tables if table.schema == schema.upper()]
        return {"tables": [table_summary(table) for table in tables]}

    @guarded
    def search_schema(self, keyword: str) -> dict[str, Any]:
        terms = [term.upper() for term in keyword.replace("_", " ").split() if len(term) >= 2]
        if not terms:
            raise PolicyViolation(ErrorCode.POLICY_VIOLATION, "Arama kelimesi en az 2 karakter olmalı")
        tables = self.gateway.tables()
        matched_columns: dict[str, list[str]] = {}
        for schema in self.allowed_schemas:
            for term in terms:
                for key, column in self.gateway.search_columns(schema, term):
                    if key in tables and column not in matched_columns.setdefault(key, []):
                        matched_columns[key].append(column)
        matches: list[dict[str, Any]] = []
        for key, table in tables.items():
            haystack = f"{table.name} {table.comment}".upper().replace("_", " ")
            columns = matched_columns.get(key, [])
            score = sum(term in haystack for term in terms) * 3 + len(columns)
            if score:
                matches.append({**table_summary(table), "matched_columns": columns[:10], "score": score})
        matches.sort(key=lambda match: (-match["score"], match["table"]))
        return {"matches": matches[:SEARCH_RESULT_LIMIT]}

    @guarded
    def describe_table(self, table: str) -> dict[str, Any]:
        schema, name = self._split(table)
        info = self.gateway.table_detail(schema, name)
        return {
            **table_summary(info),
            "primary_key": info.primary_key,
            "columns": [
                {"name": column.name, "type": column.data_type, "nullable": column.nullable, "comment": column.comment} for column in info.columns
            ],
        }

    @guarded
    def get_relationships(self, table: str) -> dict[str, Any]:
        schema, name = self._split(table)
        info = self.gateway.table_detail(schema, name)
        incoming = self.gateway.incoming_foreign_keys(schema, name)
        return {"table": info.key, "primary_key": info.primary_key, "outgoing": info.foreign_keys, "incoming": incoming}

    @guarded
    def build_select(self, plan: dict[str, Any]) -> dict[str, Any]:
        built = build_select(SelectPlan.model_validate(plan))
        validation = self._validate(built.sql, built.binds)
        return {"sql": built.sql, "binds": built.binds, "tables": validation.tables}

    @guarded
    def validate_select(self, sql: str, binds: dict[str, Any] | None = None) -> dict[str, Any]:
        validation = self._validate(sql, binds or {})
        return {"tables": validation.tables, "bind_names": validation.bind_names}

    @guarded
    def execute_select(self, sql: str, binds: dict[str, Any] | None = None) -> dict[str, Any]:
        binds = binds or {}
        self._validate(sql, binds)
        return {"sql": sql, "binds": binds, **self.gateway.execute(sql, binds)}

    def _split(self, table: str) -> tuple[str, str]:
        parts = table.upper().split(".")
        if len(parts) == 2:
            return parts[0], parts[1]
        if len(parts) == 1 and len(self.allowed_schemas) == 1:
            return self.allowed_schemas[0], parts[0]
        raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Tablo SCHEMA.TABLE biçiminde verilmeli: {table}")
