from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from app.agents.actions import ActionValidationError, ToolRequest, ToolSpec, action_schema, parse_action
from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.project import ModelProfile
from app.model_providers.ollama import OllamaModelProvider
from app.rag.embeddings import OllamaEmbedder

PROBE_TOOL = "lookup_rule"


class ProbeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    top_k: int


class ProbeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


@dataclass
class ProbeResult:
    decision_model: str
    structured_actions: bool
    embedding_model: str
    embedding_dimension: int
    detail: str


async def probe_capabilities(profile: ModelProfile) -> ProbeResult:
    provider = OllamaModelProvider(profile)
    tools = [ToolSpec(PROBE_TOOL, ProbeArgs)]
    messages = [
        SystemMessage("Yalnızca şemaya uyan tek bir JSON aksiyon üret."),
        HumanMessage(f"'{PROBE_TOOL}' aracını query='aktif kayıt' ve top_k=3 argümanlarıyla çağır."),
    ]
    completion = await provider.complete_json(messages, action_schema(tools, ProbeOutput, allow_clarification=False))
    try:
        action = parse_action(completion.content, tools, ProbeOutput, allow_clarification=False)
    except ActionValidationError as error:
        raise ContractFlowError(ErrorCode.MODEL_CAPABILITY_ERROR, f"Model geçerli yapılandırılmış aksiyon üretemedi: {error}") from error
    if not isinstance(action, ToolRequest) or action.arguments.top_k != 3:
        raise ContractFlowError(ErrorCode.MODEL_CAPABILITY_ERROR, "Model istenen araç adını/argümanlarını üretemedi")
    embedder = OllamaEmbedder(profile.base_url, profile.embedding_model, profile.embedding_dimension, profile.request_timeout_seconds)
    vector = embedder.embed_query("aktif kayıt kuralı")
    return ProbeResult(profile.model, True, profile.embedding_model, len(vector), f"Araç çağrısı doğrulandı: {action.tool}({action.arguments.model_dump()})")
