import re
from collections.abc import Iterable
from typing import Any

from app.core.models.service import ServiceCatalog
from app.rag.lexical import LexicalIndex

SUGGESTION_LIMIT = 5


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold().strip()


def ground_options(options: Iterable[str], vocabulary: Iterable[str]) -> list[str]:
    corpus = " | ".join(normalize(entry) for entry in vocabulary)
    grounded = [option for option in options if len(normalize(option)) >= 2 and normalize(option) in corpus]
    return list(dict.fromkeys(grounded))


def catalog_vocabulary(catalog: ServiceCatalog | None) -> list[str]:
    if catalog is None:
        return []
    return [operation.search_text() for operation in catalog.operations if operation.executable]


def schema_vocabulary(table_cache: dict[str, dict[str, Any]]) -> list[str]:
    entries: list[str] = []
    for key, info in table_cache.items():
        entries.append(key)
        entries.extend(f"{key}.{column['name']} {column['name']}" for column in info.get("columns", []))
    return entries


def catalog_suggestions(catalog: ServiceCatalog | None, text: str, limit: int = SUGGESTION_LIMIT) -> list[str]:
    if catalog is None:
        return []
    executable = [operation for operation in catalog.operations if operation.executable]
    index = LexicalIndex([operation.operation_id for operation in executable], [operation.search_text() for operation in executable])
    by_id = {operation.operation_id: operation for operation in executable}
    ranked = [by_id[operation_id] for operation_id, _ in index.search(text, limit)] if text.strip() else []
    return [f"{operation.method} {operation.path}" + (f" — {operation.summary}" if operation.summary else "") for operation in ranked]
