import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.models.common import utc_now
from app.persistence.database import Database

SQL_IN_LINE = re.compile(r"\b(select|with)\b[\s\S]+", re.IGNORECASE)
LOG_TAIL_BYTES = 2_000_000
STATEMENT_ATTRIBUTES = {"db.query.text", "db.statement"}

OTEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS otel_statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS otel_statements_trace ON otel_statements(trace_id);
"""


@dataclass
class CapturedStatement:
    source: str
    sql: str
    reference: str
    binds: list[str | None] = field(default_factory=list)
    executions: int = 0


def trace_identifiers(run_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(run_id.encode()).hexdigest()
    return digest[:32], digest[32:48]


def trace_headers(run_id: str) -> dict[str, str]:
    trace_id, span_id = trace_identifiers(run_id)
    return {"traceparent": f"00-{trace_id}-{span_id}-01", "X-ContractFlow-Trace": trace_id}


class OtelStatementStore:
    def __init__(self, database: Database):
        self.database = database
        with database.connect() as connection:
            connection.executescript(OTEL_SCHEMA)

    def ingest(self, payload: dict[str, Any]) -> int:
        stored = 0
        for trace_id, statement in otlp_statements(payload):
            self.database.execute("INSERT INTO otel_statements (trace_id, statement, received_at) VALUES (?, ?, ?)", (trace_id, statement, utc_now().isoformat()))
            stored += 1
        return stored

    def has_any(self) -> bool:
        return self.database.fetch_one("SELECT 1 FROM otel_statements LIMIT 1") is not None

    def statements(self, trace_id: str) -> list[str]:
        rows = self.database.fetch_all("SELECT statement FROM otel_statements WHERE trace_id = ? ORDER BY id", (trace_id,))
        return [row["statement"] for row in rows]


def attribute_value(value: dict[str, Any]) -> str:
    return str(value.get("stringValue") or value.get("intValue") or "")


def otlp_statements(payload: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for resource in payload.get("resourceSpans", []):
        for scope in resource.get("scopeSpans", []) + resource.get("instrumentationLibrarySpans", []):
            for span in scope.get("spans", []):
                for attribute in span.get("attributes", []):
                    if attribute.get("key") in STATEMENT_ATTRIBUTES:
                        text = attribute_value(attribute.get("value", {}))
                        if text:
                            found.append((str(span.get("traceId", "")).lower(), text))
    return found


def correlated_log_statements(path: str, trace_id: str) -> list[str]:
    log_file = Path(path)
    if not trace_id or not log_file.is_file():
        return []
    with log_file.open("rb") as handle:
        handle.seek(max(0, log_file.stat().st_size - LOG_TAIL_BYTES))
        tail = handle.read().decode("utf-8", errors="replace")
    statements: list[str] = []
    for line in tail.splitlines():
        if trace_id not in line:
            continue
        match = SQL_IN_LINE.search(line.split("|")[-1]) or SQL_IN_LINE.search(line)
        if match:
            statements.append(match.group(0).strip())
    return list(dict.fromkeys(statements))


def rank_statements(statements: list[CapturedStatement], known_tables: set[str]) -> list[CapturedStatement]:
    def relevant(statement: CapturedStatement) -> bool:
        upper = statement.sql.upper()
        return "V$" not in upper and any(re.search(rf"\b{re.escape(table)}\b", upper) for table in known_tables)

    candidates = [statement for statement in statements if relevant(statement)]
    return sorted(candidates, key=lambda statement: (not re.match(r"\s*select\s+count\s*\(", statement.sql, re.IGNORECASE), -statement.executions))


def describe(statement: CapturedStatement) -> dict[str, Any]:
    return {"source": statement.source, "reference": statement.reference, "raw_sql": statement.sql[:4000]}
