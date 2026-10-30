import re
from collections.abc import Iterable

from rank_bm25 import BM25Plus

TOKEN_PATTERN = re.compile(r"[0-9a-zçğıöşü_]+", re.IGNORECASE)
CAMEL_BOUNDARY = re.compile(r"(?<=[a-zçğıöşü0-9])(?=[A-ZÇĞİÖŞÜ])")
SYNONYM_GROUPS = [
    {"aktif", "active", "etkin", "geçerli", "enabled"},
    {"pasif", "inactive", "passive", "disabled"},
    {"silinmiş", "silinen", "silindi", "deleted", "silme", "soft", "delete"},
    {"adet", "sayı", "sayısı", "count", "toplam", "total", "miktar"},
    {"sayfa", "sayfalama", "page", "pagination", "pageable"},
    {"kayıt", "kayıtlar", "record", "records", "satır", "row"},
    {"istisna", "hariç", "exception", "dışında", "except"},
    {"kural", "kuralı", "rule", "politika", "policy"},
]
SYNONYMS = {word: group for group in SYNONYM_GROUPS for word in group}
SUFFIXES = ("ları", "leri", "ların", "lerin", "lar", "ler", "nın", "nin", "nun", "nün", "ın", "in", "un", "ün", "ı", "i", "u", "ü", "da", "de", "ta", "te")


def turkish_lower(text: str) -> str:
    return text.replace("I", "ı").replace("İ", "i").lower()


def stem(token: str) -> str:
    for suffix in SUFFIXES:
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def lower_variants(raw: str) -> list[str]:
    return list(dict.fromkeys([raw.lower(), turkish_lower(raw)]))


def word_variants(raw: str) -> list[str]:
    variants: list[str] = []
    for lowered in lower_variants(raw):
        variants.append(lowered)
        stemmed = stem(lowered)
        if stemmed != lowered:
            variants.append(stemmed)
        if "_" in lowered:
            variants.extend(part for part in lowered.split("_") if part)
    camel_parts = CAMEL_BOUNDARY.sub(" ", raw).split()
    if len(camel_parts) > 1:
        variants.extend(variant for part in camel_parts for variant in lower_variants(part))
    return list(dict.fromkeys(variants))


def tokenize(text: str) -> list[str]:
    return [variant for raw in TOKEN_PATTERN.findall(text) for variant in word_variants(raw)]


def expand_query(tokens: Iterable[str]) -> list[str]:
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        expanded.extend(sorted(SYNONYMS.get(token, set()) - {token}))
    return expanded


class LexicalIndex:
    def __init__(self, ids: list[str], texts: list[str]):
        self.ids = ids
        self.documents = [tokenize(text) for text in texts]
        self.index = BM25Plus(self.documents) if texts else None

    def search(self, query: str, top_k: int) -> list[tuple[str, float]]:
        if self.index is None:
            return []
        terms = expand_query(tokenize(query))
        scores = self.index.get_scores(terms)
        query_terms = set(terms)
        matching = [(chunk_id, float(score)) for chunk_id, score, tokens in zip(self.ids, scores, self.documents, strict=True) if query_terms & set(tokens)]
        return sorted(matching, key=lambda pair: pair[1], reverse=True)[:top_k]


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
