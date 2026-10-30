import base64
import datetime as dt
import socket
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import oracledb

from app.core.models.common import ErrorCode
from oracle_mcp.policy.validator import LazyObjectCatalog, ObjectCatalog, PolicyViolation
from oracle_mcp.settings import OracleServerSettings

oracledb.defaults.fetch_decimals = True

ERROR_CODE_BY_PREFIX = {
    "ORA-01017": ErrorCode.AUTH_REQUIRED,
    "ORA-28000": ErrorCode.AUTH_REQUIRED,
    "ORA-00942": ErrorCode.UNKNOWN_OBJECT,
    "ORA-00904": ErrorCode.UNKNOWN_OBJECT,
    "ORA-01013": ErrorCode.QUERY_TIMEOUT,
    "DPY-4024": ErrorCode.QUERY_TIMEOUT,
    "DPY-4011": ErrorCode.CONNECTION_REFUSED,
    "DPY-6005": ErrorCode.CONNECTION_REFUSED,
    "DPY-6000": ErrorCode.CONNECTION_REFUSED,
    "DPY-6001": ErrorCode.CONFIGURATION_ERROR,
    "ORA-12514": ErrorCode.CONFIGURATION_ERROR,
    "DPY-3010": ErrorCode.CONFIGURATION_ERROR,
    "DPY-3015": ErrorCode.CONFIGURATION_ERROR,
}

TABLES_SQL = "SELECT TABLE_NAME, TABLE_TYPE, COMMENTS FROM ALL_TAB_COMMENTS WHERE OWNER = :owner AND TABLE_TYPE IN ('TABLE', 'VIEW')"
COLUMNS_SQL = "SELECT COLUMN_NAME, DATA_TYPE, NULLABLE FROM ALL_TAB_COLUMNS WHERE OWNER = :owner AND TABLE_NAME = :table_name ORDER BY COLUMN_ID"
COLUMN_COMMENTS_SQL = "SELECT COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS WHERE OWNER = :owner AND TABLE_NAME = :table_name AND COMMENTS IS NOT NULL"
CONSTRAINTS_SQL = "SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE, R_OWNER, R_CONSTRAINT_NAME FROM ALL_CONSTRAINTS WHERE OWNER = :owner AND TABLE_NAME = :table_name AND CONSTRAINT_TYPE IN ('P', 'R')"
CONSTRAINT_COLUMNS_SQL = "SELECT TABLE_NAME, COLUMN_NAME FROM ALL_CONS_COLUMNS WHERE OWNER = :owner AND CONSTRAINT_NAME = :constraint_name ORDER BY POSITION"
INCOMING_SQL = "SELECT OWNER, TABLE_NAME, CONSTRAINT_NAME FROM ALL_CONSTRAINTS WHERE R_OWNER = :owner AND R_CONSTRAINT_NAME = :constraint_name AND CONSTRAINT_TYPE = 'R'"
CONTRACTFLOW_MODULE = "contractflow"
DATABASE_TIME_SQL = "SELECT TO_CHAR(SYSDATE, 'YYYY-MM-DD\"T\"HH24:MI:SS') FROM DUAL"
RECENT_SQL = (
    "SELECT SQL_ID, PARSING_SCHEMA_NAME, MODULE, TO_CHAR(LAST_ACTIVE_TIME, 'YYYY-MM-DD\"T\"HH24:MI:SS'), EXECUTIONS, SQL_FULLTEXT FROM V$SQL "
    "WHERE LAST_ACTIVE_TIME >= TO_DATE(:since, 'YYYY-MM-DD\"T\"HH24:MI:SS') - 1/86400 AND COMMAND_TYPE = 3 "
    "AND PARSING_SCHEMA_NAME IN ({users}) AND NVL(MODULE, '-') <> :module ORDER BY LAST_ACTIVE_TIME DESC FETCH FIRST 30 ROWS ONLY"
)
BIND_CAPTURE_SQL = "SELECT POSITION, VALUE_STRING FROM V$SQL_BIND_CAPTURE WHERE SQL_ID = :sql_id ORDER BY POSITION"
COLUMN_SEARCH_SQL = (
    "SELECT TABLE_NAME, COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER = :owner AND UPPER(COLUMN_NAME) LIKE :pattern "
    "ORDER BY TABLE_NAME, COLUMN_ID FETCH FIRST 200 ROWS ONLY"
)


