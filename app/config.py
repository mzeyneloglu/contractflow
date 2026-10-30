from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTRACTFLOW_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = PROJECT_ROOT / "data"
    prompts_dir: Path = PROJECT_ROOT / "prompts"
    ollama_url: str = "http://127.0.0.1:11434"
    decision_model: str = "gemma3:4b"
    embedding_model: str = "qwen3-embedding:0.6b"
    secret_backend: str = "auto"
    oracle_mcp_module: str = "oracle_mcp.server"
    query_timeout_seconds: int = 10
    metadata_timeout_seconds: int = 60
    query_max_rows: int = 500
    query_max_bytes: int = 2_000_000
    max_upload_bytes: int = 50_000_000

    @property
    def database_path(self) -> Path:
        return self.data_dir / "contractflow.sqlite3"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def vector_dir(self) -> Path:
        return self.data_dir / "vectors"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.uploads_dir, self.vector_dir, self.secrets_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
