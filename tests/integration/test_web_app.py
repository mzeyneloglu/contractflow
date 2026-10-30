import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.models.common import RunStatus
from app.main import create_app
from app.services.container import build_container
from tests.support.simulation import openapi_document


@pytest.fixture
def client(tmp_path):
    container = build_container(Settings(data_dir=tmp_path, secret_backend="file"))
    with TestClient(create_app(container)) as test_client:
        test_client.container = container
        yield test_client


def create_project(client) -> dict:
    response = client.post("/api/projects", json={"name": "Taslak proje", "environment": "test"})
    assert response.status_code == 200
    return response.json()


def test_pages_render_in_turkish(client):
    project = create_project(client)
    for path in ("/", f"/projects/{project['id']}/setup", f"/projects/{project['id']}/verify", f"/projects/{project['id']}/history"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'lang="tr"' in response.text


def test_draft_project_is_saved_without_connections(client):
    project = create_project(client)
    assert project["is_draft"] is True
    assert project["readiness"]["oracle"]["state"] == "unknown"


def test_secrets_never_returned_and_stored_by_reference(client):
    project = create_project(client)
    response = client.put(
        f"/api/projects/{project['id']}/oracle",
        json={"host": "db.local", "port": 1521, "service_name": "ORCL", "username": "reader", "password": "TopSecret-123", "schema_allowlist": ["app", " APP "]},
    )
    body = response.json()
    assert "TopSecret-123" not in response.text
    assert body["oracle"]["password_set"] is True
    assert body["oracle"]["schema_allowlist"] == ["APP"]
    raw = client.container.database.fetch_one("SELECT payload FROM projects WHERE id = ?", (project["id"],))["payload"]
    assert "TopSecret-123" not in raw
    api_response = client.put(f"/api/projects/{project['id']}/api-profile", json={"base_url": "http://127.0.0.1:9", "auth_type": "bearer", "credential": "tok-999"})
    assert "tok-999" not in api_response.text
    assert api_response.json()["api"]["credential_set"] is True


def test_openapi_upload_and_catalog_preview(client):
    project = create_project(client)
    response = client.post(f"/api/projects/{project['id']}/openapi", files={"file": ("openapi.json", openapi_document(), "application/json")})
    assert response.status_code == 200
    catalog = response.json()
    listing = next(operation for operation in catalog["operations"] if operation["operation_id"] == "listOrders")
    assert listing["executable"] and listing["response_profile"]["total_path"] == "totalElements"


def test_invalid_openapi_is_rejected_with_message(client):
    project = create_project(client)
    response = client.post(f"/api/projects/{project['id']}/openapi", files={"file": ("bad.json", b"{oops", "application/json")})
    assert response.status_code == 422
    assert response.json()["error_code"] == "UNSUPPORTED_SPEC"


def test_service_down_is_reported_as_connection_refused(client):
    project = create_project(client)
    client.put(f"/api/projects/{project['id']}/api-profile", json={"base_url": "http://127.0.0.1:9", "auth_type": "none"})
    body = client.post(f"/api/projects/{project['id']}/api-profile/test").json()
    assert body["readiness"]["api"]["state"] == "failed"
    assert "CONNECTION_REFUSED" in body["readiness"]["api"]["message"]


def test_documents_can_be_made_optional(client):
    project = create_project(client)
    body = client.put(f"/api/projects/{project['id']}/documents-optional", json={"optional": True}).json()
    assert body["documents_optional"] is True
    assert body["readiness"]["knowledge_base"]["state"] == "ready"


def test_unsupported_document_type_is_rejected(client):
    project = create_project(client)
    response = client.post(f"/api/projects/{project['id']}/documents", files=[("files", ("notes.docx", b"x", "application/octet-stream"))])
    assert response.status_code == 400


def test_fixed_headers_are_stored_as_secret(client):
    project = create_project(client)
    rows = [{"name": "X-EMPLOYEE-NUMBER", "value": "98765"}, {"name": "x-rly-corr", "value": "{uuid}"}]
    response = client.put(f"/api/projects/{project['id']}/api-profile", json={"base_url": "http://127.0.0.1:9", "headers": rows})
    assert response.json()["api"]["header_names"] == ["X-EMPLOYEE-NUMBER", "x-rly-corr"]
    assert "98765" not in response.text
    raw = client.container.database.fetch_one("SELECT payload FROM projects WHERE id = ?", (project["id"],))["payload"]
    assert "98765" not in raw
    kept = client.put(f"/api/projects/{project['id']}/api-profile", json={"base_url": "http://127.0.0.1:9", "headers": [{"name": "X-EMPLOYEE-NUMBER", "value": ""}, {"name": "X-NEW", "value": "1"}]})
    assert kept.json()["api"]["header_names"] == ["X-EMPLOYEE-NUMBER", "X-NEW"]
    stored = client.container.secrets.get(client.container.projects.get(project["id"]).api.headers_ref)
    assert "X-EMPLOYEE-NUMBER: 98765" in stored and "x-rly-corr" not in stored
    missing_value = client.put(f"/api/projects/{project['id']}/api-profile", json={"base_url": "http://127.0.0.1:9", "headers": [{"name": "X-UNKNOWN", "value": ""}]})
    assert missing_value.status_code == 400


def test_examples_use_real_values_only(client):
    project = create_project(client)
    assert client.get(f"/api/projects/{project['id']}/examples").json() == []
    client.container.runs.create("run_example", project["id"], "midterms servisinde yearId'si 2026 olan kayıtlar 12 olmalı")
    examples = client.get(f"/api/projects/{project['id']}/examples").json()
    assert examples == ["midterms servisinde yearId'si 2026 olan kayıtlar 12 olmalı"]
    assert not any("<" in example for example in examples)


def test_single_page_and_legacy_redirects(client):
    project = create_project(client)
    assert 'id="view"' in client.get("/").text
    redirect = client.get(f"/projects/{project['id']}/setup", follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers["location"] == f"/#/projects/{project['id']}/setup"
    assert client.get("/static/logo.svg").status_code == 200


def test_event_stream_replays_finished_run(client):
    project = create_project(client)
    client.container.runs.create("run_stream", project["id"], "talep")
    client.container.runs.add_event("run_stream", {"type": "node_started", "node": "planner"})
    client.container.runs.update("run_stream", RunStatus.INCONCLUSIVE, {})
    body = client.get("/api/runs/run_stream/stream").text
    assert "event: run_event" in body and '"node": "planner"' in body and "event: run_status" in body


def test_otlp_receiver_accepts_json_and_rejects_protobuf(client):
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "ab" * 16, "attributes": [{"key": "db.statement", "value": {"stringValue": "select 1 from DUAL"}}]}]}]}]}
    assert client.post("/v1/traces", json=payload).json()["stored"] == 1
    assert client.container.otel.statements("ab" * 16) == ["select 1 from DUAL"]
    assert client.post("/v1/traces", content=b"\x00", headers={"content-type": "application/x-protobuf"}).status_code == 415
