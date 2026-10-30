import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from app.agents.actions import ActionValidationError, ClarificationRequest, FinalAnswer, ToolRequest, action_schema, parse_action
from app.agents.dispatcher import AgentTool, DispatchResult, ToolDispatcher
from app.agents.prompts import system_prompt
from app.core.models.common import ContractFlowError, ErrorCode
from app.core.models.project import ModelProfile
from app.model_providers.ollama import ModelProvider


class RunCancelled(Exception):
    pass


@dataclass
class ModelBudget:
    limit: int
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.limit:
            raise ContractFlowError(ErrorCode.STEP_LIMIT, f"Çalışma başına model çağrısı limiti ({self.limit}) doldu")
        self.used += 1


@dataclass
class AgentResult:
    output: BaseModel | None = None
    clarification: ClarificationRequest | None = None
    reason: str = ""
    tool_calls: list[DispatchResult] = field(default_factory=list)


EventSink = Callable[[dict[str, Any]], Awaitable[None] | None]
CancelCheck = Callable[[], bool]


class AgentRuntime:
    def __init__(self, provider: ModelProvider, profile: ModelProfile, budget: ModelBudget, emit: EventSink, cancelled: CancelCheck, progress: Callable[[dict[str, Any]], None] | None = None):
        self.progress = progress
        self.provider = provider
        self.profile = profile
        self.budget = budget
        self.emit = emit
        self.cancelled = cancelled

    async def _emit(self, event: dict[str, Any]) -> None:
        result = self.emit(event)
        if result is not None:
            await result

    async def run(
        self,
        agent: str,
        task: dict[str, Any],
        tools: list[AgentTool],
        output_model: type[BaseModel],
        allow_clarification: bool = True,
    ) -> AgentResult:
        specs = [tool.spec for tool in tools]
        schema = action_schema(specs, output_model, allow_clarification)
        dispatcher = ToolDispatcher(agent, tools)
        tool_catalog = [{"name": tool.name, "description": tool.description, "arguments": tool.args_model.model_json_schema().get("properties", {})} for tool in tools]
        messages: list[BaseMessage] = [
            SystemMessage(system_prompt(agent)),
            HumanMessage(json.dumps({"gorev": task, "araclar": tool_catalog}, ensure_ascii=False, default=str)),
        ]
        result = AgentResult()
        for step in range(1, self.profile.max_steps_per_agent + 1):
            self._raise_if_cancelled()
            action = await self._next_action(agent, messages, schema, specs, output_model, allow_clarification)
            if isinstance(action, FinalAnswer):
                await self._emit({"type": "agent_final", "agent": agent, "step": step, "reason": action.reason[:300]})
                result.output, result.reason = action.output, action.reason
                return result
            if isinstance(action, ClarificationRequest):
                await self._emit({"type": "agent_clarification", "agent": agent, "question": action.question, "options": action.options})
                result.clarification = action
                return result
            dispatched = await self._dispatch(agent, dispatcher, action)
            result.tool_calls.append(dispatched)
            messages.append(AIMessage(json.dumps({"action": "tool_request", "tool": action.tool, "arguments": dispatched.arguments}, ensure_ascii=False)))
            remaining = self.profile.max_steps_per_agent - step
            messages.append(HumanMessage(f"ARAÇ SONUCU ({action.tool}): {dispatched.for_model()}\nKalan adım: {remaining}. Yeterli kanıt varsa 'final' üret."))
        raise ContractFlowError(ErrorCode.STEP_LIMIT, f"{agent} ajanı {self.profile.max_steps_per_agent} adımda sonuca ulaşamadı")

    async def _dispatch(self, agent: str, dispatcher: ToolDispatcher, action: ToolRequest) -> DispatchResult:
        await self._emit({"type": "tool_call", "agent": agent, "tool": action.tool, "arguments": action.arguments.model_dump(mode="json"), "reason": action.reason[:300]})
        dispatched = await dispatcher.dispatch(action)
        await self._emit(
            {
                "type": "tool_result",
                "agent": agent,
                "tool": action.tool,
                "ok": dispatched.ok,
                "summary": summarize_result(dispatched.result),
                "idempotency_key": dispatched.idempotency_key,
            }
        )
        return dispatched

    async def _next_action(self, agent, messages, schema, specs, output_model, allow_clarification):
        attempts = self.profile.max_output_repair_attempts + 1
        last_error = ""
        for attempt in range(attempts):
            self.budget.consume()
            await self._emit({"type": "model_started", "agent": agent, "attempt": attempt + 1})
            completion = await self.provider.complete_json(messages, schema, self._progress_reporter(agent))
            await self._emit({"type": "model_finished", "agent": agent, "output_tokens": completion.output_tokens, "input_tokens": completion.input_tokens})
            try:
                return parse_action(completion.content, specs, output_model, allow_clarification)
            except ActionValidationError as error:
                last_error = str(error)
                await self._emit({"type": "model_output_invalid", "agent": agent, "attempt": attempt + 1, "error": last_error[:300]})
                messages = messages + [AIMessage(completion.content[:2000]), HumanMessage(f"ÇIKTI HATASI: {last_error}. Aynı şemaya uyan düzeltilmiş tek bir JSON aksiyonu üret.")]
        raise ContractFlowError(ErrorCode.MODEL_OUTPUT_INVALID, f"{agent} ajanının çıktısı {attempts} denemede doğrulanamadı: {last_error}")

    def _progress_reporter(self, agent: str):
        if self.progress is None:
            return None

        def report(tokens: int, elapsed: float, reason: str = "") -> None:
            self.progress({"type": "model_progress", "agent": agent, "tokens": tokens, "elapsed": round(elapsed, 1), "reason": reason})

        return report

    def _raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise RunCancelled()


def summarize_result(result: dict[str, Any]) -> str:
    if not result.get("ok", True):
        return f"{result.get('error_code', 'HATA')}: {str(result.get('message', ''))[:200]}"
    parts = []
    for key, value in result.items():
        if key == "ok":
            continue
        if isinstance(value, list):
            parts.append(f"{key}: {len(value)} öğe")
        elif isinstance(value, (str, int, float)) and len(str(value)) < 80:
            parts.append(f"{key}: {value}")
    return ", ".join(parts)[:300]
