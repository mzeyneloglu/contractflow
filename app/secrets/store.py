import os
from pathlib import Path
from typing import Protocol

import keyring
from cryptography.fernet import Fernet, InvalidToken
from keyring.backends.fail import Keyring as FailKeyring

from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.project import SecretReference

KEYRING_SERVICE = "contractflow"


class SecretStore(Protocol):
    backend_name: str

    def put(self, key: str, value: str) -> SecretReference: ...

    def get(self, reference: SecretReference) -> str: ...

    def delete(self, reference: SecretReference) -> None: ...


class KeyringSecretStore:
    backend_name = "os-keyring"

    def put(self, key: str, value: str) -> SecretReference:
        keyring.set_password(KEYRING_SERVICE, key, value)
        return SecretReference(key=key)

    def get(self, reference: SecretReference) -> str:
        value = keyring.get_password(KEYRING_SERVICE, reference.key)
        if value is None:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"Gizli bilgi bulunamadı: {reference}")
        return value

    def delete(self, reference: SecretReference) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, reference.key)
        except keyring.errors.PasswordDeleteError:
            pass


class EncryptedFileSecretStore:
    backend_name = "encrypted-file"

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        key_path = self.directory / "master.key"
        if not key_path.exists():
            key_path.write_bytes(Fernet.generate_key())
            os.chmod(key_path, 0o600)
        return key_path.read_bytes()

    def _path(self, key: str) -> Path:
        safe_name = "".join(character if character.isalnum() or character in "-_." else "_" for character in key)
        return self.directory / f"{safe_name}.secret"

    def put(self, key: str, value: str) -> SecretReference:
        self._path(key).write_bytes(self.fernet.encrypt(value.encode("utf-8")))
        return SecretReference(key=key)

    def get(self, reference: SecretReference) -> str:
        path = self._path(reference.key)
        if not path.exists():
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, f"Gizli bilgi bulunamadı: {reference}")
        try:
            return self.fernet.decrypt(path.read_bytes()).decode("utf-8")
        except InvalidToken as error:
            raise ContractFlowError(ErrorCode.CONFIGURATION_ERROR, "Gizli bilgi çözülemedi") from error

    def delete(self, reference: SecretReference) -> None:
        self._path(reference.key).unlink(missing_ok=True)


def keyring_available() -> bool:
    return not isinstance(keyring.get_keyring(), FailKeyring)


def create_secret_store(backend: str, secrets_dir: Path) -> SecretStore:
    if backend == "keyring" or (backend == "auto" and keyring_available()):
        return KeyringSecretStore()
    return EncryptedFileSecretStore(secrets_dir)


def secret_key(project_id: str, name: str) -> str:
    return f"{project_id}/{name}"
