import importlib
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.rag.embeddings import installed_models
from app.secrets.store import keyring_available

REQUIRED_MODULES = ["fastapi", "uvicorn", "jinja2", "httpx", "pydantic", "langgraph", "langchain_ollama", "chromadb", "rank_bm25", "sqlglot", "oracledb", "mcp", "pypdf", "keyring", "cryptography", "tokenizers"]


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'OK' if ok else 'EKSİK'}] {label}{' — ' + detail if detail else ''}")
    return ok


def main() -> int:
    settings = get_settings()
    results = [check("Python", sys.version_info >= (3, 12), f"{platform.python_version()} ({platform.machine()})")]
    for module in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
            results.append(check(f"Paket {module}", True))
        except ImportError as error:
            results.append(check(f"Paket {module}", False, f"{error}. 'pip install -r requirements.lock' çalıştırın"))
    try:
        models = installed_models(settings.ollama_url, timeout=5)
        results.append(check("Ollama", True, settings.ollama_url))
        for model in (settings.decision_model, settings.embedding_model):
            results.append(check(f"Model {model}", model in models, "" if model in models else f"'ollama pull {model}' çalıştırın"))
    except Exception as error:
        results.append(check("Ollama", False, f"{error}"))
    results.append(check("Gizli bilgi deposu", True, "OS keyring" if keyring_available() else "şifreli dosya (keyring yok)"))
    results.append(check("Veri dizini", settings.data_dir.exists(), str(settings.data_dir)))
    results.append(check("Bind adresi", settings.host in {"127.0.0.1", "localhost", "::1"}, settings.host))
    print("\nSonuç:", "hazır" if all(results) else "eksikler var")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
