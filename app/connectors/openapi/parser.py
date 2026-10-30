import copy
import hashlib
import json
import re
from typing import Any

import yaml

from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.service import (
    OperationParameter,
    ParameterLocation,
    ResponseSpec,
    SecurityScheme,
    ServiceCatalog,
    ServiceOperation,
)

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
MAX_REF_DEPTH = 32
COLLECTION_FORMAT_STYLES = {
    "csv": ("form", False),
    "multi": ("form", True),
    "ssv": ("spaceDelimited", False),
    "pipes": ("pipeDelimited", False),
    "tsv": ("tsv", False),
}
DEFAULT_STYLES = {
    ParameterLocation.QUERY: ("form", True),
    ParameterLocation.COOKIE: ("form", True),
    ParameterLocation.PATH: ("simple", False),
    ParameterLocation.HEADER: ("simple", False),
}


def load_document(content: bytes) -> dict[str, Any]:
    text = content.decode("utf-8-sig")
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ContractFlowError(ErrorCode.UNSUPPORTED_SPEC, f"Dosya geçerli JSON/YAML değil: {error}") from error
    if not isinstance(document, dict):
        raise ContractFlowError(ErrorCode.UNSUPPORTED_SPEC, "OpenAPI belgesi bir nesne olmalı")
    return document


def detect_version(document: dict[str, Any]) -> str:
    if str(document.get("swagger", "")).startswith("2."):
        return "2.0"
    version = str(document.get("openapi", ""))
    if re.match(r"^3\.[01]\.", version):
        return version
    raise ContractFlowError(ErrorCode.UNSUPPORTED_SPEC, f"Desteklenmeyen sözleşme sürümü: {version or document.get('swagger') or 'bilinmiyor'}")


class RefResolver:
    def __init__(self, document: dict[str, Any]):
        self.document = document
        self.unsupported: set[str] = set()

    def lookup(self, reference: str) -> Any:
        if not reference.startswith("#/"):
            self.unsupported.add(f"Uzak $ref desteklenmiyor: {reference}")
            return {}
        node: Any = self.document
        for part in reference[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                self.unsupported.add(f"Çözülemeyen $ref: {reference}")
                return {}
            node = node[part]
        return node

    def resolve(self, value: Any, depth: int = 0, seen: frozenset[str] = frozenset()) -> Any:
        if depth > MAX_REF_DEPTH:
            return {}
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str):
                if reference in seen:
                    return {"type": "object", "x-recursive-ref": reference}
                target = copy.deepcopy(self.lookup(reference))
                siblings = {key: item for key, item in value.items() if key != "$ref"}
                if isinstance(target, dict):
                    target.update(siblings)
                return self.resolve(target, depth + 1, seen | {reference})
            return {key: self.resolve(item, depth + 1, seen) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item, depth + 1, seen) for item in value]
        return value


def operation_identifier(method: str, path: str, raw: dict[str, Any]) -> str:
    if raw.get("operationId"):
        return str(raw["operationId"])
    slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")
    return f"{method}_{slug or 'root'}"


def parse_parameter(raw: dict[str, Any], is_swagger2: bool) -> OperationParameter | None:
    location_name = raw.get("in", "")
    if location_name not in {location.value for location in ParameterLocation}:
        return None
    location = ParameterLocation(location_name)
    schema = raw if is_swagger2 else raw.get("schema", {}) or {}
    style, explode = DEFAULT_STYLES[location]
    if is_swagger2 and schema.get("type") == "array":
        style, explode = COLLECTION_FORMAT_STYLES.get(raw.get("collectionFormat", "csv"), ("form", False))
    if not is_swagger2:
        style = raw.get("style", style)
        explode = raw.get("explode", style == "form")
    return OperationParameter(
        name=raw.get("name", ""),
        location=location,
        required=bool(raw.get("required", location is ParameterLocation.PATH)),
        schema_type=str(schema.get("type", "object" if "properties" in schema else "string")),
        description=raw.get("description", ""),
        enum=list(schema.get("enum", []) or []),
        default=schema.get("default"),
        example=raw.get("example", schema.get("example")),
        style=style,
        explode=bool(explode),
    )


