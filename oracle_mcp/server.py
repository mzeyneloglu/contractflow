import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from oracle_mcp.database import OracleGateway
from oracle_mcp.settings import OracleServerSettings
from oracle_mcp.tools.handlers import OracleToolHandlers


def create_server(handlers: OracleToolHandlers) -> MCPServer:
    server = MCPServer("oracle_mcp", instructions="Yalnızca okuma amaçlı Oracle şema keşfi ve SELECT yürütme araçları.")

    @server.tool(description="Bağlantıyı ve Oracle sürümünü kontrol eder")
    def ping() -> dict[str, Any]:
        return handlers.ping()

    @server.tool(description="Veritabanı saatini döndürür (çalışma anı SQL yakalama penceresi için)")
    def database_time() -> dict[str, Any]:
        return handlers.database_time()

    @server.tool(description="Servisin DB kullanıcısının verilen zamandan sonra çalıştırdığı SELECT ifadelerini V$SQL'den okur (sabit sorgu, salt-okunur)")
    def recent_service_sql(since: str) -> dict[str, Any]:
        return handlers.recent_service_sql(since)

    @server.tool(description="İzin verilen şemaları listeler")
    def list_schemas() -> dict[str, Any]:
        return handlers.list_schemas()

    @server.tool(description="İzinli tablo ve view'ları açıklamalarıyla listeler")
    def list_tables(schema: str = "") -> dict[str, Any]:
        return handlers.list_tables(schema)

    @server.tool(description="Tablo, kolon ve açıklamalarda kelime araması yapar")
    def search_schema(keyword: str) -> dict[str, Any]:
        return handlers.search_schema(keyword)

    @server.tool(description="Tablonun kolonlarını, tiplerini, null bilgisini, PK ve açıklamalarını döndürür")
    def describe_table(table: str) -> dict[str, Any]:
        return handlers.describe_table(table)

    @server.tool(description="Tablonun PK/FK ilişkilerini döndürür")
    def get_relationships(table: str) -> dict[str, Any]:
        return handlers.get_relationships(table)

    @server.tool(description="Yapılandırılmış sorgu planını SQL'e dönüştürür ve doğrular; çalıştırmaz")
    def build_select(plan: dict[str, Any]) -> dict[str, Any]:
        return handlers.build_select(plan)

    @server.tool(description="SELECT ifadesini AST, nesne, bind ve politika açısından doğrular")
    def validate_select(sql: str, binds: dict[str, Any] | None = None) -> dict[str, Any]:
        return handlers.validate_select(sql, binds)

    @server.tool(description="SELECT ifadesini yeniden doğrulayıp bind parametreleriyle salt-okunur yürütür")
    def execute_select(sql: str, binds: dict[str, Any] | None = None) -> dict[str, Any]:
        return handlers.execute_select(sql, binds)

    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    settings = OracleServerSettings.from_environment()
    gateway = OracleGateway(settings)
    try:
        create_server(OracleToolHandlers(gateway, settings.schemas)).run("stdio")
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
