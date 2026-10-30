import threading
from dataclasses import dataclass
from functools import lru_cache

from app.code.runtime_capture import OtelStatementStore
from app.config import Settings, get_settings
from app.core.models.project import ModelProfile
from app.persistence.database import Database
from app.persistence.repositories import (
    DocumentRepository,
    IndexGenerationRepository,
    MappingRepository,
    OpenApiRepository,
    OperationResultRepository,
    ProjectRepository,
    RunRepository,
)
from app.rag.embeddings import Embedder, OllamaEmbedder
from app.rag.indexer import DocumentIndexer
from app.rag.knowledge import KnowledgeBase
from app.rag.store import ChunkRepository, HybridRetrievalStore
from app.rag.tokenizer import TokenCounter, load_tokenizer
from app.secrets.store import SecretStore, create_secret_store


@dataclass
class Container:
    settings: Settings
    database: Database
    secrets: SecretStore
    projects: ProjectRepository
    openapi: OpenApiRepository
    documents: DocumentRepository
    generations: IndexGenerationRepository
    runs: RunRepository
    operation_results: OperationResultRepository
    mappings: MappingRepository
    chunks: ChunkRepository
    retrieval: HybridRetrievalStore
    indexer: DocumentIndexer
    knowledge: KnowledgeBase
    otel: OtelStatementStore


class EmbedderCache:
    def __init__(self) -> None:
        self._embedders: dict[tuple[str, str, int], Embedder] = {}
        self._lock = threading.Lock()

    def __call__(self, model: ModelProfile) -> Embedder:
        key = (model.base_url, model.embedding_model, model.embedding_dimension)
        with self._lock:
            if key not in self._embedders:
                self._embedders[key] = OllamaEmbedder(model.base_url, model.embedding_model, model.embedding_dimension, model.request_timeout_seconds)
            return self._embedders[key]


def build_container(settings: Settings) -> Container:
    settings.ensure_directories()
    database = Database(settings.database_path)
    projects = ProjectRepository(database)
    documents = DocumentRepository(database)
    generations = IndexGenerationRepository(database)
    chunks = ChunkRepository(database)
    retrieval = HybridRetrievalStore(settings.vector_dir, chunks)
    embedders = EmbedderCache()

    def tokenizer_for(model: ModelProfile, embedder: Embedder) -> TokenCounter:
        return load_tokenizer(model.base_url, embedder.model, embedder.digest, settings.data_dir / "tokenizers")

    indexer = DocumentIndexer(settings.uploads_dir, documents, generations, retrieval, embedders, tokenizer_for, projects.get, settings.max_upload_bytes)
    return Container(
        settings=settings,
        database=database,
        secrets=create_secret_store(settings.secret_backend, settings.secrets_dir),
        projects=projects,
        openapi=OpenApiRepository(database),
        documents=documents,
        generations=generations,
        runs=RunRepository(database),
        operation_results=OperationResultRepository(database),
        mappings=MappingRepository(database),
        chunks=chunks,
        retrieval=retrieval,
        indexer=indexer,
        knowledge=KnowledgeBase(documents, generations, retrieval, chunks, embedders),
        otel=OtelStatementStore(database),
    )


@lru_cache
def get_container() -> Container:
    return build_container(get_settings())
