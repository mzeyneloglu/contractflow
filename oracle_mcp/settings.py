import os
from dataclasses import dataclass
from pathlib import Path

from app.core.models.project import SecretReference
from app.secrets.store import create_secret_store

ENV_PREFIX = "CONTRACTFLOW_ORACLE_"


@dataclass(frozen=True)
class OracleServerSettings:
    host: str
    port: int
    service_name: str
    username: str
    secret_key: str
    secret_backend: str
    secrets_dir: Path
    schemas: list[str]
    service_users: list[str]
    timeout_seconds: int
    metadata_timeout_seconds: int
    max_rows: int
    max_bytes: int

    @classmethod
    def from_environment(cls) -> "OracleServerSettings":
        def read(name: str, default: str = "") -> str:
            return os.environ.get(ENV_PREFIX + name, default)

        return cls(
            host=read("HOST"),
            port=int(read("PORT", "1521")),
            service_name=read("SERVICE_NAME"),
            username=read("USERNAME"),
            secret_key=read("SECRET_KEY"),
            secret_backend=read("SECRET_BACKEND", "auto"),
            secrets_dir=Path(read("SECRETS_DIR", "data/secrets")),
            schemas=[schema.strip().upper() for schema in read("SCHEMAS").split(",") if schema.strip()],
            service_users=[user.strip().upper() for user in read("SERVICE_USERS").split(",") if user.strip()],
            timeout_seconds=int(read("TIMEOUT_SECONDS", "10")),
            metadata_timeout_seconds=int(read("METADATA_TIMEOUT_SECONDS", "60")),
            max_rows=int(read("MAX_ROWS", "500")),
            max_bytes=int(read("MAX_BYTES", "2000000")),
        )

    def to_environment(self) -> dict[str, str]:
        return {
            ENV_PREFIX + "HOST": self.host,
            ENV_PREFIX + "PORT": str(self.port),
            ENV_PREFIX + "SERVICE_NAME": self.service_name,
            ENV_PREFIX + "USERNAME": self.username,
            ENV_PREFIX + "SECRET_KEY": self.secret_key,
            ENV_PREFIX + "SECRET_BACKEND": self.secret_backend,
            ENV_PREFIX + "SECRETS_DIR": str(self.secrets_dir),
            ENV_PREFIX + "SCHEMAS": ",".join(self.schemas),
            ENV_PREFIX + "SERVICE_USERS": ",".join(self.service_users),
            ENV_PREFIX + "TIMEOUT_SECONDS": str(self.timeout_seconds),
            ENV_PREFIX + "METADATA_TIMEOUT_SECONDS": str(self.metadata_timeout_seconds),
            ENV_PREFIX + "MAX_ROWS": str(self.max_rows),
            ENV_PREFIX + "MAX_BYTES": str(self.max_bytes),
        }

    @property
    def dsn(self) -> str:
        return f"{self.host}:{self.port}/{self.service_name}"

    def password(self) -> str:
        store = create_secret_store(self.secret_backend, self.secrets_dir)
        return store.get(SecretReference(key=self.secret_key))
