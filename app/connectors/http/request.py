import base64
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.project import AuthType
from app.core.models.service import ParameterLocation, ServiceOperation
from app.core.models.verification import HttpRequestSpec

DELIMITERS = {"form": ",", "spaceDelimited": " ", "pipeDelimited": "|", "tsv": "\t"}
UUID_PLACEHOLDER = "{uuid}"


@dataclass
class PreparedRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    secret_values: list[str] = field(default_factory=list)


def join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def render_path(template: str, path_params: dict[str, str]) -> str:
    rendered = template
    for name, value in path_params.items():
        rendered = rendered.replace("{" + name + "}", quote(str(value), safe=""))
    if "{" in rendered:
        raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"Doldurulmamış path parametresi: {rendered}")
    return rendered


def serialize_query(spec: HttpRequestSpec, operation: ServiceOperation | None) -> list[tuple[str, str]]:
    styles = {parameter.name: parameter for parameter in operation.parameters if parameter.location is ParameterLocation.QUERY} if operation else {}
    pairs: list[tuple[str, str]] = []
    for name, value in spec.query_params.items():
        if isinstance(value, list):
            parameter = styles.get(name)
            explode = parameter.explode if parameter else True
            if explode:
                pairs.extend((name, str(item)) for item in value)
            else:
                pairs.append((name, DELIMITERS.get(parameter.style if parameter else "form", ",").join(str(item) for item in value)))
        else:
            pairs.append((name, str(value)))
    return pairs


def build_url(spec: HttpRequestSpec, operation: ServiceOperation | None = None, extra_query: list[tuple[str, str]] | None = None) -> str:
    url = join_url(spec.base_url, render_path(spec.path_template, spec.path_params))
    pairs = serialize_query(spec, operation) + list(extra_query or [])
    return f"{url}?{urlencode(pairs, quote_via=quote)}" if pairs else url


def same_origin(url: str, base_url: str) -> bool:
    target, base = urlsplit(url), urlsplit(base_url)
    return (target.scheme, target.netloc.lower()) == (base.scheme, base.netloc.lower())


def apply_auth(spec: HttpRequestSpec, secret: str | None) -> tuple[dict[str, str], list[tuple[str, str]]]:
    if spec.auth_type is AuthType.NONE:
        return {}, []
    if not secret:
        raise ContractFlowError(ErrorCode.AUTH_REQUIRED, "Kimlik bilgisi tanımlı değil")
    if spec.auth_type is AuthType.BEARER:
        return {"Authorization": f"Bearer {secret}"}, []
    if spec.auth_type is AuthType.BASIC:
        token = base64.b64encode(f"{spec.auth_username}:{secret}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}, []
    if spec.auth_type is AuthType.API_KEY_HEADER:
        return {spec.api_key_name: secret}, []
    return {}, [(spec.api_key_name, secret)]


def parse_header_lines(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip():
            headers[name.strip()] = value.strip()
    return headers


def resolve_header_values(headers: dict[str, str]) -> dict[str, str]:
    return {name: str(uuid4()) if value == UUID_PLACEHOLDER else value for name, value in headers.items()}


def prepare(
    spec: HttpRequestSpec,
    operation: ServiceOperation | None,
    secret: str | None,
    extra_query: list[tuple[str, str]] | None = None,
    fixed_headers: dict[str, str] | None = None,
) -> PreparedRequest:
    if spec.method != "GET":
        raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "İlk sürüm yalnızca GET operasyonlarını yürütür")
    auth_headers, auth_query = apply_auth(spec, secret)
    url = build_url(spec, operation, list(extra_query or []) + auth_query)
    if not same_origin(url, spec.base_url):
        raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "İstek doğrulanmış base URL dışına çıkıyor")
    configured = resolve_header_values(fixed_headers or {})
    headers = {"Accept": "application/json", **spec.headers, **configured, **auth_headers}
    secrets = [value for value in (secret, *configured.values()) if value]
    if spec.auth_type is AuthType.BASIC and secret:
        secrets.append(auth_headers["Authorization"].split(" ", 1)[1])
    return PreparedRequest(method=spec.method, url=url, headers=headers, secret_values=secrets)
