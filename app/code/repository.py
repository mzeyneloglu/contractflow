import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_SUFFIXES = {".java", ".kt", ".xml", ".sql"}
IGNORED_DIRECTORIES = {".git", ".idea", ".vscode", "target", "build", "out", "node_modules", ".gradle", "bin", "generated", "test", "tests"}
MAX_FILE_BYTES = 1_500_000
MAX_FILES = 20_000


@dataclass
class SourceFile:
    path: Path
    relative: str
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class CodeIndex:
    roots: list[Path]
    files: list[SourceFile] = field(default_factory=list)
    signature: tuple = ()

    def by_suffix(self, *suffixes: str) -> list[SourceFile]:
        return [source for source in self.files if source.path.suffix in suffixes]

    def search(self, pattern: re.Pattern, suffixes: tuple[str, ...] = (".java", ".kt", ".xml", ".sql"), limit: int = 200) -> list[tuple[SourceFile, int, str]]:
        hits: list[tuple[SourceFile, int, str]] = []
        for source in self.files:
            if source.path.suffix not in suffixes:
                continue
            for number, line in enumerate(source.lines, start=1):
                if pattern.search(line):
                    hits.append((source, number, line.strip()))
                    if len(hits) >= limit:
                        return hits
        return hits


def is_ignored(directory: str) -> bool:
    return directory.lower() in IGNORED_DIRECTORIES or directory.startswith(".")


def scan(roots: list[Path]) -> list[SourceFile]:
    files: list[SourceFile] = []
    for root in roots:
        for current, directories, names in os.walk(root):
            directories[:] = [directory for directory in directories if not is_ignored(directory)]
            for name in names:
                path = Path(current) / name
                if path.suffix not in SOURCE_SUFFIXES or path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                files.append(SourceFile(path, f"{root.name}/{path.relative_to(root).as_posix()}", text.splitlines()))
                if len(files) >= MAX_FILES:
                    return files
    return files


def tree_signature(roots: list[Path]) -> tuple:
    latest = 0.0
    count = 0
    for root in roots:
        for current, directories, names in os.walk(root):
            directories[:] = [directory for directory in directories if not is_ignored(directory)]
            for name in names:
                if Path(name).suffix in SOURCE_SUFFIXES:
                    count += 1
                    latest = max(latest, (Path(current) / name).stat().st_mtime)
    return tuple(str(root) for root in roots), count, latest


_cache: dict[tuple[str, ...], CodeIndex] = {}
_lock = threading.Lock()


def load_index(paths: list[str]) -> CodeIndex:
    roots = [Path(path).resolve() for path in paths if Path(path).is_dir()]
    key = tuple(str(root) for root in roots)
    signature = tree_signature(roots)
    with _lock:
        cached = _cache.get(key)
        if cached and cached.signature == signature:
            return cached
        index = CodeIndex(roots=roots, files=scan(roots), signature=signature)
        _cache[key] = index
        return index
