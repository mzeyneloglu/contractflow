from collections.abc import Callable

from pydantic import BaseModel, Field

from app.core.models.documents import DocumentChunk, DocumentStatus
from app.core.models.project import ModelProfile
from app.persistence.repositories import DocumentRepository, IndexGenerationRepository
from app.rag.embeddings import Embedder
from app.rag.store import ChunkRepository, HybridRetrievalStore, SearchHit


class KnowledgeSnapshot(BaseModel):
    project_id: str
    generation: str = ""
    document_ids: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.generation or not self.document_ids


class KnowledgeBase:
    def __init__(
        self,
        documents: DocumentRepository,
        generations: IndexGenerationRepository,
        store: HybridRetrievalStore,
        chunks: ChunkRepository,
        embedder_factory: Callable[[ModelProfile], Embedder],
    ):
        self.documents = documents
        self.generations = generations
        self.store = store
        self.chunks = chunks
        self.embedder_factory = embedder_factory

    def snapshot(self, project_id: str) -> KnowledgeSnapshot:
        active = self.generations.active(project_id)
        if active is None:
            return KnowledgeSnapshot(project_id=project_id)
        ready = [document for document in self.documents.list_for_project(project_id) if document.status is DocumentStatus.READY and document.index_generation == active["generation"]]
        return KnowledgeSnapshot(
            project_id=project_id,
            generation=active["generation"],
            document_ids=[document.id for document in ready],
            labels=[f"{document.filename}@{document.content_hash[:8]}" for document in ready],
        )

    def search(self, snapshot: KnowledgeSnapshot, model: ModelProfile, query: str, top_k: int = 6) -> list[SearchHit]:
        if snapshot.is_empty:
            return []
        return self.store.search(snapshot.project_id, snapshot.generation, snapshot.document_ids, query, self.embedder_factory(model), top_k)

    def get_source(self, snapshot: KnowledgeSnapshot, chunk_id: str) -> DocumentChunk | None:
        chunk = self.chunks.get(snapshot.generation, chunk_id)
        return chunk if chunk and chunk.document_id in snapshot.document_ids else None

    def get_section(self, snapshot: KnowledgeSnapshot, parent_id: str) -> list[DocumentChunk]:
        return [chunk for chunk in self.chunks.section(snapshot.generation, parent_id) if chunk.document_id in snapshot.document_ids]
