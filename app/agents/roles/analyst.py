from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.core.models.verification import AssertionResult, BusinessRuleBundle
from app.orchestration.context import RunContext

AGENT = "analyst"


class AnalystOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=500)
    hypotheses: list[Annotated[str, Field(max_length=220)]] = Field(max_length=3)
    open_questions: list[Annotated[str, Field(max_length=200)]] = Field(max_length=3)


def only_labelled_hypotheses(hypotheses: list[str]) -> list[str]:
    return [item if item.strip().lower().startswith("hipotez") else f"Hipotez: {item.strip()}" for item in hypotheses if item.strip()]


async def run_analyst(context: RunContext, status: str, interpretation: str, assertions: list[AssertionResult], coverage: list[str], rules: BusinessRuleBundle | None) -> AnalystOutput:
    task = {
        "genel_durum": status,
        "deterministik_yorum": interpretation,
        "assertionlar": [assertion.model_dump(mode="json") for assertion in assertions],
        "kapsam_notlari": coverage,
        "is_kurallari": [rule.text for rule in rules.rules] if rules else [],
        "cozulemeyenler": rules.unresolved if rules else [],
    }
    result = await context.runtime().run(AGENT, task, [], AnalystOutput, allow_clarification=False)
    output = result.output
    output.hypotheses = only_labelled_hypotheses(output.hypotheses)
    return output
