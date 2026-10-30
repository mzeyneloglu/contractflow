from pathlib import Path

from app.rag.chunker import ChunkingPolicy, SectionChunker
from app.rag.embeddings import format_query
from app.rag.lexical import LexicalIndex, reciprocal_rank_fusion, tokenize
from app.rag.loaders import BlockKind, MarkdownLoader, PdfLoader
from tests.support.pdf import make_blank_pdf, make_text_pdf

CORPUS = Path(__file__).resolve().parent.parent / "evaluation" / "corpus"


class WordCounter:
    name = "whitespace-test-counter"

    def count(self, text: str) -> int:
        return len(text.split())


def test_markdown_keeps_heading_hierarchy_lines_and_code_blocks():
    loaded = MarkdownLoader().load((CORPUS / "siparis_servisi_kurallari.md").read_bytes())
    exception = next(block for block in loaded.blocks if "RESTORED_AT" in block.text)
    assert exception.section_path[-2:] == ["Silinmiş Kayıtlar", "İstisna: Arşivden Geri Alınanlar"]
    assert exception.line_start and exception.line_end >= exception.line_start
    code = next(block for block in loaded.blocks if block.kind is BlockKind.CODE)
    assert code.text.startswith("```json") and code.text.endswith("```")
    assert code.section_path[-1] == "Sayfalama"


def test_markdown_lists_and_tables_are_grouped():
    loaded = MarkdownLoader().load("# A\n- bir\n- iki\n  devam\n\n| a | b |\n| - | - |\n| 1 | 2 |\n".encode())
    kinds = [block.kind for block in loaded.blocks]
    assert kinds == [BlockKind.LIST, BlockKind.TABLE]
    assert "devam" in loaded.blocks[0].text


def test_pdf_pages_and_numbered_headings():
    pdf = make_text_pdf([["1. Genel", "Bu kurallar musteri servisi icindir."], ["2. Aktif Musteri", "ACTIVE kolonu 1 ise musteri aktiftir.", "2.1 Istisna", "Bloklu musteriler haric tutulur."]])
    loaded = PdfLoader().load(pdf)
    active = next(block for block in loaded.blocks if "ACTIVE" in block.text)
    assert active.page == 2 and active.section_path == ["2. Aktif Musteri"]
    exception = next(block for block in loaded.blocks if "Bloklu" in block.text)
    assert exception.section_path == ["2. Aktif Musteri", "2.1 Istisna"]


def test_scanned_pdf_without_text_needs_ocr():
    loaded = PdfLoader().load(make_blank_pdf(2))
    assert loaded.needs_ocr and not loaded.blocks


def test_chunker_respects_sections_and_budget():
    loaded = MarkdownLoader().load((CORPUS / "siparis_servisi_kurallari.md").read_bytes())
    chunks = SectionChunker(WordCounter(), ChunkingPolicy(target_tokens=40, max_tokens=60, min_tokens=20, overlap_tokens=10)).chunk("p", "d", "f.md", loaded.blocks)
    assert all(chunk.token_count <= 75 for chunk in chunks)
    assert len({chunk.parent_id for chunk in chunks}) == len({tuple(chunk.location.section_path) for chunk in chunks})
    assert all(chunk.text.startswith("[") for chunk in chunks if chunk.location.section_path)
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)


def test_long_paragraph_is_split_with_overlap():
    long_text = " ".join(f"Cümle {index} burada bitiyor." for index in range(60))
    loaded = MarkdownLoader().load(f"# Uzun\n{long_text}\n".encode())
    chunks = SectionChunker(WordCounter(), ChunkingPolicy(target_tokens=50, max_tokens=60, min_tokens=20, overlap_tokens=8)).chunk("p", "d", "f.md", loaded.blocks)
    assert len(chunks) > 2
    assert len({chunk.parent_id for chunk in chunks}) == 1


def test_query_formatter_prevents_double_prefix():
    once = format_query("aktif kayıt")
    assert once.startswith("Instruct: ") and once.endswith("\nQuery: aktif kayıt")
    assert format_query(once) == once


def test_tokenize_handles_camel_case_turkish_and_underscores():
    tokens = tokenize("midtermId DELETED_AT İptal Işık")
    assert {"midtermid", "midterm", "id", "deleted_at", "deleted", "at", "iptal", "ışık"} <= set(tokens)


def test_lexical_search_uses_synonyms():
    index = LexicalIndex(["a", "b"], ["Silinmiş kayıtlar sayılmaz", "Sayfalama kuralı"])
    assert index.search("deleted records", 2)[0][0] == "a"


def test_reciprocal_rank_fusion_rewards_agreement():
    fused = reciprocal_rank_fusion([["x", "y", "z"], ["y", "x", "w"]])
    assert [item for item, _ in fused][:2] in (["x", "y"], ["y", "x"])
    assert dict(fused)["x"] > dict(fused)["z"]
