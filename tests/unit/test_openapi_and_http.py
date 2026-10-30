import json

import httpx
import pytest

from app.adapters.spring_boot.adapter import SpringBootAdapter
from app.agents.roles.api_agent import apply_override, deterministic_choice
from app.connectors.http.curl import render_curl
from app.connectors.http.executor import HttpExecutor
from app.connectors.http.request import build_url, prepare
from app.connectors.openapi.parser import parse_catalog
from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.project import AuthType, SecretReference
from app.core.models.service import PaginationStrategy, ParameterLocation
from app.core.models.verification import FilterCondition, HttpRequestSpec, VerificationIntent
from tests.support.simulation import openapi_document

OPENAPI_30 = {
    "openapi": "3.0.3",
    "info": {"title": "Örnek"},
    "servers": [{"url": "http://evil.example.com"}],
    "security": [{"bearerAuth": []}],
    "components": {
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}, "oauth": {"type": "oauth2", "flows": {}}},
        "parameters": {"Page": {"name": "page", "in": "query", "schema": {"type": "integer"}}},
        "schemas": {
            "Item": {"type": "object", "properties": {"itemId": {"type": "integer"}, "name": {"type": "string", "nullable": True}}},
            "PageItem": {
                "type": "object",
                "properties": {
                    "content": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
                    "totalElements": {"type": "integer"},
                    "last": {"type": "boolean"},
                },
            },
        },
    },
    "paths": {
        "/items/{group}": {
            "parameters": [{"name": "group", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {
                "operationId": "listItems",
                "tags": ["items"],
                "parameters": [
                    {"$ref": "#/components/parameters/Page"},
                    {"name": "size", "in": "query", "schema": {"type": "integer"}},
                    {"name": "status", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}, "explode": False},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                    {"name": "session", "in": "cookie", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PageItem"}}}}},
            },
            "post": {"operationId": "createItem", "security": [], "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}, "responses": {"201": {"description": "c"}}},
        },
        "/remote": {"get": {"responses": {"200": {"description": "x", "content": {"application/json": {"schema": {"$ref": "other.yaml#/X"}}}}}}},
    },
}

SWAGGER_20 = {
    "swagger": "2.0",
    "host": "localhost:8080",
    "basePath": "/api",
    "schemes": ["http"],
    "securityDefinitions": {"key": {"type": "apiKey", "in": "header", "name": "X-API-KEY"}, "basic": {"type": "basic"}},
    "paths": {
        "/orders": {
            "get": {
                "operationId": "orders",
                "parameters": [{"name": "ids", "in": "query", "type": "array", "items": {"type": "integer"}, "collectionFormat": "pipes"}],
                "responses": {"200": {"description": "ok", "schema": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}}}}}},
            },
            "put": {"parameters": [{"name": "body", "in": "body", "schema": {"type": "object"}}], "responses": {"200": {"description": "ok"}}},
        }
    },
}


def catalog_for(document: dict):
    return parse_catalog(json.dumps(document).encode())


def test_openapi30_operations_refs_overrides_and_security():
    catalog = catalog_for(OPENAPI_30)
    operation = catalog.find("listItems")
    assert catalog.source_version == "3.0.3"
    assert {parameter.name for parameter in operation.parameters} == {"group", "page", "size", "status", "X-Trace", "session"}
    assert {parameter.location for parameter in operation.parameters} == set(ParameterLocation)
    status = next(parameter for parameter in operation.parameters if parameter.name == "status")
    assert status.explode is False
    assert operation.security == ["bearerAuth"]
    assert operation.executable
    create = catalog.find("createItem")
    assert create.security == [] and create.has_request_body and not create.executable
    oauth = next(scheme for scheme in catalog.security_schemes if scheme.name == "oauth")
    assert not oauth.supported and "Bearer token" in oauth.note
    assert any("Uzak $ref" in warning for warning in catalog.warnings)


def test_swagger20_normalization():
    catalog = catalog_for(SWAGGER_20)
    assert catalog.servers == ["http://localhost:8080/api"]
    ids = catalog.find("orders").parameters[0]
    assert (ids.style, ids.explode) == ("pipeDelimited", False)
    put = next(operation for operation in catalog.operations if operation.method == "PUT")
    assert put.has_request_body and put.operation_id == "put_orders"


@pytest.mark.parametrize("content", [b"{not json", b"openapi: 4.0.0\npaths: {}", b"[]"])
def test_invalid_or_unsupported_documents_raise(content):
    with pytest.raises(ContractFlowError) as error:
        parse_catalog(content)
    assert error.value.code is ErrorCode.UNSUPPORTED_SPEC


def test_yaml_openapi_is_accepted():
    catalog = parse_catalog(b"openapi: 3.1.0\ninfo: {title: t}\npaths:\n  /a:\n    get:\n      responses: {'200': {description: ok}}\n")
    assert catalog.operations[0].operation_id == "get_a"


