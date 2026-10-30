import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.config import PROJECT_ROOT, Settings
from app.core.models.common import ErrorCode
from app.core.models.project import OracleConnectionProfile
from oracle_mcp.settings import OracleServerSettings

READ_ONLY_TOOLS = frozenset(
    {"ping", "list_schemas", "list_tables", "search_schema", "describe_table", "get_relationships", "build_select", "validate_select", "execute_select", "database_time", "recent_service_sql"}
)


def server_settings(profile: OracleConnectionProfile, settings: Settings) -> OracleServerSettings:
    return OracleServerSettings(
        host=profile.host,
        port=profile.port,
        service_name=profile.service_name,
        username=profile.username,
        secret_key=profile.password_ref.key if profile.password_ref else "",
        secret_backend=settings.secret_backend,
        secrets_dir=Path(settings.secrets_dir).resolve(),
        schemas=profile.schema_allowlist,
        service_users=profile.service_db_users,
        timeout_seconds=settings.query_timeout_seconds,
        metadata_timeout_seconds=settings.metadata_timeout_seconds,
        max_rows=settings.query_max_rows,
        max_bytes=settings.query_max_bytes,
    )


class OracleMcpConnector:
    def __init__(self, profile: OracleConnectionProfile, settings: Settings, errlog_path: Path | None = None):
        self.parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", settings.oracle_mcp_module],
            env=server_settings(profile, settings).to_environment() | {"PYTHONIOENCODING": "utf-8"},
            cwd=str(PROJECT_ROOT),
        )
        self.errlog_path = errlog_path or Path(settings.data_dir) / "oracle_mcp.log"
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.discovered_tools: list[str] = []

    async def __aenter__(self) -> "OracleMcpConnector":
        self._stack = AsyncExitStack()
        errlog = self._stack.enter_context(open(self.errlog_path, "a", encoding="utf-8"))
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(self.parameters, errlog=errlog))
        self._session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await self._session.initialize()
        listed = await self._session.list_tools()
        self.discovered_tools = sorted(tool.name for tool in listed.tools)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if tool not in READ_ONLY_TOOLS or tool not in self.discovered_tools:
            return {"ok": False, "error_code": ErrorCode.POLICY_VIOLATION.value, "message": f"Bilinmeyen veya izinsiz araç: {tool}"}
        if self._session is None:
            raise RuntimeError("MCP oturumu açık değil")
        result = await self._session.call_tool(tool, arguments or {})
        text = "".join(getattr(content, "text", "") for content in result.content)
        if result.is_error:
            return {"ok": False, "error_code": ErrorCode.DATABASE_ERROR.value, "message": text[:500]}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error_code": ErrorCode.DATABASE_ERROR.value, "message": "MCP yanıtı JSON değil"}
