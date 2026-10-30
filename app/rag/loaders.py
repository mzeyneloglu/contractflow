import re
from dataclasses import dataclass, field
from enum import StrEnum
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MIN_TEXT_CHARS_PER_PAGE = 20
MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.{0,90})$")
LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")


class BlockKind(StrEnum):
    PARAGRAPH = "paragraph"
    LIST = "list"
    CODE = "code"
    TABLE = "table"


@dataclass
class Block:
    text: str
    kind: BlockKind
    section_path: list[str] = field(default_factory=list)
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass
class LoadedDocument:
    blocks: list[Block]
    needs_ocr: bool = False
    warnings: list[str] = field(default_factory=list)


class DocumentLoadError(Exception):
    pass


def line_kind(line: str) -> BlockKind:
    if line.lstrip().startswith("|"):
        return BlockKind.TABLE
    if LIST_ITEM.match(line):
        return BlockKind.LIST
    return BlockKind.PARAGRAPH


class MarkdownLoader:
    def load(self, content: bytes) -> LoadedDocument:
        lines = content.decode("utf-8-sig", errors="replace").splitlines()
        blocks: list[Block] = []
        headings: list[tuple[int, str]] = []
        buffer: list[str] = []
        buffer_kind = BlockKind.PARAGRAPH
        buffer_start = 0
        in_code = False

        def flush(end_line: int) -> None:
            nonlocal buffer
            text = "\n".join(buffer).strip()
            if text:
                blocks.append(Block(text, buffer_kind, [title for _, title in headings], line_start=buffer_start, line_end=end_line))
            buffer = []

        for number, line in enumerate(lines, start=1):
            if line.strip().startswith("```"):
                if in_code:
                    buffer.append(line)
                    flush(number)
                    in_code = False
                else:
                    flush(number - 1)
                    in_code, buffer_kind, buffer_start, buffer = True, BlockKind.CODE, number, [line]
                continue
            if in_code:
                buffer.append(line)
                continue
            heading = MARKDOWN_HEADING.match(line)
            if heading:
                flush(number - 1)
                level = len(heading.group(1))
                headings = [(existing_level, title) for existing_level, title in headings if existing_level < level] + [(level, heading.group(2).strip())]
                continue
            if not line.strip():
                flush(number - 1)
                continue
            kind = line_kind(line)
            continuation = buffer_kind is BlockKind.LIST and line.startswith((" ", "\t"))
            if buffer and kind is not buffer_kind and not continuation:
                flush(number - 1)
            if not buffer:
                buffer_kind, buffer_start = kind, number
            buffer.append(line)
        flush(len(lines))
        return LoadedDocument(blocks=blocks)


class PdfLoader:
    def load(self, content: bytes) -> LoadedDocument:
        try:
            reader = PdfReader(BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
        except (PdfReadError, ValueError, KeyError) as error:
            raise DocumentLoadError(f"PDF okunamadı: {error}") from error
        if not pages:
            return LoadedDocument(blocks=[], needs_ocr=True, warnings=["PDF sayfa içermiyor"])
        empty_pages = [index + 1 for index, text in enumerate(pages) if len(text.strip()) < MIN_TEXT_CHARS_PER_PAGE]
        if len(empty_pages) == len(pages):
            return LoadedDocument(blocks=[], needs_ocr=True, warnings=["Metin çıkarılamadı; taranmış PDF olabilir"])
        blocks: list[Block] = []
        headings: list[tuple[int, str]] = []
        for page_number, text in enumerate(pages, start=1):
            paragraph: list[str] = []

            def flush() -> None:
                joined = " ".join(part.strip() for part in paragraph).strip()
                if joined:
                    kind = BlockKind.LIST if LIST_ITEM.match(paragraph[0]) else BlockKind.PARAGRAPH
                    blocks.append(Block(joined, kind, [title for _, title in headings], page=page_number))
                paragraph.clear()

            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    flush()
                    continue
                level = self._heading_level(stripped)
                if level:
                    flush()
                    headings = [(existing, title) for existing, title in headings if existing < level] + [(level, stripped)]
                    continue
                if LIST_ITEM.match(stripped) and paragraph:
                    flush()
                paragraph.append(stripped)
            flush()
        warnings = [f"Metin içermeyen sayfalar: {', '.join(map(str, empty_pages))}"] if empty_pages else []
        return LoadedDocument(blocks=blocks, warnings=warnings)

    def _heading_level(self, line: str) -> int:
        numbered = NUMBERED_HEADING.match(line)
        if numbered and not line.endswith((".", ",", ";", ":")) and len(line) <= 90:
            return numbered.group(1).count(".") + 1
        letters = [character for character in line if character.isalpha()]
        if 3 <= len(line) <= 70 and letters and all(character.isupper() for character in letters) and not line.endswith("."):
            return 1
        return 0


def loader_for(filename: str):
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        return PdfLoader()
    if lowered.endswith((".md", ".markdown")):
        return MarkdownLoader()
    raise DocumentLoadError("Yalnızca PDF ve Markdown dosyaları desteklenir")
