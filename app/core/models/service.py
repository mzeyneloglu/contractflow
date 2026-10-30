from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ParameterLocation(StrEnum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"


class OperationParameter(BaseModel):
    name: str
    location: ParameterLocation
    required: bool = False
    schema_type: str = "string"
    description: str = ""
    enum: list[Any] = Field(default_factory=list)
    default: Any = None
    example: Any = None
    style: str = "form"
    explode: bool = True


class SecurityScheme(BaseModel):
    name: str
    scheme_type: str
    location: str = ""
    parameter_name: str = ""
    http_scheme: str = ""
    supported: bool = True
    note: str = ""


class ResponseSpec(BaseModel):
    status_code: str
    description: str = ""
    content_type: str = ""
    json_schema: dict[str, Any] | None = None


class ServiceOperation(BaseModel):
    operation_id: str
    method: str
    path: str
    summary: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: list[OperationParameter] = Field(default_factory=list)
    has_request_body: bool = False
    responses: list[ResponseSpec] = Field(default_factory=list)
    security: list[str] = Field(default_factory=list)
    executable: bool = False
    unsupported_features: list[str] = Field(default_factory=list)

    def success_response(self) -> ResponseSpec | None:
        for response in sorted(self.responses, key=lambda item: item.status_code):
            if response.status_code.startswith("2"):
                return response
        return None

    def search_text(self) -> str:
        parameter_names = " ".join(parameter.name for parameter in self.parameters)
        return " ".join([self.operation_id, self.method, self.path, self.summary, self.description, " ".join(self.tags), parameter_names])


class ServiceCatalog(BaseModel):
    source_version: str
    title: str = ""
    servers: list[str] = Field(default_factory=list)
    operations: list[ServiceOperation] = Field(default_factory=list)
    security_schemes: list[SecurityScheme] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    content_hash: str = ""

    def find(self, operation_id: str) -> ServiceOperation | None:
        return next((operation for operation in self.operations if operation.operation_id == operation_id), None)


class PaginationStrategy(StrEnum):
    NONE = "none"
    PAGE_NUMBER = "page_number"
    OFFSET_LIMIT = "offset_limit"


class ResponseProfile(BaseModel):
    items_path: str = ""
    total_path: str = ""
    id_path: str = "id"
    pagination: PaginationStrategy = PaginationStrategy.NONE
    page_param: str = "page"
    size_param: str = "size"
    first_page: int = 0
    page_size: int = 100
    max_pages: int = 200
    max_bytes: int = 20_000_000
    detected_by: str = ""
    locked: bool = False
