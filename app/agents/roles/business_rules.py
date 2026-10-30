from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.dispatcher import AgentTool
from app.core.models.documents import DocumentChunk, SourceReference
from app.core.models.verification import BusinessRule, BusinessRuleBundle, VerificationIntent
from app.orchestration.context import RunContext

AGENT = "business_rules"
EXCERPT_CHARS = 550
SEED_TOP_K = 3
SEED_TOPICS = (
    "aktif kayıt tanımı durum değeri",
    "silinmiş kayıtlar soft-delete hariç tutma",
    "sayma birimi benzersiz kayıt tekrar",
    "istisna hariç tutulan kayıtlar",
    "proje geneli kapsam tenant ortam politikası",
)


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=300)
    top_k: int = Field(default=5, ge=1, le=8)


class ChunkArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str


class SectionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_id: str


class RuleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=240)
    applicability: str = Field(max_length=120)
    conditions: list[Annotated[str, Field(max_length=160)]] = Field(max_length=4)
    exceptions: list[Annotated[str, Field(max_length=160)]] = Field(max_length=4)
    source_chunk_ids: list[str] = Field(max_length=3)


class RuleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[RuleItem] = Field(max_length=6)
    unresolved: list[Annotated[str, Field(max_length=200)]] = Field(max_length=4)
    conflicts: list[Annotated[str, Field(max_length=200)]] = Field(max_length=4)


def chunk_view(chunk: DocumentChunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "parent_id": chunk.parent_id,
        "source": chunk.source_filename,
        "location": chunk.location.describe(),
        "text": chunk.text[:EXCERPT_CHARS],
    }


def reference(chunk: DocumentChunk) -> SourceReference:
    return SourceReference(document_id=chunk.document_id, chunk_id=chunk.chunk_id, source_filename=chunk.source_filename, location=chunk.location, excerpt=chunk.text[:400])


class RuleResearchTools:
    def __init__(self, context: RunContext):
        self.context = context
        self.seen: dict[str, DocumentChunk] = {}

    async def search_query(self, query: str, top_k: int = 5) -> dict[str, Any]:
        hits = self.context.container.knowledge.search(self.context.snapshot, self.context.project.model, query, top_k)
        for hit in hits:
            self.seen[hit.chunk.chunk_id] = hit.chunk
        return {"ok": True, "hits": [chunk_view(hit.chunk) for hit in hits]}

    async def get_source(self, chunk_id: str) -> dict[str, Any]:
        chunk = self.context.container.knowledge.get_source(self.context.snapshot, chunk_id)
        if chunk is None:
            return {"ok": False, "error_code": "NOT_FOUND", "message": "Parça bu çalışmanın belge snapshot'ında yok"}
        self.seen[chunk.chunk_id] = chunk
        return {"ok": True, **chunk_view(chunk), "text": chunk.text}

    async def get_section(self, parent_id: str) -> dict[str, Any]:
        chunks = self.context.container.knowledge.get_section(self.context.snapshot, parent_id)
        for chunk in chunks:
            self.seen[chunk.chunk_id] = chunk
        return {"ok": True, "chunks": [chunk_view(chunk) for chunk in chunks]}

    def agent_tools(self) -> list[AgentTool]:
        return [
            AgentTool("search_query", "Belge parçalarında hibrit (vektör + kelime) arama", SearchArgs, self.search_query),
            AgentTool("get_source", "Bir parçanın tam metnini getirir", ChunkArgs, self.get_source),
            AgentTool("get_section", "Parçanın ait olduğu bölümün tüm parçalarını getirir", SectionArgs, self.get_section),
        ]


def seed_queries(intent: VerificationIntent) -> list[str]:
    filter_names = " ".join(condition.name for condition in intent.filters)
    return [f"{intent.resource_hint} {filter_names} kapsam"] + [f"{intent.resource_hint} {topic}" for topic in SEED_TOPICS]


def ground_rules(output: RuleOutput, seen: dict[str, DocumentChunk]) -> BusinessRuleBundle:
    bundle = BusinessRuleBundle(unresolved=list(output.unresolved), conflicts=list(output.conflicts))
    for item in output.rules:
        sources = [reference(seen[chunk_id]) for chunk_id in dict.fromkeys(item.source_chunk_ids) if chunk_id in seen]
        if not sources:
            bundle.unresolved.append(f"Kaynağı doğrulanamayan kural atıldı: {item.text[:120]}")
            continue
        bundle.rules.append(BusinessRule(text=item.text, applicability=item.applicability, conditions=item.conditions, exceptions=item.exceptions, sources=sources))
    return bundle


async def run_business_rules(context: RunContext, intent: VerificationIntent, clarifications: list[str]):
    tools = RuleResearchTools(context)
    runtime = context.runtime()
    seeded = []
    for query in seed_queries(intent):
        await context.emit({"type": "tool_call", "agent": AGENT, "tool": "search_query", "arguments": {"query": query, "top_k": SEED_TOP_K}, "reason": "Ön arama"})
        result = await tools.search_query(query, SEED_TOP_K)
        await context.emit({"type": "tool_result", "agent": AGENT, "tool": "search_query", "ok": True, "summary": f"{len(result['hits'])} parça"})
        seeded.extend(result["hits"])
    unique_seed = list({hit["chunk_id"]: hit for hit in seeded}.values())
    task = {"dogrulama_niyeti": intent.model_dump(mode="json"), "on_arama_sonuclari": unique_seed, "kullanici_aciklamalari": clarifications}
    result = await runtime.run(AGENT, task, tools.agent_tools(), RuleOutput)
    if result.clarification:
        return None, result.clarification
    return ground_rules(result.output, tools.seen), None
