import json
from datetime import datetime
import sqlite3
import time
from pathlib import Path
from typing import Any

import sqlglot

from oracle_mcp.database import ColumnInfo, InMemoryGateway, TableInfo
from oracle_mcp.settings import OracleServerSettings

SCHEMA = "APP"

TABLES = {
    "CUSTOMERS": (
        "Müşteri ana kaydı",
        [("ID", "NUMBER", False, "Müşteri kimliği"), ("NAME", "VARCHAR2", True, "Müşteri adı"), ("CUSTOMER_TYPE", "VARCHAR2", True, "Müşteri tipi: STANDARD veya TEST")],
        ["ID"],
        [],
    ),
    "ORDERS": (
        "Sipariş başlık kayıtları",
        [
            ("ID", "NUMBER", False, "Sipariş kimliği (orderId)"),
            ("CUSTOMER_ID", "NUMBER", False, "Siparişin müşterisi"),
            ("STATUS", "VARCHAR2", False, "Durum: A aktif, P beklemede, C iptal"),
            ("DELETED_AT", "DATE", True, "Soft-delete zamanı"),
            ("RESTORED_AT", "DATE", True, "Arşivden geri alınma zamanı"),
            ("TENANT_ID", "NUMBER", False, "Tenant"),
        ],
        ["ID"],
        [{"constraint": "FK_ORDERS_CUSTOMER", "table": "APP.ORDERS", "columns": ["CUSTOMER_ID"], "references_table": "APP.CUSTOMERS", "references_columns": ["ID"]}],
    ),
    "ORDER_LINES": (
        "Sipariş satırları",
        [("ID", "NUMBER", False, "Satır kimliği"), ("ORDER_ID", "NUMBER", False, "Sipariş"), ("QTY", "NUMBER", True, "Adet")],
        ["ID"],
        [{"constraint": "FK_LINES_ORDER", "table": "APP.ORDER_LINES", "columns": ["ORDER_ID"], "references_table": "APP.ORDERS", "references_columns": ["ID"]}],
    ),
}


def order_rows() -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    next_id = 1000
    for customer_id, active, pending, cancelled, deleted in ((7, 12, 2, 1, 3), (8, 9, 1, 0, 2)):
        for status, count, deleted_at in (("A", active, None), ("P", pending, None), ("C", cancelled, None), ("A", deleted, "2026-01-01")):
            for _ in range(count):
                rows.append((next_id, customer_id, status, deleted_at, None, 1))
                next_id += 1
    return rows


def create_database(path: Path) -> Path:
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE CUSTOMERS (ID INTEGER PRIMARY KEY, NAME TEXT, CUSTOMER_TYPE TEXT)")
    connection.execute("CREATE TABLE ORDERS (ID INTEGER PRIMARY KEY, CUSTOMER_ID INTEGER, STATUS TEXT, DELETED_AT TEXT, RESTORED_AT TEXT, TENANT_ID INTEGER)")
    connection.execute("CREATE TABLE ORDER_LINES (ID INTEGER PRIMARY KEY, ORDER_ID INTEGER, QTY INTEGER)")
    connection.execute("CREATE TABLE SQL_LOG (AT TEXT, SQL_TEXT TEXT, BINDS TEXT)")
    connection.executemany("INSERT INTO CUSTOMERS VALUES (?, ?, ?)", [(7, "Yedi A.Ş.", "STANDARD"), (8, "Sekiz Ltd.", "STANDARD")])
    orders = order_rows()
    connection.executemany("INSERT INTO ORDERS VALUES (?, ?, ?, ?, ?, ?)", orders)
    connection.executemany("INSERT INTO ORDER_LINES VALUES (?, ?, ?)", [(index, order[0], 1) for index, order in enumerate(orders * 2, start=1)])
    connection.commit()
    connection.close()
    return path


def simulated_tables() -> dict[str, TableInfo]:
    result = {}
    for name, (comment, columns, primary_key, foreign_keys) in TABLES.items():
        info = TableInfo(SCHEMA, name, "TABLE", comment, [ColumnInfo(*column) for column in columns], list(primary_key), [dict(item) for item in foreign_keys])
        result[info.key] = info
    return result


