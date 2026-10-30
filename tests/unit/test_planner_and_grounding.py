import json

import pytest

from app.agents.grounding import catalog_suggestions, ground_options, schema_vocabulary
from app.agents.roles.planner import run_planner
from app.agents.runtime import ModelBudget
from app.connectors.openapi.parser import parse_catalog
from app.core.models.project import ProjectProfile
from app.model_providers.ollama import Completion
from app.orchestration.context import RunContext
from tests.support.simulation import openapi_document


class ScriptedProvider:
    name = "scripted"

    def __init__(self, output: dict):
        self.output = output
        self.calls = 0

    async def complete_json(self, messages, schema, on_progress=None):
        self.calls += 1
        return Completion(json.dumps({"action": "final", "output": self.output, "reason": "test"}))


async def noop(event):
    return None


def context_with(provider) -> RunContext:
    return RunContext(
        run_id="run_test",
        project=ProjectProfile(name="test"),
        catalog=parse_catalog(openapi_document()),
        container=None,
        provider=provider,
        budget=ModelBudget(5),
        emit=noop,
        cancelled=lambda: False,
    )


def planner_output(**overrides) -> dict:
    values = {"request_type": "count_verification", "resource_hint": "", "filters": [], "expected_value": None, "ambiguities": []}
    values.update(overrides)
    return values


async def test_off_topic_request_is_not_turned_into_a_verification():
    decision = await run_planner(context_with(ScriptedProvider(planner_output(request_type="other"))), "Merhaba, senin görevin nedir ?", [])
    assert decision.off_topic and decision.intent is None and not decision.options


async def test_hallucinated_resource_is_removed_and_question_uses_catalog():
    output = planner_output(resource_hint="employees", filters=[{"name": "id", "value": "5"}])
    decision = await run_planner(context_with(ScriptedProvider(output)), "kayıtları kontrol et", [])
    assert decision.intent is None
    assert "hangi servis" in decision.question
    assert all(option.startswith("GET /") for option in decision.options)


async def test_grounded_request_produces_intent():
    output = planner_output(resource_hint="orders", filters=[{"name": "customerId", "value": "7"}], expected_value=12)
    decision = await run_planner(context_with(ScriptedProvider(output)), "orders servisinde customerId'si 7 olan siparişlerin sayısı 12 olmalı", [])
    assert decision.intent.resource_hint == "orders"
    assert decision.intent.expected_value == 12


def test_ungrounded_clarification_options_are_dropped():
    vocabulary = ["listOrders GET /orders Müşterinin aktif siparişleri orders customerId"]
    assert ground_options(["orders", "customers", "employees", "branches"], vocabulary) == ["orders"]
    assert ground_options(["", "x"], vocabulary) == []


def test_schema_options_must_exist_in_described_tables():
    cache = {"APP.ORDERS": {"columns": [{"name": "STATUS"}, {"name": "DELETED_AT"}]}}
    assert ground_options(["APP.ORDERS", "STATUS", "APP.INVENTED", "IS_ACTIVE"], schema_vocabulary(cache)) == ["APP.ORDERS", "STATUS"]


def test_catalog_suggestions_come_from_real_operations():
    catalog = parse_catalog(openapi_document())
    suggestions = catalog_suggestions(catalog, "sipariş müşteri orders")
    assert suggestions and suggestions[0].startswith("GET /orders")
    assert catalog_suggestions(catalog, "") == []