def spec(**overrides) -> HttpRequestSpec:
    values = dict(base_url="http://127.0.0.1:8080/api", path_template="/items/{group}", path_params={"group": "a/b c"}, query_params={"status": ["A", "B"], "q": "x&y"})
    values.update(overrides)
    return HttpRequestSpec(**values)


def test_url_encoding_and_serialization_styles():
    operation = catalog_for(OPENAPI_30).find("listItems")
    url = build_url(spec(), operation)
    assert url == "http://127.0.0.1:8080/api/items/a%2Fb%20c?status=A%2CB&q=x%26y"
    exploded = build_url(spec(), None)
    assert "status=A&status=B" in exploded


def test_curl_masks_secrets_and_quotes():
    curl = render_curl(spec(auth_type=AuthType.BEARER, auth_secret=SecretReference(key="p/token"), headers={"X-Api-Key": "real"}))
    assert "Bearer $CONTRACTFLOW_TOKEN" in curl
    assert "real" not in curl
    assert curl.startswith("curl -sS -X GET 'http://127.0.0.1:8080/api/items/a%2Fb%20c?")
    query_key = render_curl(spec(auth_type=AuthType.API_KEY_QUERY, api_key_name="apikey"))
    assert "apikey=${CONTRACTFLOW_API_KEY}" in query_key


def test_prepare_injects_auth_only_at_execution_and_blocks_redirect_origin():
    prepared = prepare(spec(auth_type=AuthType.BASIC, auth_username="u"), None, "pw")
    assert prepared.headers["Authorization"].startswith("Basic ")
    with pytest.raises(ContractFlowError):
        prepare(spec(auth_type=AuthType.BEARER), None, None)
    hostile = prepare(spec(path_template="//evil.com/x", path_params={}), None, None)
    assert hostile.url.startswith("http://127.0.0.1:8080/api/")


def test_spring_page_profile_detection_from_schema():
    operation = catalog_for(OPENAPI_30).find("listItems")
    profile = SpringBootAdapter().response_profile(operation)
    assert (profile.items_path, profile.total_path, profile.id_path) == ("content", "totalElements", "itemId")
    assert profile.pagination is PaginationStrategy.PAGE_NUMBER and profile.first_page == 0


def paged_transport(total: int, page_size_limit: int | None = None, status: int = 200, duplicate: bool = False):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": "x"})
        page = int(request.url.params["page"])
        size = int(request.url.params["size"])
        start = page * size
        ids = list(range(start, min(start + size, total)))
        if duplicate and page == 1:
            ids[0] = 0
        content = [{"itemId": item, "name": None} for item in ids]
        return httpx.Response(200, json={"content": content, "totalElements": total, "last": start + size >= total})

    return httpx.MockTransport(handler), requests


async def run_fetch(total: int, page_size: int = 10, **transport_options):
    catalog = catalog_for(OPENAPI_30)
    operation = catalog.find("listItems")
    profile = SpringBootAdapter().response_profile(operation)
    profile.page_size = page_size
    transport, requests = paged_transport(total, **transport_options)
    executor = HttpExecutor(lambda reference: "tkn", transport=transport)
    request_spec = spec(query_params={"page": "5", "size": "1"}, auth_type=AuthType.BEARER, auth_secret=SecretReference(key="p/t"))
    observation = await executor.fetch_all(request_spec, operation, profile, catalog.source_version)
    return observation, requests


async def test_fetch_all_pages_counts_unique_ids():
    observation, requests = await run_fetch(25)
    assert observation.complete and observation.pages_fetched == 3
    assert observation.unique_id_count == 25 and observation.reported_total == 25
    assert [request.url.params["page"] for request in requests] == ["0", "1", "2"]
    assert all(request.headers["Authorization"] == "Bearer tkn" for request in requests)
    assert "tkn" not in observation.curl
    assert observation.schema_errors == []


async def test_duplicate_ids_across_pages_are_reported():
    observation, _ = await run_fetch(25, duplicate=True)
    assert observation.duplicate_ids == ["0"]
    assert observation.unique_id_count == 24


async def test_page_limit_makes_observation_incomplete():
    catalog = catalog_for(OPENAPI_30)
    operation = catalog.find("listItems")
    profile = SpringBootAdapter().response_profile(operation)
    profile.page_size, profile.max_pages = 10, 2
    transport, _ = paged_transport(100)
    observation = await HttpExecutor(lambda reference: "t", transport=transport).fetch_all(spec(), operation, profile, "3.0.3")
    assert not observation.complete and "sayfa limiti" in observation.incomplete_reason


@pytest.mark.parametrize(("status", "code"), [(401, ErrorCode.AUTH_REQUIRED), (403, ErrorCode.AUTH_REQUIRED), (404, ErrorCode.NOT_FOUND), (500, ErrorCode.HTTP_ERROR)])
async def test_http_status_classification(status, code):
    observation, _ = await run_fetch(5, status=status)
    assert observation.error_code is code


