from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

from app.core.models.common import ErrorCode

ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Lock,
    exp.Into,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)

ALLOWED_FUNCTION_CLASSES: tuple[type[exp.Expression], ...] = (
    exp.Count,
    exp.Sum,
    exp.Avg,
    exp.Min,
    exp.Max,
    exp.Coalesce,
    exp.DecodeCase,
    exp.Upper,
    exp.Lower,
    exp.Trim,
    exp.Cast,
    exp.TryCast,
    exp.Substring,
    exp.Length,
    exp.Round,
    exp.Abs,
    exp.Concat,
    exp.ToChar,
    exp.StrToDate,
    exp.StrToTime,
    exp.ToNumber,
    exp.CurrentDate,
    exp.CurrentTimestamp,
    exp.Case,
    exp.If,
    exp.Nullif,
    exp.Greatest,
    exp.Least,
    exp.Extract,
    exp.DateTrunc,
    exp.TimestampTrunc,
    exp.RowNumber,
)

ALLOWED_ANONYMOUS_FUNCTIONS = frozenset(
    {"NVL", "NVL2", "DECODE", "TRUNC", "TO_DATE", "TO_CHAR", "TO_NUMBER", "TO_TIMESTAMP", "SYSDATE", "SYSTIMESTAMP", "INSTR", "LPAD", "RPAD", "MOD", "FLOOR", "CEIL"}
)

ALWAYS_ALLOWED_TABLES = frozenset({"DUAL"})


@dataclass
class ObjectCatalog:
    columns_by_table: dict[str, set[str]] = field(default_factory=dict)

    @staticmethod
    def key(schema: str, table: str) -> str:
        return f"{schema.upper()}.{table.upper()}"

    def has_table(self, schema: str, table: str) -> bool:
        return self.key(schema, table) in self.columns_by_table

    def columns(self, schema: str, table: str) -> set[str]:
        return self.columns_by_table.get(self.key(schema, table), set())


class LazyObjectCatalog:
    def __init__(self, table_keys: set[str], column_loader):
        self.table_keys = table_keys
        self.column_loader = column_loader
        self._columns: dict[str, set[str]] = {}

    def has_table(self, schema: str, table: str) -> bool:
        return ObjectCatalog.key(schema, table) in self.table_keys

    def columns(self, schema: str, table: str) -> set[str]:
        key = ObjectCatalog.key(schema, table)
        if key not in self.table_keys:
            return set()
        if key not in self._columns:
            self._columns[key] = self.column_loader(key)
        return self._columns[key]


@dataclass
class ValidationResult:
    ok: bool
    error_code: ErrorCode | None = None
    message: str = ""
    tables: list[str] = field(default_factory=list)
    bind_names: list[str] = field(default_factory=list)

    @classmethod
    def reject(cls, code: ErrorCode, message: str) -> "ValidationResult":
        return cls(ok=False, error_code=code, message=message)


