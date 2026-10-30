import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors.oracle_mcp.client import OracleMcpConnector
from app.core.models.project import ComponentState
from app.services.container import get_container
from app.services.setup import SetupService


def line(label: str, ok: bool, detail: str) -> bool:
    print(f"[{'OK' if ok else 'HATA'}] {label}: {detail}")
    return ok


async def run(project_id: str) -> int:
    container = get_container()
    setup = SetupService(container)
    project = setup.project(project_id)
    print(f"Proje: {project.name} ({project.id}) — gerçek bağlantılarla smoke testi\n")
    results: list[bool] = []
    async with OracleMcpConnector(project.oracle, container.settings) as connector:
        results.append(line("MCP araç keşfi", len(connector.discovered_tools) >= 8, ", ".join(connector.discovered_tools)))
        ping = await connector.call("ping")
        results.append(line("Oracle ping", ping.get("ok", False), ping.get("server_version") or ping.get("message", "")))
        dual = await connector.call("execute_select", {"sql": "SELECT 1 AS ONE FROM DUAL", "binds": {}})
        results.append(line("Gerçek SELECT", dual.get("ok", False), str(dual.get("rows") or dual.get("message"))))
        tables = await connector.call("list_tables")
        results.append(line("İzinli tablolar", tables.get("ok", False) and bool(tables.get("tables")), f"{len(tables.get('tables', []))} tablo/view"))
        blocked = await connector.call("execute_select", {"sql": "DELETE FROM DUAL", "binds": {}})
        results.append(line("SELECT-only politika", blocked.get("error_code") == "POLICY_VIOLATION", blocked.get("message", "")))
    project = await setup.test_api(project_id)
    results.append(line("Hedef servis", project.readiness.api.state is ComponentState.READY, project.readiness.api.message))
    project = await setup.probe_model(project_id)
    results.append(line("Ollama yetenek testi", project.readiness.ollama.state is ComponentState.READY, project.readiness.ollama.message))
    knowledge = setup.refresh_knowledge_status(project).readiness.knowledge_base
    results.append(line("Bilgi tabanı", knowledge.state is ComponentState.READY, knowledge.message))
    print("\nSonuç:", "tüm gerçek entegrasyonlar hazır" if all(results) else "eksikler var")
    return 0 if all(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Bir ContractFlow projesinin gerçek Oracle, API ve Ollama bağlantılarını doğrular")
    parser.add_argument("project_id")
    return asyncio.run(run(parser.parse_args().project_id))


if __name__ == "__main__":
    raise SystemExit(main())
