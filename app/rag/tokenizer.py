import threading
from pathlib import Path
from typing import Protocol

import httpx
from tokenizers import Regex, Tokenizer, models, pre_tokenizers

QWEN2_PRETOKENIZE_PATTERN = (
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
)


class TokenCounter(Protocol):
    name: str

    def count(self, text: str) -> int: ...


class OllamaVocabularyTokenizer:
    def __init__(self, tokenizer: Tokenizer, model: str, digest: str, adds_eos: bool):
        self.tokenizer = tokenizer
        self.name = f"{model}@{digest[:12]}"
        self.adds_eos = adds_eos

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids) + (1 if self.adds_eos else 0)


_cache: dict[str, OllamaVocabularyTokenizer] = {}
_lock = threading.Lock()


def build_tokenizer(tokens: list[str], merges: list[str], pretokenizer: str) -> Tokenizer:
    if pretokenizer != "qwen2":
        raise ValueError(f"Desteklenmeyen tokenizer ön işleyicisi: {pretokenizer}")
    vocabulary = {token: index for index, token in enumerate(tokens)}
    pairs = [tuple(merge.split(" ", 1)) for merge in merges]
    tokenizer = Tokenizer(models.BPE(vocab=vocabulary, merges=pairs))
    tokenizer.pre_tokenizer = pre_tokenizers.Sequence(
        [
            pre_tokenizers.Split(Regex(QWEN2_PRETOKENIZE_PATTERN), behavior="isolated", invert=False),
            pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=False),
        ]
    )
    return tokenizer


def load_tokenizer(base_url: str, model: str, digest: str, cache_dir: Path) -> OllamaVocabularyTokenizer:
    key = f"{model}@{digest}"
    with _lock:
        if key in _cache:
            return _cache[key]
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{digest[:24]}.json"
        adds_eos_file = cache_dir / f"{digest[:24]}.eos"
        if cache_file.exists() and adds_eos_file.exists():
            tokenizer = Tokenizer.from_file(str(cache_file))
            adds_eos = adds_eos_file.read_text() == "1"
        else:
            info = httpx.post(f"{base_url}/api/show", json={"model": model, "verbose": True}, timeout=120).json()["model_info"]
            tokenizer = build_tokenizer(info["tokenizer.ggml.tokens"], info["tokenizer.ggml.merges"], info.get("tokenizer.ggml.pre", ""))
            adds_eos = bool(info.get("tokenizer.ggml.add_eos_token", False))
            tokenizer.save(str(cache_file))
            adds_eos_file.write_text("1" if adds_eos else "0")
        _cache[key] = OllamaVocabularyTokenizer(tokenizer, model, digest, adds_eos)
        return _cache[key]
