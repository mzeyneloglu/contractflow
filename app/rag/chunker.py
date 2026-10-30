import hashlib
import re
from dataclasses import dataclass, field

from app.core.models.documents import DocumentChunk, SourceLocation
from app.rag.loaders import Block
from app.rag.tokenizer import TokenCounter

CHUNKER_VERSION = "section-aware-v1"
SENTENCE_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\n+")


@dataclass(frozen=True)
class ChunkingPolicy:
    target_tokens: int = 550
    max_tokens: int = 700
    min_tokens: int = 400
    overlap_tokens: int = 60


@dataclass
class Section:
    path: list[str]
    blocks: list[Block] = field(default_factory=list)


def short_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


def group_sections(blocks: list[Block]) -> list[Section]:
    sections: list[Section] = []
    for block in blocks:
        if sections and sections[-1].path == block.section_path:
            sections[-1].blocks.append(block)
        else:
            sections.append(Section(block.section_path, [block]))
    return sections


def location_of(blocks: list[Block]) -> SourceLocation:
    pages = [block.page for block in blocks if block.page is not None]
    starts = [block.line_start for block in blocks if block.line_start is not None]
    ends = [block.line_end for block in blocks if block.line_end is not None]
    return SourceLocation(
        page=pages[0] if pages else None,
        line_start=min(starts) if starts else None,
        line_end=max(ends) if ends else None,
        section_path=list(blocks[0].section_path) if blocks else [],
    )


class SectionChunker:
    def __init__(self, counter: TokenCounter, policy: ChunkingPolicy = ChunkingPolicy()):
        self.counter = counter
        self.policy = policy

    def chunk(self, project_id: str, document_id: str, filename: str, blocks: list[Block]) -> list[DocumentChunk]:
        chunks: list[DocumentChunk] = []
        for index, section in enumerate(group_sections(blocks)):
            parent_id = f"sec_{short_hash(document_id, str(index), ' > '.join(section.path))}"
            for pieces in self._pack(self._split_oversized(section.blocks)):
                text = self._render(section.path, pieces)
                chunks.append(
                    DocumentChunk(
                        chunk_id=f"chk_{short_hash(document_id, parent_id, str(len(chunks)), text)}",
                        parent_id=parent_id,
                        project_id=project_id,
                        document_id=document_id,
                        source_filename=filename,
                        text=text,
                        location=location_of(pieces),
                        token_count=self.counter.count(text),
                        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        chunker_version=CHUNKER_VERSION,
                    )
                )
        return chunks

    def _render(self, path: list[str], blocks: list[Block]) -> str:
        body = "\n\n".join(block.text for block in blocks)
        return f"[{' > '.join(path)}]\n{body}" if path else body

    def _split_oversized(self, blocks: list[Block]) -> list[Block]:
        result: list[Block] = []
        for block in blocks:
            if self.counter.count(block.text) <= self.policy.max_tokens:
                result.append(block)
                continue
            current: list[str] = []
            for sentence in (part for part in SENTENCE_SPLIT.split(block.text) if part.strip()):
                candidate = " ".join(current + [sentence])
                if current and self.counter.count(candidate) > self.policy.target_tokens:
                    result.append(Block(" ".join(current), block.kind, block.section_path, block.page, block.line_start, block.line_end))
                    current = []
                current.append(sentence)
            if current:
                result.append(Block(" ".join(current), block.kind, block.section_path, block.page, block.line_start, block.line_end))
        return result

    def _pack(self, blocks: list[Block]) -> list[list[Block]]:
        groups: list[list[Block]] = []
        current: list[Block] = []
        for block in blocks:
            candidate = "\n\n".join(item.text for item in current + [block])
            if current and self.counter.count(candidate) > self.policy.target_tokens:
                groups.append(current)
                current = self._overlap(current)
            current.append(block)
        if current:
            groups.append(current)
        return groups

    def _overlap(self, previous: list[Block]) -> list[Block]:
        last = previous[-1]
        sentences = [part for part in SENTENCE_SPLIT.split(last.text) if part.strip()]
        tail: list[str] = []
        for sentence in reversed(sentences):
            if self.counter.count(" ".join([sentence] + tail)) > self.policy.overlap_tokens:
                break
            tail.insert(0, sentence)
        if not tail:
            return []
        return [Block(" ".join(tail), last.kind, last.section_path, last.page, last.line_start, last.line_end)]
