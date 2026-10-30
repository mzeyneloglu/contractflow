from oracle_mcp.server import create_server
from oracle_mcp.tools.handlers import OracleToolHandlers
from tests.support.simulation import SCHEMA, SqliteSimulationGateway, simulation_db_path


def main() -> None:
    create_server(OracleToolHandlers(SqliteSimulationGateway(simulation_db_path()), [SCHEMA])).run("stdio")


if __name__ == "__main__":
    main()