class SqliteSimulationGateway(InMemoryGateway):
    def __init__(self, path: Path):
        super().__init__(simulated_tables())
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.execute(f"ATTACH DATABASE ? AS {SCHEMA}", (str(self.path),))
        return connection

    def server_version(self) -> str:
        return "SIMULATION-sqlite"

    def database_time(self) -> str:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    def recent_statements(self, since: str) -> list[dict[str, Any]]:
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute("SELECT rowid, SQL_TEXT, BINDS FROM SQL_LOG WHERE AT >= ? ORDER BY rowid DESC LIMIT 30", (since,)).fetchall()
        finally:
            connection.close()
        return [{"sql_id": f"sim{rowid}", "schema": "SERVICE_APP", "module": "JDBC Thin Client", "last_active": since, "executions": 1, "sql": text, "binds": [str(value) for value in json.loads(binds)]} for rowid, text, binds in rows]

    def execute(self, sql: str, binds: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        translated = sqlglot.transpile(sql, read="oracle", write="sqlite")[0]
        connection = self._connect()
        try:
            cursor = connection.execute(translated, binds)
            rows = [list(row) for row in cursor.fetchall()]
            columns = [description[0] for description in cursor.description or []]
        finally:
            connection.close()
        return {"columns": columns, "column_types": ["SIMULATED"] * len(columns), "rows": rows, "row_count": len(rows), "truncated": False, "truncate_reason": "", "elapsed_ms": int((time.perf_counter() - started) * 1000)}


SERVICE_COUNT_SQL = {
    "listOrders": "select count(o1_0.ID) from ORDERS o1_0 where o1_0.CUSTOMER_ID=:1 and o1_0.STATUS='A' and o1_0.DELETED_AT is null",
    "listLegacyOrders": "select count(o1_0.ID) from ORDERS o1_0 where o1_0.CUSTOMER_ID=:1 and o1_0.STATUS='A'",
}
SERVICE_PAGE_SQL = {
    "listOrders": "select o1_0.ID,o1_0.STATUS from ORDERS o1_0 where o1_0.CUSTOMER_ID=:1 and o1_0.STATUS='A' and o1_0.DELETED_AT is null order by o1_0.ID offset :2 rows fetch first :3 rows only",
    "listLegacyOrders": "select o1_0.ID,o1_0.STATUS from ORDERS o1_0 where o1_0.CUSTOMER_ID=:1 and o1_0.STATUS='A' order by o1_0.ID offset :2 rows fetch first :3 rows only",
}


def record_statement(connection: sqlite3.Connection, sql: str, binds: list[object]) -> None:
    connection.execute("INSERT INTO SQL_LOG (AT, SQL_TEXT, BINDS) VALUES (?, ?, ?)", (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), sql, json.dumps(binds)))


def service_orders(path: Path, operation: str, customer_id: int) -> list[int]:
    deleted_filter = " AND DELETED_AT IS NULL" if operation == "listOrders" else ""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(f"SELECT ID FROM ORDERS WHERE CUSTOMER_ID = ? AND STATUS = 'A'{deleted_filter} ORDER BY ID", (customer_id,)).fetchall()
        record_statement(connection, SERVICE_COUNT_SQL[operation], [customer_id])
        record_statement(connection, SERVICE_PAGE_SQL[operation], [customer_id, 0, 20])
        connection.commit()
    finally:
        connection.close()
    return [row[0] for row in rows]


def active_orders(path: Path, customer_id: int) -> list[int]:
    return service_orders(path, "listOrders", customer_id)


def openapi_document() -> bytes:
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Sipariş Servisi (SIMULATION)"},
        "paths": {
            "/orders": {
                "get": {
                    "operationId": "listOrders",
                    "summary": "Müşterinin aktif siparişlerini sayfalı listeler",
                    "tags": ["orders"],
                    "parameters": [
                        {"name": "customerId", "in": "query", "required": True, "schema": {"type": "integer"}},
                        {"name": "page", "in": "query", "schema": {"type": "integer", "default": 0}},
                        {"name": "size", "in": "query", "schema": {"type": "integer", "default": 20}},
                    ],
                    "responses": {
                        "200": {
                            "description": "Spring Page",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "content": {"type": "array", "items": {"type": "object", "properties": {"orderId": {"type": "integer"}, "status": {"type": "string"}}}},
                                            "totalElements": {"type": "integer"},
                                            "number": {"type": "integer"},
                                            "size": {"type": "integer"},
                                            "last": {"type": "boolean"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/orders/legacy": {
                "get": {
                    "operationId": "listLegacyOrders",
                    "summary": "Eski sipariş listesi (legacy uç)",
                    "tags": ["orders"],
                    "parameters": [
                        {"name": "customerId", "in": "query", "required": True, "schema": {"type": "integer", "example": 7}},
                        {"name": "page", "in": "query", "schema": {"type": "integer", "default": 0}},
                        {"name": "size", "in": "query", "schema": {"type": "integer", "default": 20}},
                    ],
                    "responses": {"200": {"description": "Spring Page", "content": {"application/json": {"schema": {"type": "object", "properties": {"content": {"type": "array", "items": {"type": "object", "properties": {"orderId": {"type": "integer"}}}}, "totalElements": {"type": "integer"}}}}}}},
                }
            },
            "/customers/{id}": {"get": {"operationId": "getCustomer", "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "ok"}}}},
        },
    }
    return json.dumps(document).encode()


def simulation_db_path() -> Path:
    settings = OracleServerSettings.from_environment()
    direct = Path(settings.service_name)
    return direct if direct.is_file() else settings.secrets_dir.resolve().parent.parent / f"{settings.service_name}.sqlite3"
