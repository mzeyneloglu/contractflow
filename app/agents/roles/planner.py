import re
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.grounding import catalog_suggestions, normalize
from app.core.models.verification import FilterCondition, VerificationIntent
from app.orchestration.context import RunContext

AGENT = "planner"
CAPABILITY_MESSAGE = (
    "ContractFlow bir kaynağın kayıt adedini; iş kuralı belgeleri, OpenAPI sözleşmesi, gerçek API yanıtı ve Oracle verisi üzerinden doğrular. "
    "Örnek talep: \"<servis> servisinde <parametre>'si <değer> olan kayıtların sayısı <N> olmalı. API ile Oracle'ı karşılaştır.\""
)


class PlannedFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=80)
    value: str = Field(max_length=120)


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_type: Literal["count_verification", "other"]
    resource_hint: str = Field(max_length=120)
    filters: list[PlannedFilter] = Field(max_length=6)
    expected_value: int | None
    ambiguities: list[Annotated[str, Field(max_length=200)]] = Field(max_length=3)


@dataclass
class PlannerDecision:
    intent: VerificationIntent | None = None
    off_topic: bool = False
    question: str = ""
    options: list[str] = field(default_factory=list)


def ground_intent(output: PlannerOutput, user_request: str) -> VerificationIntent:
    request = normalize(user_request)
    ambiguities = list(output.ambiguities)
    filters: list[FilterCondition] = []
    for planned in output.filters:
        if planned.value.strip() and normalize(planned.value) in request:
            filters.append(FilterCondition(name=planned.name, value=planned.value))
        else:
            ambiguities.append(f"Talepte bulunmayan filtre çıkarıldı: {planned.name}={planned.value}")
    expected = output.expected_value
    if expected is not None and not re.search(rf"(?<!\d){expected}(?!\d)", user_request):
        ambiguities.append(f"Talepte bulunmayan beklenti değeri çıkarıldı: {expected}")
        expected = None
    resource = output.resource_hint.strip()
    if resource and normalize(resource) not in request:
        ambiguities.append(f"Talepte bulunmayan kaynak adı çıkarıldı: {resource}")
        resource = ""
    return VerificationIntent(resource_hint=resource, filters=filters, expected_value=expected, ambiguities=ambiguities)


def known_resources(context: RunContext, user_request: str) -> list[str]:
    return catalog_suggestions(context.catalog, user_request, limit=8)


def missing_information_question(intent: VerificationIntent) -> str:
    missing = []
    if not intent.resource_hint:
        missing.append("hangi servis/endpoint")
    if not intent.filters:
        missing.append("hangi filtre ve değer")
    return "Talepte şu bilgiler bulunamadı: " + ", ".join(missing) + ". Örnek: \"<servis> servisinde <parametre>'si <değer> olan kayıtların sayısı <N> olmalı.\""


async def run_planner(context: RunContext, user_request: str, clarifications: list[str]) -> PlannerDecision:
    task = {"kullanici_talebi": user_request, "bilinen_kaynaklar": known_resources(context, user_request), "kullanici_aciklamalari": clarifications}
    result = await context.runtime().run(AGENT, task, [], PlannerOutput, allow_clarification=False)
    output: PlannerOutput = result.output
    if output.request_type == "other" and not clarifications:
        return PlannerDecision(off_topic=True)
    combined = user_request + " " + " ".join(clarifications)
    intent = ground_intent(output, combined)
    if intent.resource_hint and intent.filters:
        return PlannerDecision(intent=intent)
    return PlannerDecision(question=missing_information_question(intent), options=catalog_suggestions(context.catalog, combined))