class DatabaseFailure(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def classify_oracle_error(error: Exception) -> DatabaseFailure:
    text = str(error)
    for prefix, code in ERROR_CODE_BY_PREFIX.items():
        if prefix in text:
            return DatabaseFailure(code, text.splitlines()[0])
    return DatabaseFailure(ErrorCode.DATABASE_ERROR, text.splitlines()[0] if text else type(error).__name__)


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    comment: str = ""


@dataclass
class TableInfo:
    schema: str
    name: str
    object_type: str
    comment: str = ""
    columns: list[ColumnInfo] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.name}"


def table_key(schema: str, table: str) -> str:
    return f"{schema.upper()}.{table.upper()}"


def require_known_table(tables: dict[str, TableInfo], schema: str, table: str) -> TableInfo:
    key = table_key(schema, table)
    if key not in tables:
        raise PolicyViolation(ErrorCode.UNKNOWN_OBJECT, f"İzinli şemalarda bulunamadı: {key}")
    return tables[key]


def datetime_now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value[:256]).decode("ascii")
    if isinstance(value, oracledb.LOB):
        return value.read(1, 4000)
    return value


class InMemoryGateway:
    def __init__(self, tables: dict[str, TableInfo]):
        self._all = tables

    def tables(self, refresh: bool = False) -> dict[str, TableInfo]:
        return self._all

    def table_detail(self, schema: str, table: str) -> TableInfo:
        return require_known_table(self._all, schema, table)

    def incoming_foreign_keys(self, schema: str, table: str) -> list[dict[str, Any]]:
        key = table_key(schema, table)
        return [foreign_key for other in self._all.values() for foreign_key in other.foreign_keys if foreign_key["references_table"] == key]

    def search_columns(self, schema: str, term: str) -> list[tuple[str, str]]:
        return [(info.key, column.name) for info in self._all.values() if info.schema == schema for column in info.columns if term in f"{column.name} {column.comment}".upper()]

    def object_catalog(self) -> ObjectCatalog:
        return ObjectCatalog({key: {column.name for column in info.columns} for key, info in self._all.items()})

    def database_time(self) -> str:
        return datetime_now_text()

    def recent_statements(self, since: str) -> list[dict[str, Any]]:
        return []


