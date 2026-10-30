import re
import threading
from dataclasses import dataclass
from pathlib import Path

import chromadb

from app.core.models.documents import DocumentChunk, SourceReference
from app.persistence.database import Database
from app.rag.embeddings import Embedder
from app.rag.lexical import LexicalIndex, reciprocal_rank_fusion

CANDIDATE_MULTIPLIER = 4

CHUNKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (generation, chunk_id)
);
CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS chunks_parent ON chunks(parent_id);
"""


@dataclass
class SearchHit:
    chunk: DocumentChunk
    fused_score: float
    dense_rank: int | None
    lexical_rank: int | None

    def reference(self) -> SourceReference:
        return SourceReference(
            document_id=self.chunk.document_id,
            chunk_id=self.chunk.chunk_id,
            source_filename=self.chunk.source_filename,
            location=self.chunk.location,
            excerpt=self.chunk.text[:400],
        )


def collection_name(project_id: str, generation: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{project_id}_{generation}")[:60]


class ChunkRepository:
    def __init__(self, database: Database):
        self.database = database
        with database.connect() as connection:
            connection.executescript(CHUNKS_SCHEMA)

    def replace_document(self, generation: str, document_id: str, chunks: list[DocumentChunk]) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM chunks WHERE generation = ? AND document_id = ?", (generation, document_id))
            connection.executemany(
                "INSERT INTO chunks (chunk_id, generation, project_id, document_id, parent_id, payload) VALUES (?, ?, ?, ?, ?, ?)",
                [(chunk.chunk_id, generation, chunk.project_id, chunk.document_id, chunk.parent_id, chunk.model_dump_json()) for chunk in chunks],
            )

    def for_documents(self, generation: str, document_ids: list[str]) -> list[DocumentChunk]:
        if not document_ids:
            return []
        placeholders = ",".join("?" for _ in document_ids)
        rows = self.database.fetch_all(f"SELECT payload FROM chunks WHERE generation = ? AND document_id IN ({placeholders})", (generation, *document_ids))
        return [DocumentChunk.model_validate_json(row["payload"]) for row in rows]

    def get(self, generation: str, chunk_id: str) -> DocumentChunk | None:
        row = self.database.fetch_one("SELECT payload FROM chunks WHERE generation = ? AND chunk_id = ?", (generation, chunk_id))
        return DocumentChunk.model_validate_json(row["payload"]) if row else None

    def section(self, generation: str, parent_id: str) -> list[DocumentChunk]:
        rows = self.database.fetch_all("SELECT payload FROM chunks WHERE generation = ? AND parent_id = ? ORDER BY rowid", (generation, parent_id))
        return [DocumentChunk.model_validate_json(row["payload"]) for row in rows]


class HybridRetrievalStore:
    def __init__(self, vector_dir: Path, chunks: ChunkRepository):
        self.client = chromadb.PersistentClient(path=str(vector_dir), settings=chromadb.Settings(anonymized_telemetry=False))
        self.chunks = chunks
        self._lock = threading.Lock()

    def _collection(self, project_id: str, generation: str):
        return self.client.get_or_create_collection(collection_name(project_id, generation), embedding_function=None, metadata={"hnsw:space": "cosine"})

    def index_document(self, generation: str, document_id: str, chunks: list[DocumentChunk], vectors: list[list[float]]) -> None:
        if not chunks:
            self.chunks.replace_document(generation, document_id, [])
            return
        with self._lock:
            collection = self._collection(chunks[0].project_id, generation)
            existing = collection.get(where={"document_id": document_id}, include=[])
            if existing["ids"]:
                collection.delete(ids=existing["ids"])
            collection.upsert(
                ids=[chunk.chunk_id for chunk in chunks],
                embeddings=vectors,
                documents=[chunk.text for chunk in chunks],
                metadatas=[{"document_id": chunk.document_id, "parent_id": chunk.parent_id, "project_id": chunk.project_id} for chunk in chunks],
            )
        self.chunks.replace_document(generation, document_id, chunks)

    def search(self, project_id: str, generation: str, document_ids: list[str], query: str, embedder: Embedder, top_k: int = 6) -> list[SearchHit]:
        candidates = self.chunks.for_documents(generation, document_ids)
        if not candidates:
            return []
        by_id = {chunk.chunk_id: chunk for chunk in candidates}
        width = min(len(candidates), top_k * CANDIDATE_MULTIPLIER)
        dense = self._dense(project_id, generation, document_ids, embedder.embed_query(query), width)
        lexical = [chunk_id for chunk_id, _ in LexicalIndex(list(by_id), [chunk.text for chunk in candidates]).search(query, width)]
        fused = reciprocal_rank_fusion([dense, lexical])
        hits: list[SearchHit] = []
        for chunk_id, score in fused:
            if chunk_id not in by_id:
                continue
            hits.append(
                SearchHit(
                    chunk=by_id[chunk_id],
                    fused_score=score,
                    dense_rank=dense.index(chunk_id) + 1 if chunk_id in dense else None,
                    lexical_rank=lexical.index(chunk_id) + 1 if chunk_id in lexical else None,
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def _dense(self, project_id: str, generation: str, document_ids: list[str], vector: list[float], width: int) -> list[str]:
        collection = self._collection(project_id, generation)
        result = collection.query(query_embeddings=[vector], n_results=width, where={"document_id": {"$in": document_ids}}, include=[])
        return list(result["ids"][0]) if result["ids"] else []
