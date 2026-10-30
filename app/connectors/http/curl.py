import shlex

from app.connectors.http.request import build_url
from app.core.models.project import AuthType
from app.core.models.service import ServiceOperation
from app.core.models.verification import HttpRequestSpec
from app.secrets.masking import mask_headers

PLACEHOLDERS = {
    AuthType.BEARER: ("Authorization", "Bearer $CONTRACTFLOW_TOKEN"),
    AuthType.BASIC: ("Authorization", "Basic $CONTRACTFLOW_BASIC_AUTH"),
}


def render_curl(spec: HttpRequestSpec, operation: ServiceOperation | None = None, extra_query: list[tuple[str, str]] | None = None) -> str:
    query = list(extra_query or [])
    if spec.auth_type is AuthType.API_KEY_QUERY:
        query.append((spec.api_key_name, "$CONTRACTFLOW_API_KEY"))
    url = build_url(spec, operation, query).replace("%24CONTRACTFLOW_API_KEY", "${CONTRACTFLOW_API_KEY}")
    headers = {"Accept": "application/json", **mask_headers(spec.headers)}
    if spec.auth_type in PLACEHOLDERS:
        name, value = PLACEHOLDERS[spec.auth_type]
        headers[name] = value
    if spec.auth_type is AuthType.API_KEY_HEADER:
        headers[spec.api_key_name] = "$CONTRACTFLOW_API_KEY"
    for name in spec.header_names:
        headers[name] = f"$CONTRACTFLOW_HEADER_{header_variable(name)}"
    parts = ["curl", "-sS", "-X", spec.method, quote_allowing_variables(url)]
    for name, value in headers.items():
        parts.extend(["-H", quote_allowing_variables(f"{name}: {value}")])
    return " ".join(parts)


def header_variable(name: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in name).upper()


def quote_allowing_variables(value: str) -> str:
    if "$CONTRACTFLOW_" in value or "${CONTRACTFLOW_" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
        escaped = escaped.replace("$", "\\$").replace("\\$CONTRACTFLOW_", "$CONTRACTFLOW_").replace("\\${CONTRACTFLOW_", "${CONTRACTFLOW_")
        return f'"{escaped}"'
    return shlex.quote(value)