class OracleGateway:
    def __init__(self, settings: OracleServerSettings):
        self.settings = settings
        self._connection: oracledb.Connection | None = None
        self._tables: dict[str, TableInfo] | None = None
        self._details: dict[str, TableInfo] = {}

    def connection(self) -> oracledb.Connection:
        if self._connection is None or not self._connection.is_healthy():
            try:
                self._connection = oracledb.connect(user=self.settings.username, password=self.settings.password(), dsn=self.settings.dsn)
            except oracledb.Error as error:
                raise classify_oracle_error(error) from error
            except socket.gaierror as error:
                raise DatabaseFailure(ErrorCode.DNS_FAILURE, f"Oracle host adı çözümlenemedi: {self.settings.host}. VPN/DNS bağlantısını kontrol edin") from error
            except OSError as error:
                raise DatabaseFailure(ErrorCode.CONNECTION_REFUSED, f"Oracle sunucusuna bağlanılamadı: {self.settings.host}:{self.settings.port} ({type(error).__name__})") from error
            self._connection.module = CONTRACTFLOW_MODULE
        return self._connection

    def server_version(self) -> str:
        return self.connection().version

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _metadata(self, sql: str, **binds: Any) -> list[tuple[Any, ...]]:
        connection = self.connection()
        connection.call_timeout = self.settings.metadata_timeout_seconds * 1000
        cursor = connection.cursor()
        try:
            cursor.execute(sql, binds)
            return cursor.fetchall()
        except oracledb.Error as error:
            raise classify_oracle_error(error) from error
        finally:
            cursor.close()

    def tables(self, refresh: bool = False) -> dict[str, TableInfo]:
        if self._tables is not None and not refresh:
            return self._tables
        tables: dict[str, TableInfo] = {}
        for schema in self.settings.schemas:
            for name, object_type, comment in self._metadata(TABLES_SQL, owner=schema):
                tables[table_key(schema, name)] = TableInfo(schema, name, object_type, comment or "")
        self._tables = tables
        return tables

    def table_detail(self, schema: str, table: str) -> TableInfo:
        key = table_key(schema, table)
        if key in self._details:
            return self._details[key]
        base = require_known_table(self.tables(), schema, table)
        owner, name = base.schema, base.name
        comments = dict(self._metadata(COLUMN_COMMENTS_SQL, owner=owner, table_name=name))
        columns = [ColumnInfo(column, data_type, nullable == "Y", comments.get(column) or "") for column, data_type, nullable in self._metadata(COLUMNS_SQL, owner=owner, table_name=name)]
        detail = TableInfo(owner, name, base.object_type, base.comment, columns)
        for constraint, kind, ref_owner, ref_constraint in self._metadata(CONSTRAINTS_SQL, owner=owner, table_name=name):
            own_columns = [column for _, column in self._metadata(CONSTRAINT_COLUMNS_SQL, owner=owner, constraint_name=constraint)]
            if kind == "P":
                detail.primary_key = own_columns
                continue
            referenced = self._metadata(CONSTRAINT_COLUMNS_SQL, owner=ref_owner, constraint_name=ref_constraint)
            detail.foreign_keys.append(
                {
                    "constraint": constraint,
                    "table": detail.key,
                    "columns": own_columns,
                    "references_table": table_key(ref_owner, referenced[0][0]) if referenced else "",
                    "references_columns": [column for _, column in referenced],
                }
            )
        self._details[key] = detail
        return detail

    def incoming_foreign_keys(self, schema: str, table: str) -> list[dict[str, Any]]:
        detail = self.table_detail(schema, table)
        constraints = [constraint for constraint, kind, _, _ in self._metadata(CONSTRAINTS_SQL, owner=detail.schema, table_name=detail.name) if kind == "P"]
        incoming: list[dict[str, Any]] = []
        allowed = set(self.settings.schemas)
        for constraint in constraints:
            for owner, child_table, child_constraint in self._metadata(INCOMING_SQL, owner=detail.schema, constraint_name=constraint):
                if owner not in allowed:
                    continue
                columns = [column for _, column in self._metadata(CONSTRAINT_COLUMNS_SQL, owner=owner, constraint_name=child_constraint)]
                incoming.append({"constraint": child_constraint, "table": table_key(owner, child_table), "columns": columns, "references_table": detail.key, "references_columns": detail.primary_key})
        return incoming

    def search_columns(self, schema: str, term: str) -> list[tuple[str, str]]:
        return [(table_key(schema, table), column) for table, column in self._metadata(COLUMN_SEARCH_SQL, owner=schema, pattern=f"%{term}%")]

    def database_time(self) -> str:
        return self._metadata(DATABASE_TIME_SQL)[0][0]

    def recent_statements(self, since: str) -> list[dict[str, Any]]:
        users = self.settings.service_users or self.settings.schemas
        placeholders = ", ".join(f":u{index}" for index in range(len(users)))
        binds = {f"u{index}": user for index, user in enumerate(users)}
        rows = self._metadata(RECENT_SQL.format(users=placeholders), since=since, module=CONTRACTFLOW_MODULE, **binds)
        statements = []
        for sql_id, schema, module, last_active, executions, text in rows:
            full_text = text.read() if isinstance(text, oracledb.LOB) else str(text or "")
            statements.append({"sql_id": sql_id, "schema": schema, "module": module or "", "last_active": last_active, "executions": int(executions or 0), "sql": full_text[:20000], "binds": self._captured_binds(sql_id)})
        return statements

    def _captured_binds(self, sql_id: str) -> list[str | None]:
        try:
            return [value for _, value in self._metadata(BIND_CAPTURE_SQL, sql_id=sql_id)]
        except DatabaseFailure:
            return []

    def object_catalog(self) -> LazyObjectCatalog:
        return LazyObjectCatalog(set(self.tables()), lambda key: {column.name for column in self.table_detail(*key.split(".", 1)).columns})

    def execute(self, sql: str, binds: dict[str, Any]) -> dict[str, Any]:
        connection = self.connection()
        connection.call_timeout = self.settings.timeout_seconds * 1000
        cursor = connection.cursor()
        started = time.perf_counter()
        try:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(sql, binds)
            columns = [description[0] for description in cursor.description or []]
            column_types = [getattr(description[1], "name", str(description[1])) for description in cursor.description or []]
            rows, truncated, truncate_reason = self._fetch_limited(cursor)
        except oracledb.Error as error:
            raise classify_oracle_error(error) from error
        finally:
            cursor.close()
            connection.rollback()
        return {
            "columns": columns,
            "column_types": column_types,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "truncate_reason": truncate_reason,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    def _fetch_limited(self, cursor: oracledb.Cursor) -> tuple[list[list[Any]], bool, str]:
        rows: list[list[Any]] = []
        size = 0
        while True:
            batch = cursor.fetchmany(100)
            if not batch:
                return rows, False, ""
            for raw in batch:
                if len(rows) >= self.settings.max_rows:
                    return rows, True, f"satır limiti ({self.settings.max_rows})"
                row = [json_safe(value) for value in raw]
                size += sum(len(str(value)) for value in row)
                if size > self.settings.max_bytes:
                    return rows, True, f"byte limiti ({self.settings.max_bytes})"
                rows.append(row)
