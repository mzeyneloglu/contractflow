import hashlib
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

ROLE_FILES = ("common", "planner", "business_rules", "api", "oracle", "analyst")


@lru_cache
def load_prompt(role: str, directory: Path | None = None) -> str:
    base = directory or get_settings().prompts_dir
    return (base / f"{role}.md").read_text(encoding="utf-8")


def system_prompt(role: str) -> str:
    return load_prompt("common") + "\n\n" + load_prompt(role)


@lru_cache
def prompt_version() -> str:
    digest = hashlib.sha256("".join(load_prompt(role) for role in ROLE_FILES).encode("utf-8")).hexdigest()[:10]
    return f"prompts@{digest}"
