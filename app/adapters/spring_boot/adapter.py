from collections.abc import Iterator
from typing import Any, Protocol

from app.core.models.service import PaginationStrategy, ParameterLocation, ResponseProfile, ServiceOperation

TOTAL_CANDIDATES = ("totalElements", "total", "totalCount", "total_count", "count", "totalItems", "totalRecords")
ID_CANDIDATES = ("id", "ID", "Id", "uuid", "key")
PAGE_PARAMS = ("page", "pageNumber", "pageNo", "page_number")
SIZE_PARAMS = ("size", "pageSize", "limit", "page_size", "per_page")
ERROR_CONTAINERS = {"fault", "faults", "error", "errors", "warnings", "messages", "objecterrors", "fielderrors", "violations"}
MAX_DEPTH = 5


class BackendFrameworkAdapter(Protocol):
    name: str

    def response_profile(self, operation: ServiceOperation, sample: Any | None = None) -> ResponseProfile: ...


def lookup(payload: Any, path: str) -> Any:
    if not path:
        return payload
    node = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def schema_properties(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    properties = dict(schema.get("properties", {}) or {})
    for part in schema.get("allOf", []) or []:
        properties.update(schema_properties(part))
    return properties


def join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


class SchemaView:
    def children(self, node: Any) -> dict[str, Any]:
        return schema_properties(node)

    def is_array(self, node: Any) -> bool:
        return isinstance(node, dict) and node.get("type") == "array"

    def first_item(self, node: Any) -> dict[str, Any]:
        return schema_properties(node.get("items"))


class SampleView:
    def children(self, node: Any) -> dict[str, Any]:
        return node if isinstance(node, dict) else {}

    def is_array(self, node: Any) -> bool:
        return isinstance(node, list)

    def first_item(self, node: Any) -> dict[str, Any]:
        return node[0] if node and isinstance(node[0], dict) else {}


def walk(node: Any, view, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, Any]]:
    yield prefix, node
    if depth >= MAX_DEPTH:
        return
    for name, child in view.children(node).items():
        if name.lower() in ERROR_CONTAINERS:
            continue
        yield from walk(child, view, join(prefix, name), depth + 1)


def total_in(names: dict[str, Any], prefix: str) -> str:
    for candidate in TOTAL_CANDIDATES:
        if candidate in names:
            return join(prefix, candidate)
    return ""


def id_from_items(properties: dict[str, Any]) -> str:
    for candidate in ID_CANDIDATES:
        if candidate in properties:
            return candidate
    id_like = sorted(name for name in properties if name.lower().endswith("id"))
    return id_like[0] if len(id_like) == 1 else "id"


def parent_of(path: str) -> str:
    return path.rpartition(".")[0]


def detect_envelope(root: Any, view) -> tuple[str, str, str, str] | None:
    nodes = list(walk(root, view))
    for path, node in nodes:
        names = view.children(node)
        if "content" in names and view.is_array(names["content"]) and total_in(names, ""):
            return join(path, "content"), total_in(names, path), id_from_items(view.first_item(names["content"])), "Spring Page"
    arrays = [(path, node) for path, node in nodes if view.is_array(node)]
    if not arrays:
        return None
    items_path, array = arrays[0]
    total = ""
    for scope in (parent_of(items_path), ""):
        container = lookup(root, scope) if view is SAMPLE_VIEW else schema_at(root, scope)
        total = total_in(view.children(container), scope)
        if total:
            break
    return items_path, total, id_from_items(view.first_item(array)), "ilk liste alanı"


def schema_at(schema: Any, path: str) -> Any:
    node = schema
    for part in [segment for segment in path.split(".") if segment]:
        node = schema_properties(node).get(part)
    return node


SCHEMA_VIEW = SchemaView()
SAMPLE_VIEW = SampleView()


class SpringBootAdapter:
    name = "spring_boot"

    def response_profile(self, operation: ServiceOperation, sample: Any | None = None) -> ResponseProfile:
        profile = ResponseProfile()
        self._detect_pagination_params(operation, profile)
        if isinstance(sample, (dict, list)):
            self._apply(profile, detect_envelope(sample, SAMPLE_VIEW), "örnek yanıt")
        else:
            response = operation.success_response()
            self._apply(profile, detect_envelope(response.json_schema, SCHEMA_VIEW) if response and response.json_schema else None, "şema")
        return profile

    def refine_with_sample(self, profile: ResponseProfile, sample: Any) -> ResponseProfile:
        detected = detect_envelope(sample, SAMPLE_VIEW)
        if detected is None or not isinstance(lookup(sample, detected[0]), list):
            return profile
        refined = profile.model_copy()
        self._apply(refined, detected, "ilk sayfa yanıtı")
        return refined

    def _apply(self, profile: ResponseProfile, detected: tuple[str, str, str, str] | None, source: str) -> None:
        if detected is None:
            return
        profile.items_path, profile.total_path, profile.id_path, kind = detected
        profile.detected_by = "; ".join(part for part in (profile.detected_by, f"{kind} ({source})") if part)

    def _detect_pagination_params(self, operation: ServiceOperation, profile: ResponseProfile) -> None:
        query_names = {parameter.name for parameter in operation.parameters if parameter.location is ParameterLocation.QUERY}
        if "pageable" in query_names:
            query_names |= {"page", "size"}
        page = next((name for name in PAGE_PARAMS if name in query_names), "")
        size = next((name for name in SIZE_PARAMS if name in query_names), "")
        if "offset" in query_names and "limit" in query_names:
            profile.pagination = PaginationStrategy.OFFSET_LIMIT
            profile.page_param, profile.size_param, profile.first_page = "offset", "limit", 0
            profile.detected_by = "offset/limit parametreleri"
        elif page and size:
            profile.pagination = PaginationStrategy.PAGE_NUMBER
            profile.page_param, profile.size_param = page, size
            profile.first_page = 0 if page == "page" or "pageable" in query_names else 1
            profile.detected_by = f"{page}/{size} parametreleri"
