import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASK = "***"
SENSITIVE_NAME_PATTERN = re.compile(r"(pass(word)?|pwd|secret|token|api[-_]?key|auth|credential|session|cookie)", re.IGNORECASE)
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}


def is_sensitive_name(name: str) -> bool:
    return name.lower() in SENSITIVE_HEADERS or bool(SENSITIVE_NAME_PATTERN.search(name))


def mask_headers(headers: dict[str, str], extra_sensitive: Iterable[str] = ()) -> dict[str, str]:
    extra = {name.lower() for name in extra_sensitive}
    return {name: MASK if is_sensitive_name(name) or name.lower() in extra else value for name, value in headers.items()}


def mask_url(url: str, extra_sensitive: Iterable[str] = ()) -> str:
    extra = {name.lower() for name in extra_sensitive}
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{MASK}@{netloc.split('@', 1)[1]}"
    query = [(name, MASK if is_sensitive_name(name) or name.lower() in extra else value) for name, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment))


def redact_values(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted({value for value in secrets if value and len(value) >= 3}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text
