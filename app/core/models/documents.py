from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.models.common import new_id, utc_now


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"
    NEEDS_OCR = "needs_ocr"
    SUPERSEDED = "superseded"


class DocumentRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("doc"))
    project_id: str
    filename: str
    stored_path: str
    media_type: str
    content_hash: str
    status: DocumentStatus = DocumentStatus.QUEUED
    status_message: str = ""
    chunk_count: int = 0
    index_generation: str = ""
    version: str | None = None
    effective_date: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SourceLocation(BaseModel):
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    section_path: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        parts: list[str] = []
        if self.section_path:
            parts.append(" > ".join(self.section_path))
        if self.page is not None:
            parts.append(f"sayfa {self.page}")
        if self.line_start is not None:
            parts.append(f"satır {self.line_start}-{self.line_end or self.line_start}")
        return ", ".join(parts)


class DocumentChunk(BaseModel):
    chunk_id: str
    parent_id: str
    project_id: str
    document_id: str
    source_filename: str
    text: str
    location: SourceLocation
    token_count: int
    content_hash: str
    chunker_version: str
    embedding_model: str = ""
    embedding_digest: str = ""
    ingested_at: datetime = Field(default_factory=utc_now)


class SourceReference(BaseModel):
    document_id: str
    chunk_id: str
    source_filename: str
    location: SourceLocation
    excerpt: str = ""

    def label(self) -> str:
        where = self.location.describe()
        return f"{self.source_filename} ({where})" if where else self.source_filename
