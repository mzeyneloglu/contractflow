import hashlib
import logging
import re
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.documents import DocumentRecord, DocumentStatus
from app.core.models.project import ModelProfile, ProjectProfile
from app.persistence.repositories import DocumentRepository, IndexGenerationRepository
from app.rag.chunker import SectionChunker
from app.rag.embeddings import Embedder
from app.rag.loaders import DocumentLoadError, loader_for
from app.rag.store import HybridRetrievalStore
from app.rag.tokenizer import TokenCounter

logger = logging.getLogger(__name__)
ALLOWED_MEDIA = {".pdf": "application/pdf", ".md": "text/markdown", ".markdown": "text/markdown"}

EmbedderFactory = Callable[[ModelProfile], Embedder]
TokenizerFactory = Callable[[ModelProfile, Embedder], TokenCounter]
ProjectLookup = Callable[[str], ProjectProfile | None]


def safe_filename(filename: str) -> str:
    name = Path(filename).name
    cleaned = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._")
    return cleaned[:120] or "belge"


def media_type_for(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_MEDIA:
        raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"Desteklenmeyen dosya türü: {suffix or 'yok'} (yalnızca PDF ve Markdown)")
    return ALLOWED_MEDIA[suffix]


class DocumentIndexer:
    def __init__(
        self,
        uploads_dir: Path,
        documents: DocumentRepository,
        generations: IndexGenerationRepository,
        store: HybridRetrievalStore,
        embedder_factory: EmbedderFactory,
        tokenizer_factory: TokenizerFactory,
        project_lookup: ProjectLookup,
        max_upload_bytes: int,
    ):
        self.uploads_dir = uploads_dir
        self.documents = documents
        self.generations = generations
        self.store = store
        self.embedder_factory = embedder_factory
        self.tokenizer_factory = tokenizer_factory
        self.project_lookup = project_lookup
        self.max_upload_bytes = max_upload_bytes
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="indexer")

    def submit(self, project: ProjectProfile, filename: str, content: bytes) -> DocumentRecord:
        media_type = media_type_for(filename)
        if len(content) > self.max_upload_bytes:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"Dosya çok büyük: {filename}")
        content_hash = hashlib.sha256(content).hexdigest()
        display_name = Path(filename).name
        for existing in self.documents.find_by_filename(project.id, display_name):
            if existing.content_hash == content_hash and existing.status in {DocumentStatus.READY, DocumentStatus.QUEUED, DocumentStatus.PARSING, DocumentStatus.EMBEDDING}:
                return existing
        record = DocumentRecord(project_id=project.id, filename=display_name, stored_path="", media_type=media_type, content_hash=content_hash)
        project_dir = self.uploads_dir / safe_filename(project.id)
        project_dir.mkdir(parents=True, exist_ok=True)
        stored = project_dir / f"{record.id}__{safe_filename(display_name)}"
        stored.write_bytes(content)
        record.stored_path = str(stored)
        self.documents.save(record)
        self.schedule(record.id)
        return record

    def schedule(self, document_id: str) -> Future:
        return self.executor.submit(self._process_safely, document_id)

    def resume_unfinished(self) -> int:
        unfinished = self.documents.list_unfinished()
        for document in unfinished:
            self._set_status(document, DocumentStatus.QUEUED, "Uygulama yeniden başladı; indeksleme tekrar kuyruğa alındı")
            self.schedule(document.id)
        return len(unfinished)

    def _set_status(self, document: DocumentRecord, status: DocumentStatus, message: str = "") -> None:
        document.status = status
        document.status_message = message
        self.documents.save(document)

    def _process_safely(self, document_id: str) -> None:
        document = self.documents.get(document_id)
        if document is None:
            return
        try:
            self._process(document)
        except ContractFlowError as error:
            self._set_status(document, DocumentStatus.FAILED, error.message)
        except Exception as error:
            logger.exception("Belge indekslenemedi: %s", document_id)
            self._set_status(document, DocumentStatus.FAILED, f"Beklenmeyen hata: {type(error).__name__}")

    def _process(self, document: DocumentRecord) -> None:
        project = self.project_lookup(document.project_id)
        if project is None:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Proje bulunamadı")
        self._set_status(document, DocumentStatus.PARSING)
        try:
            loaded = loader_for(document.filename).load(Path(document.stored_path).read_bytes())
        except DocumentLoadError as error:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, str(error)) from error
        if loaded.needs_ocr:
            self._set_status(document, DocumentStatus.NEEDS_OCR, "; ".join(loaded.warnings) + ". Metin katmanlı PDF yükleyin veya harici OCR uygulayın")
            return
        embedder = self.embedder_factory(project.model)
        generation = self.ensure_generation(project, embedder)
        chunks = SectionChunker(self.tokenizer_factory(project.model, embedder)).chunk(project.id, document.id, document.filename, loaded.blocks)
        if not chunks:
            self._set_status(document, DocumentStatus.FAILED, "Belgede indekslenecek metin bulunamadı")
            return
        self._set_status(document, DocumentStatus.EMBEDDING, f"{len(chunks)} parça embed ediliyor")
        vectors = embedder.embed_documents([chunk.text for chunk in chunks])
        for chunk in chunks:
            chunk.embedding_model, chunk.embedding_digest = embedder.model, embedder.digest
        self.store.index_document(generation, document.id, chunks, vectors)
        document.chunk_count = len(chunks)
        document.index_generation = generation
        warnings = "; ".join(loaded.warnings)
        self._set_status(document, DocumentStatus.READY, warnings)
        self._supersede_previous_versions(document)

    def _supersede_previous_versions(self, document: DocumentRecord) -> None:
        for previous in self.documents.find_by_filename(document.project_id, document.filename):
            if previous.id != document.id and previous.created_at <= document.created_at:
                self._set_status(previous, DocumentStatus.SUPERSEDED, f"Yeni sürümle değiştirildi: {document.id}")

    def ensure_generation(self, project: ProjectProfile, embedder: Embedder) -> str:
        existing = self.generations.find(project.id, embedder.model, embedder.digest, embedder.dimension)
        active = self.generations.active(project.id)
        if existing and active and existing["generation"] == active["generation"]:
            return existing["generation"]
        generation = existing["generation"] if existing else self.generations.create(project.id, embedder.model, embedder.digest, embedder.dimension)
        if active is None:
            self.generations.activate(project.id, generation)
            return generation
        self._reindex_into(project, embedder, generation)
        self.generations.activate(project.id, generation)
        return generation

    def _reindex_into(self, project: ProjectProfile, embedder: Embedder, generation: str) -> None:
        counter = self.tokenizer_factory(project.model, embedder)
        for document in self.documents.list_for_project(project.id):
            if document.status is not DocumentStatus.READY:
                continue
            loaded = loader_for(document.filename).load(Path(document.stored_path).read_bytes())
            chunks = SectionChunker(counter).chunk(project.id, document.id, document.filename, loaded.blocks)
            vectors = embedder.embed_documents([chunk.text for chunk in chunks])
            for chunk in chunks:
                chunk.embedding_model, chunk.embedding_digest = embedder.model, embedder.digest
            self.store.index_document(generation, document.id, chunks, vectors)
            document.index_generation = generation
            self.documents.save(document)
