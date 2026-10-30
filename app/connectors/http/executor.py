import json
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.adapters.spring_boot.adapter import SpringBootAdapter, lookup
from app.connectors.http.curl import render_curl
from app.connectors.http.request import parse_header_lines, prepare
from app.connectors.openapi.schema_validation import validate_response
from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.project import SecretReference
from app.core.models.service import PaginationStrategy, ResponseProfile, ServiceOperation
from app.core.models.verification import ApiObservation, HttpRequestSpec
from app.secrets.masking import redact_values

SecretResolver = Callable[[SecretReference], str]


@dataclass
class HttpFailure:
    code: ErrorCode
    message: str
    status_code: int | None = None


def classify_exception(error: Exception) -> HttpFailure:
    text = str(error)
    if isinstance(error, httpx.TimeoutException):
        return HttpFailure(ErrorCode.TIMEOUT, "Servis zaman aşımına uğradı")
    if isinstance(error, httpx.ConnectError):
        cause = error.__cause__ or error.__context__
        if isinstance(cause, ssl.SSLError) or "SSL" in text or "certificate" in text.lower():
            return HttpFailure(ErrorCode.TLS_FAILURE, "TLS/sertifika doğrulaması başarısız")
        if "getaddrinfo" in text or "Name or service not known" in text or "nodename nor servname" in text:
            return HttpFailure(ErrorCode.DNS_FAILURE, "Sunucu adı çözümlenemedi")
        return HttpFailure(ErrorCode.CONNECTION_REFUSED, "Servise bağlanılamadı. Backend projesini başlatıp tekrar kontrol edin")
    return HttpFailure(ErrorCode.HTTP_ERROR, f"HTTP hatası: {type(error).__name__}")


def classify_status(status_code: int) -> HttpFailure | None:
    if status_code in {401, 403}:
        return HttpFailure(ErrorCode.AUTH_REQUIRED, f"Yetkilendirme hatası (HTTP {status_code})", status_code)
    if status_code == 404:
        return HttpFailure(ErrorCode.NOT_FOUND, "Endpoint bulunamadı (HTTP 404); URL veya endpoint eşleştirmesini kontrol edin", status_code)
    if status_code >= 400:
        return HttpFailure(ErrorCode.HTTP_ERROR, f"Servis HTTP {status_code} döndürdü", status_code)
    return None


@dataclass
class PageCollector:
    profile: ResponseProfile
    ids: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    duplicates: list[str] = field(default_factory=list)
    fetched: int = 0
    missing_ids: int = 0
    reported_total: int | None = None
    bytes_read: int = 0

    def add(self, items: list[Any]) -> None:
        self.fetched += len(items)
        for item in items:
            value = lookup(item, self.profile.id_path) if isinstance(item, dict) else None
            if value is None:
                self.missing_ids += 1
                continue
            key = str(value)
            if key in self.seen:
                self.duplicates.append(key)
                continue
            self.seen.add(key)
            self.ids.append(key)


def page_query(profile: ResponseProfile, page_index: int) -> list[tuple[str, str]]:
    if profile.pagination is PaginationStrategy.PAGE_NUMBER:
        return [(profile.page_param, str(profile.first_page + page_index)), (profile.size_param, str(profile.page_size))]
    if profile.pagination is PaginationStrategy.OFFSET_LIMIT:
        return [(profile.page_param, str(page_index * profile.page_size)), (profile.size_param, str(profile.page_size))]
    return []


def without_paging_params(spec: HttpRequestSpec, profile: ResponseProfile) -> HttpRequestSpec:
    if profile.pagination is PaginationStrategy.NONE:
        return spec
    reserved = {profile.page_param, profile.size_param}
    return spec.model_copy(update={"query_params": {name: value for name, value in spec.query_params.items() if name not in reserved}})


