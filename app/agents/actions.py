import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.connectors.openapi.parser import RefResolver

STRIPPED_SCHEMA_KEYS = {"title", "default", "$defs", "examples", "description"}
REASON_SCHEMA = {"type": "string", "maxLength": 200}


def inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    resolved = RefResolver(raw).resolve(raw)
    return strip_schema(resolved)


def strip_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        cleaned = {key: strip_schema(value) for key, value in schema.items() if key not in STRIPPED_SCHEMA_KEYS}
        if cleaned.get("type") == "object" and "properties" in cleaned:
            cleaned.setdefault("additionalProperties", False)
        return cleaned
    if isinstance(schema, list):
        return [strip_schema(item) for item in schema]
    return schema


@dataclass(frozen=True)
class ToolSpec:
    name: str
    args_model: type[BaseModel]


def tool_variant(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {"const": "tool_request"},
            "tool": {"const": tool.name},
            "arguments": inline_schema(tool.args_model),
            "reason": REASON_SCHEMA,
        },
        "required": ["action", "tool", "arguments", "reason"],
        "additionalProperties": False,
    }


def final_variant(output_model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"action": {"const": "final"}, "output": inline_schema(output_model), "reason": REASON_SCHEMA},
        "required": ["action", "output", "reason"],
        "additionalProperties": False,
    }


CLARIFICATION_VARIANT = {
    "type": "object",
    "properties": {
        "action": {"const": "need_clarification"},
        "question": {"type": "string", "maxLength": 300},
        "options": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 5},
    },
    "required": ["action", "question", "options"],
    "additionalProperties": False,
}


def action_schema(tools: list[ToolSpec], output_model: type[BaseModel], allow_clarification: bool = True) -> dict[str, Any]:
    variants = [tool_variant(tool) for tool in tools] + [final_variant(output_model)]
    if allow_clarification:
        variants.append(CLARIFICATION_VARIANT)
    return {"anyOf": variants}


@dataclass
class ToolRequest:
    tool: str
    arguments: BaseModel
    reason: str


@dataclass
class FinalAnswer:
    output: BaseModel
    reason: str


@dataclass
class ClarificationRequest:
    question: str
    options: list[str]


Action = ToolRequest | FinalAnswer | ClarificationRequest


class ActionValidationError(Exception):
    pass


def parse_action(raw: str, tools: list[ToolSpec], output_model: type[BaseModel], allow_clarification: bool = True) -> Action:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ActionValidationError(f"Geçerli JSON değil: {error.msg}") from error
    if not isinstance(payload, dict):
        raise ActionValidationError("Aksiyon bir JSON nesnesi olmalı")
    kind = payload.get("action")
    allowed_keys = {"tool_request": {"action", "tool", "arguments", "reason"}, "final": {"action", "output", "reason"}, "need_clarification": {"action", "question", "options"}}
    if kind not in allowed_keys or (kind == "need_clarification" and not allow_clarification):
        raise ActionValidationError(f"Geçersiz aksiyon tipi: {kind!r}")
    extra = set(payload) - allowed_keys[kind]
    if extra:
        raise ActionValidationError(f"Bu aksiyon tipinde izin verilmeyen alanlar: {', '.join(sorted(extra))}")
    try:
        if kind == "tool_request":
            by_name = {tool.name: tool for tool in tools}
            tool = by_name.get(payload.get("tool"))
            if tool is None:
                raise ActionValidationError(f"İzin verilmeyen araç: {payload.get('tool')!r}; izinli: {', '.join(by_name) or 'yok'}")
            return ToolRequest(tool.name, tool.args_model.model_validate(payload.get("arguments") or {}), str(payload.get("reason", "")))
        if kind == "final":
            return FinalAnswer(output_model.model_validate(payload.get("output") or {}), str(payload.get("reason", "")))
        question = str(payload.get("question", "")).strip()
        if not question:
            raise ActionValidationError("Açıklama sorusu boş olamaz")
        return ClarificationRequest(question, [str(option) for option in payload.get("options", []) or []])
    except ValidationError as error:
        details = "; ".join(f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()[:5])
        raise ActionValidationError(f"Şema doğrulaması başarısız: {details}") from error