def merge_parameters(path_level: list[dict[str, Any]], operation_level: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {(item.get("name"), item.get("in")): item for item in path_level}
    merged.update({(item.get("name"), item.get("in")): item for item in operation_level})
    return list(merged.values())


def json_media(content: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    for media_type, media in content.items():
        if "json" in media_type:
            return media_type, media.get("schema")
    if content:
        media_type, media = next(iter(content.items()))
        return media_type, media.get("schema")
    return "", None


def parse_responses(raw: dict[str, Any], is_swagger2: bool, produces: list[str]) -> list[ResponseSpec]:
    responses: list[ResponseSpec] = []
    for status_code, response in (raw or {}).items():
        if not isinstance(response, dict):
            continue
        if is_swagger2:
            content_type = next((media for media in produces if "json" in media), produces[0] if produces else "application/json")
            schema = response.get("schema")
        else:
            content_type, schema = json_media(response.get("content", {}) or {})
        responses.append(ResponseSpec(status_code=str(status_code), description=response.get("description", ""), content_type=content_type, json_schema=schema))
    return responses


def parse_security_schemes(document: dict[str, Any], is_swagger2: bool) -> list[SecurityScheme]:
    raw = document.get("securityDefinitions", {}) if is_swagger2 else document.get("components", {}).get("securitySchemes", {})
    schemes: list[SecurityScheme] = []
    for name, definition in (raw or {}).items():
        kind = definition.get("type", "")
        scheme = SecurityScheme(name=name, scheme_type=kind, location=definition.get("in", ""), parameter_name=definition.get("name", ""))
        if kind == "basic":
            scheme.http_scheme = "basic"
        elif kind == "http":
            scheme.http_scheme = str(definition.get("scheme", "")).lower()
            scheme.supported = scheme.http_scheme in {"basic", "bearer"}
        elif kind == "apiKey":
            scheme.supported = scheme.location in {"header", "query"}
        elif kind in {"oauth2", "openIdConnect"}:
            scheme.supported = False
            scheme.note = "Otomatik OAuth akışı yok; setup'ta Bearer token verilerek kullanılabilir"
        else:
            scheme.supported = False
        if not scheme.supported and not scheme.note:
            scheme.note = "Bu güvenlik tipi otomatik desteklenmiyor"
        schemes.append(scheme)
    return schemes


def servers(document: dict[str, Any], is_swagger2: bool) -> list[str]:
    if is_swagger2:
        host = document.get("host", "")
        base_path = document.get("basePath", "")
        schemes = document.get("schemes", ["https"])
        return [f"{scheme}://{host}{base_path}" for scheme in schemes] if host else ([base_path] if base_path else [])
    return [str(server.get("url", "")) for server in document.get("servers", []) or [] if server.get("url")]


def security_names(raw: list[dict[str, Any]] | None) -> list[str]:
    return sorted({name for requirement in raw or [] for name in requirement})


def parse_catalog(content: bytes) -> ServiceCatalog:
    document = load_document(content)
    version = detect_version(document)
    is_swagger2 = version == "2.0"
    resolver = RefResolver(document)
    global_security = document.get("security")
    global_produces = document.get("produces", []) if is_swagger2 else []
    operations: list[ServiceOperation] = []
    for path, path_item in (document.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        path_item = resolver.resolve(path_item)
        path_parameters = path_item.get("parameters", []) or []
        for method in HTTP_METHODS:
            raw = path_item.get(method)
            if not isinstance(raw, dict):
                continue
            operations.append(parse_operation(method, path, raw, path_parameters, is_swagger2, global_security, global_produces))
    warnings = sorted(resolver.unsupported)
    for operation in operations:
        operation.unsupported_features.extend(warning for warning in warnings if warning not in operation.unsupported_features)
    return ServiceCatalog(
        source_version=version,
        title=str(document.get("info", {}).get("title", "")),
        servers=servers(document, is_swagger2),
        operations=operations,
        security_schemes=parse_security_schemes(resolver.resolve(document), is_swagger2),
        warnings=warnings,
        content_hash=hashlib.sha256(content).hexdigest(),
    )


def parse_operation(
    method: str,
    path: str,
    raw: dict[str, Any],
    path_parameters: list[dict[str, Any]],
    is_swagger2: bool,
    global_security: list[dict[str, Any]] | None,
    global_produces: list[str],
) -> ServiceOperation:
    raw_parameters = merge_parameters(path_parameters, raw.get("parameters", []) or [])
    body_in_parameters = any(item.get("in") in {"body", "formData"} for item in raw_parameters)
    parameters = [parameter for item in raw_parameters if (parameter := parse_parameter(item, is_swagger2)) is not None]
    unsupported = [f"Parametre '{parameter.name}' için style={parameter.style}" for parameter in parameters if parameter.style not in {"form", "simple"}]
    security = raw.get("security", global_security)
    return ServiceOperation(
        operation_id=operation_identifier(method, path, raw),
        method=method.upper(),
        path=path,
        summary=raw.get("summary", ""),
        description=raw.get("description", ""),
        tags=list(raw.get("tags", []) or []),
        parameters=parameters,
        has_request_body=body_in_parameters or "requestBody" in raw,
        responses=parse_responses(raw.get("responses", {}), is_swagger2, raw.get("produces", global_produces) or []),
        security=security_names(security),
        executable=method == "get" and not unsupported,
        unsupported_features=unsupported,
    )