def parse_total(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


class HttpExecutor:
    def __init__(self, secret_resolver: SecretResolver, transport: httpx.AsyncBaseTransport | None = None):
        self.secret_resolver = secret_resolver
        self.transport = transport

    def _secret(self, spec: HttpRequestSpec) -> str | None:
        return self.secret_resolver(spec.auth_secret) if spec.auth_secret else None

    def _fixed_headers(self, spec: HttpRequestSpec) -> dict[str, str]:
        return parse_header_lines(self.secret_resolver(spec.headers_secret)) if spec.headers_secret else {}

    def client(self, spec: HttpRequestSpec) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=spec.timeout_seconds, verify=spec.verify_tls, follow_redirects=False, transport=self.transport)

    async def fetch_all(self, spec: HttpRequestSpec, operation: ServiceOperation, profile: ResponseProfile, source_version: str) -> ApiObservation:
        spec = without_paging_params(spec, profile)
        observation = ApiObservation(operation_id=operation.operation_id, curl=render_curl(spec, operation, page_query(profile, 0)), response_profile=profile)
        started = time.perf_counter()
        try:
            secret = self._secret(spec)
            fixed_headers = self._fixed_headers(spec)
        except ContractFlowError as error:
            return self._fail(observation, HttpFailure(ErrorCode.AUTH_REQUIRED, error.message), started)
        collector = PageCollector(profile)
        response_schema = (operation.success_response().json_schema if operation.success_response() else None)
        async with self.client(spec) as client:
            for page_index in range(profile.max_pages):
                outcome = await self._fetch_page(client, spec, operation, profile, page_index, secret, fixed_headers, observation)
                if isinstance(outcome, HttpFailure):
                    return self._fail(observation, outcome, started, [secret or "", *fixed_headers.values()])
                payload, size = outcome
                collector.bytes_read += size
                if page_index == 0:
                    observation.schema_errors = validate_response(response_schema, payload, source_version)
                    if not isinstance(lookup(payload, profile.items_path), list) and not profile.locked:
                        profile = SpringBootAdapter().refine_with_sample(profile, payload)
                        collector.profile = profile
                        observation.response_profile = profile
                items = lookup(payload, profile.items_path)
                if not isinstance(items, list):
                    return self._fail(observation, HttpFailure(ErrorCode.INVALID_RESPONSE, f"Yanıtta kayıt listesi bulunamadı (items_path='{profile.items_path}')"), started)
                collector.add(items)
                if collector.reported_total is None and profile.total_path:
                    collector.reported_total = parse_total(lookup(payload, profile.total_path))
                observation.pages_fetched = page_index + 1
                if self._finished(profile, payload, items, collector):
                    observation.complete = True
                    break
                if collector.bytes_read > profile.max_bytes:
                    observation.incomplete_reason = f"byte limiti aşıldı ({profile.max_bytes})"
                    break
            else:
                observation.incomplete_reason = f"sayfa limiti aşıldı ({profile.max_pages})"
        observation.reported_total = collector.reported_total
        observation.fetched_count = collector.fetched
        observation.unique_id_count = len(collector.ids)
        observation.duplicate_ids = collector.duplicates[:50]
        observation.missing_id_count = collector.missing_ids
        observation.sample_ids = collector.ids[:20]
        observation.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return observation

    async def _fetch_page(self, client, spec, operation, profile, page_index, secret, fixed_headers, observation) -> tuple[Any, int] | HttpFailure:
        try:
            prepared = prepare(spec, operation, secret, page_query(profile, page_index), fixed_headers)
            response = await client.get(prepared.url, headers=prepared.headers)
        except ContractFlowError as error:
            return HttpFailure(error.code, error.message)
        except httpx.HTTPError as error:
            return classify_exception(error)
        observation.status_code = response.status_code
        if response.is_redirect:
            return HttpFailure(ErrorCode.HTTP_ERROR, f"Yönlendirme takip edilmedi (HTTP {response.status_code})", response.status_code)
        failure = classify_status(response.status_code)
        if failure:
            return failure
        try:
            return response.json(), len(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return HttpFailure(ErrorCode.INVALID_RESPONSE, "Yanıt JSON değil", response.status_code)

    def _finished(self, profile: ResponseProfile, payload: Any, items: list[Any], collector: PageCollector) -> bool:
        if profile.pagination is PaginationStrategy.NONE:
            return True
        if isinstance(payload, dict) and payload.get("last") is True:
            return True
        if not items or len(items) < profile.page_size:
            return True
        return collector.reported_total is not None and collector.fetched >= collector.reported_total

    def _fail(self, observation: ApiObservation, failure: HttpFailure, started: float, secrets: list[str] | None = None) -> ApiObservation:
        observation.error_code = failure.code
        observation.error_message = redact_values(failure.message, secrets or [])
        observation.status_code = failure.status_code or observation.status_code
        observation.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return observation

    async def probe(self, spec: HttpRequestSpec) -> HttpFailure | None:
        try:
            prepared = prepare(spec, None, self._secret(spec), None, self._fixed_headers(spec))
        except ContractFlowError as error:
            return HttpFailure(error.code, error.message)
        try:
            async with self.client(spec) as client:
                response = await client.get(prepared.url, headers=prepared.headers)
        except httpx.HTTPError as error:
            return classify_exception(error)
        return classify_status(response.status_code)
