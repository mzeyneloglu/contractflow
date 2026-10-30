from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agents.runtime import AgentRuntime, ModelBudget
from app.core.models.common import utc_now
from app.connectors.oracle_mcp.client import OracleMcpConnector
from app.core.models.project import ProjectProfile
from app.core.models.service import ServiceCatalog
from app.model_providers.ollama import ModelProvider
from app.rag.knowledge import KnowledgeSnapshot
from app.services.container import Container


@dataclass
class RunContext:
    run_id: str
    project: ProjectProfile
    catalog: ServiceCatalog | None
    container: Container
    provider: ModelProvider
    budget: ModelBudget
    emit: Callable[[dict[str, Any]], Awaitable[None]]
    cancelled: Callable[[], bool]
    oracle: OracleMcpConnector | None = None
    snapshot: KnowledgeSnapshot | None = None
    table_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def runtime(self) -> AgentRuntime:
        return AgentRuntime(self.provider, self.project.model, self.budget, self.emit, self.cancelled, self.progress)

    def progress(self, event: dict[str, Any]) -> None:
        if self.container is not None:
            self.container.runs.add_event(self.run_id, {**event, "at": utc_now().isoformat()})

    async def oracle_call(self, agent: str, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.oracle is None:
            return {"ok": False, "error_code": "CONFIGURATION_ERROR", "message": "Oracle MCP oturumu yok"}
        await self.emit({"type": "tool_call", "agent": agent, "tool": f"oracle_mcp.{tool}", "arguments": arguments or {}})
        result = await self.oracle.call(tool, arguments or {})
        await self.emit({"type": "tool_result", "agent": agent, "tool": f"oracle_mcp.{tool}", "ok": result.get("ok", False), "summary": result.get("message", "")[:200] if not result.get("ok") else ""})
        if tool == "describe_table" and result.get("ok"):
            self.table_cache[result["table"]] = result
        return result
