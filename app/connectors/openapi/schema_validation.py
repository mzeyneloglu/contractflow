from typing import Any

from jsonschema import Draft4Validator, Draft202012Validator

MAX_REPORTED_ERRORS = 5


def normalize_openapi30_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [normalize_openapi30_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    normalized = {key: normalize_openapi30_schema(value) for key, value in schema.items() if key not in {"nullable", "example", "xml", "discriminator", "readOnly", "writeOnly"}}
    if schema.get("nullable") and "type" in normalized:
        normalized["type"] = [normalized["type"], "null"]
    return normalized


def validate_response(schema: dict[str, Any] | None, payload: Any, source_version: str) -> list[str]:
    if not schema:
        return []
    if source_version.startswith("3.1"):
        validator = Draft202012Validator(schema)
    else:
        validator = Draft4Validator(normalize_openapi30_schema(schema))
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
    return [f"{'/'.join(str(part) for part in error.absolute_path) or '<kök>'}: {error.message[:160]}" for error in errors[:MAX_REPORTED_ERRORS]]