class PolicyViolation(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SelectPolicy:
    def __init__(self, allowed_schemas: list[str], catalog: ObjectCatalog | LazyObjectCatalog | None = None):
        self.allowed_schemas = {schema.upper() for schema in allowed_schemas}
        self.catalog = catalog

    def validate(self, sql: str, binds: dict[str, object] | None = None) -> ValidationResult:
        try:
            statement = self._parse_single_statement(sql)
            self._reject_package_calls(sql)
            self._reject_forbidden_nodes(statement)
            self._check_functions(statement)
            tables = self._check_tables(statement)
            self._check_columns(statement)
            bind_names = self._check_binds(statement, binds or {})
        except PolicyViolation as violation:
            return ValidationResult.reject(violation.code, violation.message)
        return ValidationResult(ok=True, tables=sorted(tables), bind_names=bind_names)

    def _parse_single_statement(self, sql: str) -> exp.Expression:
        if not sql or not sql.strip():
            raise PolicyViolation(ErrorCode.UNSUPPORTED_QUERY, "Boş sorgu")
        try:
            statements = [statement for statement in sqlglot.parse(sql, read="oracle") if statement is not None]
        except (ParseError, TokenError) as error:
            raise PolicyViolation(ErrorCode.UNSUPPORTED_QUERY, f"Sorgu desteklenen Oracle SELECT alt kümesinde değil: {str(error).splitlines()[0]}") from error
        if len(statements) != 1:
            raise PolicyViolation(ErrorCode.POLICY_VIOLATION, "Tek bir SELECT ifadesine izin verilir")
        statement = statements[0]
        if not isinstance(statement, ALLOWED_ROOTS):
            raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Yalnızca SELECT izinlidir; bulunan: {statement.key.upper()}")
        return statement

    def _reject_package_calls(self, sql: str) -> None:
        tokens = sqlglot.tokenize(sql, read="oracle")
        for index in range(len(tokens) - 3):
            first, dot, second, parenthesis = tokens[index : index + 4]
            if dot.token_type is TokenType.DOT and parenthesis.token_type is TokenType.L_PAREN and first.token_type is TokenType.VAR and second.token_type is TokenType.VAR:
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Paket/şema fonksiyon çağrısı yasak: {first.text}.{second.text}")

    def _reject_forbidden_nodes(self, statement: exp.Expression) -> None:
        for node in statement.walk():
            if isinstance(node, FORBIDDEN_NODES):
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Yasak SQL yapısı: {node.key.upper()}")

    def _check_functions(self, statement: exp.Expression) -> None:
        for node in statement.find_all(exp.Func):
            if isinstance(node, (exp.Connector, exp.Predicate, exp.Binary, exp.Unary)):
                continue
            if isinstance(node, exp.Anonymous):
                name = str(node.this).upper()
                if name not in ALLOWED_ANONYMOUS_FUNCTIONS:
                    raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"İzin verilmeyen fonksiyon: {name}")
                continue
            if not isinstance(node, ALLOWED_FUNCTION_CLASSES):
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"İzin verilmeyen fonksiyon: {node.sql_name()}")

    def _cte_names(self, statement: exp.Expression) -> set[str]:
        return {cte.alias_or_name.upper() for cte in statement.find_all(exp.CTE)}

    def _check_tables(self, statement: exp.Expression) -> set[str]:
        cte_names = self._cte_names(statement)
        referenced: set[str] = set()
        for table in statement.find_all(exp.Table):
            name = table.name.upper()
            schema = table.db.upper()
            if "@" in name or "@" in schema or table.catalog:
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, "Database link kullanımı yasak")
            if not name:
                raise PolicyViolation(ErrorCode.UNSUPPORTED_QUERY, "Tablo ifadesi desteklenmiyor")
            if not schema and (name in cte_names or name in ALWAYS_ALLOWED_TABLES):
                continue
            if not schema:
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Tablo şema ile nitelenmeli: {name}")
            if schema not in self.allowed_schemas:
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Şema izinli listede değil: {schema}")
            if self.catalog is not None and not self.catalog.has_table(schema, name):
                raise PolicyViolation(ErrorCode.UNKNOWN_OBJECT, f"Tablo/view bulunamadı: {schema}.{name}")
            referenced.add(f"{schema}.{name}")
        return referenced

    def _check_columns(self, statement: exp.Expression) -> None:
        if self.catalog is None:
            return
        for select in statement.find_all(exp.Select):
            aliases = self._real_table_aliases(select)
            derived = self._derived_aliases(select)
            for column in select.find_all(exp.Column):
                if column.find_ancestor(exp.Select) is not select:
                    continue
                self._check_column(column, aliases, derived)

    def _real_table_aliases(self, select: exp.Select) -> dict[str, tuple[str, str]]:
        aliases: dict[str, tuple[str, str]] = {}
        for table in self._direct_tables(select):
            if table.db:
                aliases[table.alias_or_name.upper()] = (table.db.upper(), table.name.upper())
        return aliases

    def _derived_aliases(self, select: exp.Select) -> set[str]:
        names = {table.alias_or_name.upper() for table in self._direct_tables(select) if not table.db}
        names |= {subquery.alias_or_name.upper() for subquery in select.find_all(exp.Subquery) if subquery.alias}
        return names

    def _direct_tables(self, select: exp.Select) -> list[exp.Table]:
        sources: list[exp.Expression] = []
        from_clause = select.args.get("from_") or select.args.get("from")
        if from_clause is not None:
            sources.append(from_clause.this)
        sources.extend(join.this for join in select.args.get("joins") or [])
        return [source for source in sources if isinstance(source, exp.Table)]

    def _check_column(self, column: exp.Column, aliases: dict[str, tuple[str, str]], derived: set[str]) -> None:
        name = column.name.upper()
        if not name or name in {"ROWNUM", "*"}:
            return
        qualifier = column.table.upper()
        if qualifier:
            if qualifier in derived or qualifier not in aliases:
                return
            schema, table = aliases[qualifier]
            if name not in self.catalog.columns(schema, table):
                raise PolicyViolation(ErrorCode.UNKNOWN_OBJECT, f"Kolon bulunamadı: {schema}.{table}.{name}")
            return
        if len(aliases) == 1 and not derived:
            schema, table = next(iter(aliases.values()))
            known = self.catalog.columns(schema, table)
            if name not in known and not self._is_select_alias(column, name):
                raise PolicyViolation(ErrorCode.UNKNOWN_OBJECT, f"Kolon bulunamadı: {schema}.{table}.{name}")

    def _is_select_alias(self, column: exp.Column, name: str) -> bool:
        select = column.find_ancestor(exp.Select)
        return select is not None and any(expression.alias.upper() == name for expression in select.expressions if expression.alias)

    def _check_binds(self, statement: exp.Expression, binds: dict[str, object]) -> list[str]:
        names: list[str] = []
        for placeholder in statement.find_all(exp.Placeholder):
            name = str(placeholder.this or placeholder.name or "")
            if not name or name == "?":
                raise PolicyViolation(ErrorCode.POLICY_VIOLATION, "Bind parametreleri isimli olmalı (:ad)")
            names.append(name.lower())
        provided = {name.lower() for name in binds}
        missing = sorted(set(names) - provided)
        extra = sorted(provided - set(names))
        if missing:
            raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Değeri verilmeyen bind parametreleri: {', '.join(missing)}")
        if extra:
            raise PolicyViolation(ErrorCode.POLICY_VIOLATION, f"Sorguda kullanılmayan bind parametreleri: {', '.join(extra)}")
        return sorted(set(names))
