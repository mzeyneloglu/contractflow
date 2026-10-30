from pathlib import Path

import pytest

from app.agents.runtime import ModelBudget
from app.config import Settings
from app.connectors.oracle_mcp.client import OracleMcpConnector
from app.connectors.openapi.parser import parse_catalog
from app.core.models.project import OracleConnectionProfile, ProjectProfile
from app.core.models.service import ResponseProfile
from app.core.comparison.engine import compare_counts, interpret
from app.core.models.verification import ApiObservation, BusinessRuleBundle, DatabaseObservation
from app.orchestration.context import RunContext
from app.orchestration.graph import VerificationGraph
from app.secrets.store import secret_key
from app.services.container import build_container
from tests.support.simulation import create_database, openapi_document, service_orders

REPOSITORY = """
package demo;
public interface OrderRepository extends JpaRepository<OrderEntity, Long> {
    @Query(value = "SELECT o.ID FROM ORDERS o WHERE o.CUSTOMER_ID = :customerId AND o.STATUS = 'A' AND o.DELETED_AT IS NULL ORDER BY o.ID", nativeQuery = true)
    Page<OrderEntity> findActive(@Param("customerId") Long customerId, Pageable pageable);
}
"""
CONTROLLER = """
package demo;
@RestController
public class OrderController {
    private final OrderRepository orderRepository;
    @GetMapping("/orders")
    public Page<OrderEntity> list(@RequestParam Long customerId, Pageable pageable) {
        return orderRepository.findActive(customerId, pageable);
    }
}
"""


async def noop(event):
    return None


@pytest.fixture
def code_project(tmp_path):
    source = tmp_path / "repo" / "src" / "main" / "java" / "demo"
    source.mkdir(parents=True)
    (source / "OrderRepository.java").write_text(REPOSITORY, encoding="utf-8")
    (source / "OrderController.java").write_text(CONTROLLER, encoding="utf-8")
    database = create_database(tmp_path / "simulation.sqlite3")
    settings = Settings(data_dir=tmp_path / "data", secret_backend="file", oracle_mcp_module="tests.support.sim_oracle_server")
    container = build_container(settings)
    project = ProjectProfile(name="SIMULATION kod", code_paths=[str(tmp_path / "repo")])
    project.oracle = OracleConnectionProfile(host="simulation", service_name=str(database), username="sim", password_ref=container.secrets.put(secret_key(project.id, "oracle_password"), "sim"), schema_allowlist=["APP"])
    return container, project


async def test_service_query_from_code_is_wrapped_validated_and_executed(code_project):
    container, project = code_project
    catalog = parse_catalog(openapi_document())
    state = {
        "operation_id": "listOrders",
        "api_choice": {"operation_id": "listOrders", "path_params": {}, "query_params": {"customerId": "7"}},
        "response_profile": ResponseProfile(page_param="page", size_param="size").model_dump(mode="json"),
        "rules": BusinessRuleBundle().model_dump(mode="json"),
    }
    async with OracleMcpConnector(project.oracle, container.settings) as connector:
        context = RunContext("run_code", project, catalog, container, None, ModelBudget(0), noop, lambda: False, oracle=connector)
        graph = VerificationGraph(context)
        code_update = await graph.code(state)
        assert code_update["code_sql"]["reference"].endswith("(OrderRepository.findActive)")
        assert "APP.ORDERS" in code_update["code_sql"]["sql"]
        state.update(code_update)
        implementation, observation = await graph._implementation(state, "")
    assert implementation["source"] == "code"
    assert observation.error_code is None, observation.error_message
    assert observation.rows == [[12]]
    rule_texts = [rule["text"] for rule in code_update["rules"]["rules"]]
    assert any(text.startswith("Kod (native sorgu)") for text in rule_texts)


async def test_runtime_capture_finds_service_sql_and_triangulates(code_project):
    container, project = code_project
    project.oracle.capture_runtime_sql = True
    database = Path(project.oracle.service_name)
    catalog = parse_catalog(openapi_document())
    state = {
        "operation_id": "listLegacyOrders",
        "api_choice": {"operation_id": "listLegacyOrders", "path_params": {}, "query_params": {"customerId": "7"}},
        "response_profile": ResponseProfile(page_param="page", size_param="size").model_dump(mode="json"),
    }
    async with OracleMcpConnector(project.oracle, container.settings) as connector:
        context = RunContext("run_runtime", project, catalog, container, None, ModelBudget(0), noop, lambda: False, oracle=connector)
        graph = VerificationGraph(context)
        window = await graph._database_time()
        service_orders(database, "listLegacyOrders", 7)
        implementation, observation = await graph._implementation(state, window)
    assert implementation["source"] == "vsql"
    assert "V$SQL_BIND_CAPTURE" in implementation["notes"][0]
    assert observation.rows == [[15]]
    outcome = compare_counts(None, ApiObservation(operation_id="x", curl="c", fetched_count=15, unique_id_count=15, complete=True), DatabaseObservation(sql="s", rows=[[12]], row_count=1), observation)
    assert "iş mantığı/filtre farkı" in interpret(outcome)
