from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_FOR_DOCUMENTS = "WAITING_FOR_DOCUMENTS"
    WAITING_FOR_OLLAMA = "WAITING_FOR_OLLAMA"
    WAITING_FOR_SERVICE = "WAITING_FOR_SERVICE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {RunStatus.PASS, RunStatus.FAIL, RunStatus.INCONCLUSIVE, RunStatus.ERROR, RunStatus.CANCELLED}

    @property
    def is_waiting(self) -> bool:
        return self in {
            RunStatus.WAITING_FOR_DOCUMENTS,
            RunStatus.WAITING_FOR_OLLAMA,
            RunStatus.WAITING_FOR_SERVICE,
            RunStatus.AUTH_REQUIRED,
            RunStatus.NEEDS_CLARIFICATION,
        }


class ErrorCode(StrEnum):
    CONNECTION_REFUSED = "CONNECTION_REFUSED"
    TIMEOUT = "TIMEOUT"
    DNS_FAILURE = "DNS_FAILURE"
    TLS_FAILURE = "TLS_FAILURE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    HTTP_ERROR = "HTTP_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PAGINATION_LIMIT = "PAGINATION_LIMIT"
    UNSUPPORTED_QUERY = "UNSUPPORTED_QUERY"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    UNKNOWN_OBJECT = "UNKNOWN_OBJECT"
    DATABASE_ERROR = "DATABASE_ERROR"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    RESULT_TRUNCATED = "RESULT_TRUNCATED"
    MODEL_CAPABILITY_ERROR = "MODEL_CAPABILITY_ERROR"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    STEP_LIMIT = "STEP_LIMIT"
    UNSUPPORTED_SPEC = "UNSUPPORTED_SPEC"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"


class ContractFlowError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
