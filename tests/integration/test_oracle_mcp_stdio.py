import os

import pytest

from app.config import Settings
from app.connectors.oracle_mcp.client import OracleMcpConnector
from app.core.models.project import OracleConnectionProfile, SecretReference
from app.secrets.store import EncryptedFileSecretStore

EXPECTED_TOOLS = {"ping", "list_schemas", "list_tables", "search_schema", "describe_table", "get_relationships", "build_select", "validate_select", "execute_select", "database_time", "recent_service_sql"}


def unreachable_profile() -> OracleConnectionProfile:
    return OracleConnectionProfile(host="127.0.0.1", port=1, service_name="NONE", username="nobody", password_ref=SecretReference(key="test/none"), schema_allowlist=["APP"])


@pytest.fixture
def settings(tmp_path) -> Settings:
    settings = Settings(data_dir=tmp_path, secret_backend="file")
    settings.ensure_directories()
    return settings


async def test_real_stdio_discovery_and_server_side_policy(settings):
    async with OracleMcpConnector(unreachable_profile(), settings) as connector:
        assert set(connector.discovered_tools) == EXPECTED_TOOLS
        rejected = await connector.call("execute_select", {"sql": "DELETE FROM APP.T", "binds": {}})
        assert rejected["ok"] is False
        assert rejected["error_code"] == "POLICY_VIOLATION"
        unknown = await connector.call("drop_everything", {})
        assert unknown["error_code"] == "POLICY_VIOLATION"


async def test_missing_secret_is_configuration_error(settings):
    async with OracleMcpConnector(unreachable_profile(), settings) as connector:
        result = await connector.call("ping")
        assert result["ok"] is False
        assert result["error_code"] == "CONFIGURATION_ERROR"


async def test_unreachable_database_is_connection_refused_and_password_not_leaked(settings):
    EncryptedFileSecretStore(settings.secrets_dir).put("test/none", "s3cret-value")
    async with OracleMcpConnector(unreachable_profile(), settings) as connector:
        result = await connector.call("ping")
        assert result["ok"] is False
        assert result["error_code"] == "CONNECTION_REFUSED"
        assert "s3cret-value" not in result["message"]
    assert "s3cret-value" not in " ".join(connector.parameters.args)
    assert "s3cret-value" not in " ".join((connector.parameters.env or {}).values())


@pytest.mark.oracle
@pytest.mark.skipif(not os.environ.get("CONTRACTFLOW_TEST_PROJECT_ID"), reason="BLOCKED: gerçek Oracle projesi yapılandırılmadı (CONTRACTFLOW_TEST_PROJECT_ID)")
async def test_real_oracle_ping_and_metadata():
    from app.services.container import get_container

    container = get_container()
    project = container.projects.get(os.environ["CONTRACTFLOW_TEST_PROJECT_ID"])
    async with OracleMcpConnector(project.oracle, container.settings) as connector:
        ping = await connector.call("ping")
        assert ping["ok"], ping
        tables = await connector.call("list_tables")
        assert tables["ok"] and tables["tables"]
        smoke = await connector.call("execute_select", {"sql": "SELECT 1 AS ONE FROM DUAL", "binds": {}})
        assert smoke["ok"], smoke
        assert smoke["rows"] in ([[1]], [["1"]])
