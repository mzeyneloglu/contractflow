import asyncio
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama

from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.project import ModelProfile
from app.rag.embeddings import installed_models

LOCK_WAIT_MARGIN_SECONDS = 30
KEEP_ALIVE = "30m"

_model_lock = threading.Lock()


@dataclass
class Completion:
    content: str
    input_tokens: int = 0
    output_tokens: int = 0


ProgressCallback = Callable[[int, float, str], None]
PARTIAL_REASON = re.compile(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)')
PROGRESS_INTERVAL_SECONDS = 2.0


class ModelProvider(Protocol):
    name: str

    async def complete_json(self, messages: list[BaseMessage], schema: dict[str, Any], on_progress: ProgressCallback | None = None) -> Completion: ...


class OllamaModelProvider:
    def __init__(self, profile: ModelProfile):
        self.profile = profile
        self.name = f"ollama/{profile.model}"

    def _chat(self, schema: dict[str, Any]) -> ChatOllama:
        return ChatOllama(
            model=self.profile.model,
            base_url=self.profile.base_url,
            temperature=self.profile.temperature,
            num_ctx=self.profile.num_ctx,
            num_predict=self.profile.num_predict,
            format=schema,
            keep_alive=KEEP_ALIVE,
            client_kwargs={"timeout": self.profile.request_timeout_seconds},
        )

    def _invoke_serialized(self, messages: list[BaseMessage], schema: dict[str, Any], on_progress: ProgressCallback | None) -> Completion:
        if not _model_lock.acquire(timeout=self.profile.request_timeout_seconds + LOCK_WAIT_MARGIN_SECONDS):
            raise ContractFlowError(ErrorCode.MODEL_UNAVAILABLE, "Model meşgul veya önceki çağrı yanıt vermiyor")
        started = time.monotonic()
        last_report = started
        parts: list[str] = []
        usage: dict[str, int] = {}
        try:
            for chunk in self._chat(schema).stream(messages):
                parts.append(str(chunk.content))
                usage = chunk.usage_metadata or usage
                now = time.monotonic()
                if on_progress and now - last_report >= PROGRESS_INTERVAL_SECONDS:
                    on_progress(len(parts), now - started, partial_reason(parts))
                    last_report = now
        except Exception as error:
            raise ContractFlowError(ErrorCode.MODEL_UNAVAILABLE, f"Model çağrısı başarısız: {type(error).__name__}") from error
        finally:
            _model_lock.release()
        if on_progress:
            on_progress(len(parts), time.monotonic() - started, partial_reason(parts))
        return Completion("".join(parts), usage.get("input_tokens", 0), usage.get("output_tokens", 0))

    async def complete_json(self, messages: list[BaseMessage], schema: dict[str, Any], on_progress: ProgressCallback | None = None) -> Completion:
        deadline = self.profile.request_timeout_seconds * 2 + LOCK_WAIT_MARGIN_SECONDS
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._invoke_serialized, messages, schema, on_progress), timeout=deadline)
        except TimeoutError as error:
            raise ContractFlowError(ErrorCode.MODEL_UNAVAILABLE, f"Model {deadline} saniyede yanıt vermedi; Ollama'yı kontrol edip tekrar deneyin") from error


def partial_reason(parts: list[str]) -> str:
    match = PARTIAL_REASON.search("".join(parts))
    return match.group(1).replace('\\"', '"')[-240:] if match else ""


def check_ollama(profile: ModelProfile) -> list[str]:
    models = installed_models(profile.base_url, timeout=5)
    missing = [name for name in (profile.model, profile.embedding_model) if name not in models]
    return [f"ollama pull {name}" for name in missing]
