from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.models.common import ContractFlowError, ErrorCode

QUERY_FORMATTER_VERSION = "qwen3-instruct-v1"
DEFAULT_RETRIEVAL_TASK = "Verilen soru için ilgili iş kuralı, istisna ve tanım paragraflarını getir"
BATCH_SIZE = 16


def format_query(query: str, task: str = DEFAULT_RETRIEVAL_TASK) -> str:
    stripped = query.strip()
    if stripped.startswith("Instruct:") and "\nQuery:" in stripped:
        return stripped
    return f"Instruct: {task}\nQuery: {stripped}"


class Embedder(Protocol):
    model: str
    digest: str
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


@dataclass
class ModelInfo:
    name: str
    digest: str
    capabilities: list[str]


def installed_models(base_url: str, timeout: float = 10) -> dict[str, ModelInfo]:
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise ContractFlowError(ErrorCode.MODEL_UNAVAILABLE, f"Ollama'ya ulaşılamadı ({base_url}). 'ollama serve' ile başlatın") from error
    return {
        model["name"]: ModelInfo(model["name"], model.get("digest", ""), model.get("capabilities", []) or [])
        for model in response.json().get("models", [])
    }


def require_model(base_url: str, model: str) -> ModelInfo:
    models = installed_models(base_url)
    if model not in models:
        raise ContractFlowError(ErrorCode.MODEL_UNAVAILABLE, f"Model kurulu değil: {model}. 'ollama pull {model}' çalıştırın")
    return models[model]


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, expected_dimension: int, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.expected_dimension = expected_dimension
        self.timeout = timeout
        self.digest = require_model(self.base_url, model).digest
        self.dimension = expected_dimension

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        try:
            response = httpx.post(f"{self.base_url}/api/embed", json={"model": self.model, "input": inputs}, timeout=self.timeout)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ContractFlowError(ErrorCode.MODEL_UNAVAILABLE, f"Embedding isteği başarısız: {type(error).__name__}") from error
        vectors = response.json().get("embeddings", [])
        if len(vectors) != len(inputs):
            raise ContractFlowError(ErrorCode.MODEL_CAPABILITY_ERROR, "Embedding sayısı girdi sayısıyla eşleşmiyor")
        for vector in vectors:
            if len(vector) != self.expected_dimension:
                raise ContractFlowError(ErrorCode.MODEL_CAPABILITY_ERROR, f"Embedding boyutu {len(vector)}; beklenen {self.expected_dimension}")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            vectors.extend(self._embed(texts[start : start + BATCH_SIZE]))
        return vectors

    def embed_query(self, query: str) -> list[float]:
        return self._embed([format_query(query)])[0]
