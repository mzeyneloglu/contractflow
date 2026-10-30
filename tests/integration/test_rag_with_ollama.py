import json
from pathlib import Path

import pytest

from app.config import Settings
from app.core.models.documents import DocumentStatus
from app.core.models.project import ProjectProfile
from app.rag.embeddings import installed_models
from app.services.container import build_container
from tests.evaluation.retrieval_cases import RETRIEVAL_CASES
from tests.support.pdf import make_blank_pdf, make_text_pdf

CORPUS = Path(__file__).resolve().parent.parent / "evaluation" / "corpus"
REPORT_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "evaluation_results" / "retrieval.json"
TOP_K = 4


def ollama_ready() -> bool:
    try:
        return "qwen3-embedding:0.6b" in installed_models("http://127.0.0.1:11434", timeout=3)
    except Exception:
        return False


pytestmark = [pytest.mark.ollama, pytest.mark.skipif(not ollama_ready(), reason="BLOCKED: yerel Ollama veya qwen3-embedding:0.6b yok")]


@pytest.fixture
def container(tmp_path):
    return build_container(Settings(data_dir=tmp_path, secret_backend="file"))


def index_corpus(container, project: ProjectProfile) -> None:
    futures = []
    for path in sorted(CORPUS.glob("*.md")):
        record = container.indexer.submit(project, path.name, path.read_bytes())
        futures.append(record.id)
    container.indexer.executor.submit(lambda: None).result(timeout=600)
    for document_id in futures:
        document = container.documents.get(document_id)
        assert document.status is DocumentStatus.READY, document.status_message


def test_real_embedding_retrieval_evaluation(container):
    project = container.projects.save(ProjectProfile(name="rag-eval"))
    index_corpus(container, project)
    snapshot = container.knowledge.snapshot(project.id)
    results = []
    for case in RETRIEVAL_CASES:
        hits = container.knowledge.search(snapshot, project.model, case.question, top_k=TOP_K)
        rank = next((index for index, hit in enumerate(hits, start=1) if case.required_text in hit.chunk.text and hit.chunk.source_filename == case.required_file), None)
        results.append({"question": case.question, "required": case.required_text, "rank": rank, "top": [hit.reference().label() for hit in hits]})
    hit_rate = sum(result["rank"] is not None for result in results) / len(results)
    mrr = sum(1 / result["rank"] for result in results if result["rank"]) / len(results)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps({"top_k": TOP_K, "hit_rate": hit_rate, "mrr": round(mrr, 3), "cases": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    assert len(RETRIEVAL_CASES) >= 10
    assert hit_rate >= 0.85, results


def test_chunks_record_real_token_counts_and_embedding_metadata(container):
    project = container.projects.save(ProjectProfile(name="rag-meta"))
    index_corpus(container, project)
    snapshot = container.knowledge.snapshot(project.id)
    chunks = container.chunks.for_documents(snapshot.generation, snapshot.document_ids)
    assert chunks and all(chunk.embedding_model == "qwen3-embedding:0.6b" and chunk.embedding_digest for chunk in chunks)
    assert all(0 < chunk.token_count <= 700 for chunk in chunks)
    section = container.knowledge.get_section(snapshot, chunks[0].parent_id)
    assert section and section[0].parent_id == chunks[0].parent_id


def test_document_update_supersedes_old_version_and_projects_are_isolated(container):
    first = container.projects.save(ProjectProfile(name="a"))
    second = container.projects.save(ProjectProfile(name="b"))
    container.indexer.submit(first, "kural.md", "# Kural\nEski kural: KIRMIZI_DEGER geçerlidir.".encode())
    container.indexer.submit(second, "diger.md", "# Diğer\nBaşka projeye ait MAVI_DEGER kuralı.".encode())
    container.indexer.executor.submit(lambda: None).result(timeout=300)
    container.indexer.submit(first, "kural.md", "# Kural\nYeni kural: YESIL_DEGER geçerlidir.".encode())
    container.indexer.executor.submit(lambda: None).result(timeout=300)
    snapshot = container.knowledge.snapshot(first.id)
    texts = " ".join(hit.chunk.text for hit in container.knowledge.search(snapshot, first.model, "kural değer", top_k=10))
    assert "YESIL_DEGER" in texts and "KIRMIZI_DEGER" not in texts and "MAVI_DEGER" not in texts
    statuses = sorted(document.status for document in container.documents.list_for_project(first.id, include_superseded=True))
    assert statuses == sorted([DocumentStatus.READY, DocumentStatus.SUPERSEDED])


def test_pdf_with_text_is_indexed_and_scanned_pdf_needs_ocr(container):
    project = container.projects.save(ProjectProfile(name="pdf"))
    text_pdf = container.indexer.submit(project, "musteri.pdf", make_text_pdf([["1. Aktif Musteri", "ACTIVE kolonu 1 olan musteriler aktiftir."]]))
    scanned = container.indexer.submit(project, "taranmis.pdf", make_blank_pdf())
    container.indexer.executor.submit(lambda: None).result(timeout=300)
    assert container.documents.get(text_pdf.id).status is DocumentStatus.READY
    assert container.documents.get(scanned.id).status is DocumentStatus.NEEDS_OCR
    snapshot = container.knowledge.snapshot(project.id)
    hit = container.knowledge.search(snapshot, project.model, "aktif müşteri kuralı", top_k=1)[0]
    assert hit.chunk.location.page == 1
