import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.agents.actions import ToolRequest, ToolSpec

MAX_RESULT_CHARS = 3500


@dataclass
class AgentTool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[..., Awaitable[dict[str, Any]]]

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.args_model)

    def as_langchain_tool(self) -> StructuredTool:
        return StructuredTool.from_function(coroutine=self.handler, name=self.name, description=self.description, args_schema=self.args_model)


@dataclass
class DispatchResult:
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    idempotency_key: str

    @property
    def ok(self) -> bool:
        return bool(self.result.get("ok", True))

    def for_model(self) -> str:
        text = json.dumps(self.result, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + '..."(kısaltıldı)"'
        return text


def idempotency_key(agent: str, tool: str, arguments: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"{agent}:{tool}:{digest}"


class ToolDispatcher:
    def __init__(self, agent: str, tools: list[AgentTool]):
        self.agent = agent
        self.langchain_tools = {tool.name: tool.as_langchain_tool() for tool in tools}

    async def dispatch(self, request: ToolRequest) -> DispatchResult:
        arguments = request.arguments.model_dump(mode="json")
        tool = self.langchain_tools[request.tool]
        try:
            result = await tool.ainvoke(arguments)
        except Exception as error:
            result = {"ok": False, "error_code": "TOOL_ERROR", "message": f"{type(error).__name__}: {str(error)[:200]}"}
        if not isinstance(result, dict):
            result = {"ok": True, "value": result}
        return DispatchResult(request.tool, arguments, result, idempotency_key(self.agent, request.tool, arguments))
