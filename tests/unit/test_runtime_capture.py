from app.code.runtime_capture import CapturedStatement, OtelStatementStore, correlated_log_statements, otlp_statements, rank_statements, trace_headers, trace_identifiers
from app.persistence.database import Database


def otlp_payload(trace_id: str) -> dict:
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {"traceId": trace_id.upper(), "attributes": [{"key": "db.query.text", "value": {"stringValue": "select count(*) from ORDERS where CUSTOMER_ID=?"}}]},
                            {"traceId": trace_id, "attributes": [{"key": "http.route", "value": {"stringValue": "/orders"}}]},
                        ]
                    }
                ]
            }
        ]
    }


def test_trace_headers_are_deterministic_w3c():
    trace_id, span_id = trace_identifiers("run_1")
    headers = trace_headers("run_1")
    assert headers["traceparent"] == f"00-{trace_id}-{span_id}-01"
    assert len(trace_id) == 32 and len(span_id) == 16


def test_otlp_json_spans_are_stored_by_trace(tmp_path):
    store = OtelStatementStore(Database(tmp_path / "otel.sqlite3"))
    trace_id, _ = trace_identifiers("run_otel")
    assert otlp_statements(otlp_payload(trace_id)) == [(trace_id, "select count(*) from ORDERS where CUSTOMER_ID=?")]
    assert store.ingest(otlp_payload(trace_id)) == 1
    assert store.statements(trace_id) == ["select count(*) from ORDERS where CUSTOMER_ID=?"]
    assert store.has_any()


def test_correlated_log_lines_yield_sql_for_trace_only(tmp_path):
    log = tmp_path / "sql.log"
    log.write_text(
        "2026-10-30 10:00:01 [trace=abc123] p6spy|1|statement|select count(*) from ORDERS where CUSTOMER_ID=7\n"
        "2026-10-30 10:00:02 [trace=other] p6spy|1|statement|select * from SECRET\n",
        encoding="utf-8",
    )
    assert correlated_log_statements(str(log), "abc123") == ["select count(*) from ORDERS where CUSTOMER_ID=7"]
    assert correlated_log_statements(str(tmp_path / "missing.log"), "abc123") == []


def test_statements_are_ranked_count_first_and_filtered_by_known_tables():
    statements = [
        CapturedStatement("vsql", "select o.ID from ORDERS o", "a", executions=5),
        CapturedStatement("vsql", "select count(*) from ORDERS", "b", executions=1),
        CapturedStatement("vsql", "select * from V$SESSION", "c"),
        CapturedStatement("vsql", "select * from UNRELATED", "d"),
    ]
    assert [statement.reference for statement in rank_statements(statements, {"ORDERS"})] == ["b", "a"]