async def test_connection_refused_is_classified():
    def refuse(request):
        raise httpx.ConnectError("[WinError 10061] refused", request=request)

    catalog = catalog_for(OPENAPI_30)
    operation = catalog.find("listItems")
    executor = HttpExecutor(lambda reference: "t", transport=httpx.MockTransport(refuse))
    observation = await executor.fetch_all(spec(), operation, SpringBootAdapter().response_profile(operation), "3.0.3")
    assert observation.error_code is ErrorCode.CONNECTION_REFUSED
    assert "Backend projesini başlatıp" in observation.error_message


async def test_timeout_is_classified():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    executor = HttpExecutor(lambda reference: "t", transport=httpx.MockTransport(slow))
    failure = await executor.probe(spec(path_params={"group": "a"}))
    assert failure.code is ErrorCode.TIMEOUT


def schemaless_operation():
    document = {"openapi": "3.0.3", "info": {"title": "t"}, "paths": {"/rows": {"get": {"operationId": "rows", "parameters": [{"name": "page", "in": "query", "schema": {"type": "integer"}}, {"name": "size", "in": "query", "schema": {"type": "integer"}}], "responses": {"200": {"description": "ok"}}}}}}
    return catalog_for(document).find("rows")


async def test_profile_is_refined_from_first_page_when_schema_is_missing():
    operation = schemaless_operation()
    profile = SpringBootAdapter().response_profile(operation)
    profile.page_size = 2

    def handler(request):
        page = int(request.url.params["page"])
        rows = [{"rowId": page * 2 + offset} for offset in range(2)] if page < 2 else [{"rowId": 4}]
        return httpx.Response(200, json={"data": {"rows": rows}, "total": 5})

    observation = await HttpExecutor(lambda reference: "t", transport=httpx.MockTransport(handler)).fetch_all(spec(path_template="/rows", path_params={}, query_params={}), operation, profile, "3.0.3")
    assert observation.complete and observation.unique_id_count == 5 and observation.reported_total == 5
    assert observation.response_profile.items_path == "data.rows" and observation.response_profile.id_path == "rowId"


def test_user_override_locks_response_profile():
    operation = schemaless_operation()
    profile = apply_override(SpringBootAdapter().response_profile(operation), {"items_path": "items", "total_path": "count", "id_path": "key"})
    assert profile.locked and profile.items_path == "items" and profile.detected_by == "kullanıcı ayarı"


def test_nested_page_inside_envelope_skips_fault_arrays():
    fault = {"type": "object", "properties": {"objectErrors": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}}}}}}
    page = {"type": "object", "properties": {"content": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "midtermId": {"type": "integer"}}}}, "page": {"type": "integer"}, "size": {"type": "integer"}, "totalElements": {"type": "integer"}}}
    envelope = {"type": "object", "properties": {"success": {"type": "boolean"}, "fault": fault, "result": {"type": "object", "properties": {"result": page, "active": {"type": "string"}}}}}
    document = {"openapi": "3.1.0", "info": {"title": "t"}, "paths": {"/x": {"get": {"operationId": "x", "parameters": [{"name": "page", "in": "query"}, {"name": "size", "in": "query"}], "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": envelope}}}}}}}}
    profile = SpringBootAdapter().response_profile(catalog_for(document).find("x"))
    assert (profile.items_path, profile.total_path, profile.id_path) == ("result.result.content", "result.result.totalElements", "id")
    sample = {"fault": {"objectErrors": []}, "result": {"result": {"content": [{"id": 1}], "totalElements": 1}}}
    assert SpringBootAdapter().refine_with_sample(profile.model_copy(update={"items_path": ""}), sample).items_path == "result.result.content"


def test_fixed_headers_are_sent_masked_and_uuid_is_generated():
    prepared = prepare(spec(header_names=["X-EMP", "x-rly-corr"]), None, None, None, {"X-EMP": "12345", "x-rly-corr": "{uuid}"})
    assert prepared.headers["X-EMP"] == "12345"
    assert len(prepared.headers["x-rly-corr"]) == 36
    assert "12345" in prepared.secret_values
    curl = render_curl(spec(header_names=["X-EMP"]))
    assert "X-EMP: $CONTRACTFLOW_HEADER_X_EMP" in curl and "12345" not in curl


def test_deterministic_api_choice_only_for_unique_exact_match():
    catalog = parse_catalog(openapi_document())
    chosen = deterministic_choice(catalog, VerificationIntent(resource_hint="legacy", filters=[FilterCondition(name="customerId", value="7")]))
    assert chosen.operation_id == "listLegacyOrders" and chosen.query_params == {"customerId": "7"}
    assert deterministic_choice(catalog, VerificationIntent(resource_hint="orders", filters=[FilterCondition(name="customerId", value="7")])) is None
    assert deterministic_choice(catalog, VerificationIntent(resource_hint="orders", filters=[FilterCondition(name="unknown", value="7")])) is None
    assert deterministic_choice(catalog, VerificationIntent(resource_hint="", filters=[FilterCondition(name="customerId", value="7")])) is None
