"""The agent loop, written by hand (no framework) so every step is visible.

    messages = [user question]
    repeat up to max_steps:
        response = model(system, tools, messages)
        append the assistant turn to messages, unchanged
        if the model asked for tools: run them all, append ALL results
            in ONE user message, and loop again
        else: the text is the final answer -> stop

Guards: max steps, a whole-run timeout, SDK timeouts/retries per model
call, tools that return errors instead of raising, and a log entry for
every run whatever happens.
"""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from gridagent.agent.config import LoadedAgent
from gridagent.agent.runlog import RESULT_PREVIEW_CHARS, RunLog, StepLog, ToolCallLog
from gridagent.data.provider import DataProvider
from gridagent.llm.base import LLM, LLMError, ModelResponse
from gridagent.llm.pricing import estimate_cost_usd
from gridagent.tools.toolbox import ToolBox

REFUSAL_ANSWER = "Sorry, I can't help with that request."
FAILURE_ANSWER = "Sorry, I couldn't complete this request. No answer was produced."


def build_user_message(question: str, today: date) -> str:
    """Put today's date in the user turn, not the system prompt.

    The system prompt must stay byte-identical between runs so it can be
    prompt-cached; a date in it would change every day.
    """
    return f"Today's date in Germany is {today.isoformat()} ({today:%A}).\n\nQuestion: {question}"


class Agent:
    """Runs one agent definition against a model and a data provider."""

    def __init__(
        self,
        agent: LoadedAgent,
        llm: LLM,
        data: DataProvider,
        today: Callable[[], date],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.agent = agent
        self.llm = llm
        self.data = data
        self.today = today
        self._clock = clock  # injectable for timeout tests

    def run(self, question: str) -> RunLog:
        config = self.agent.config
        today = self.today()
        tools = ToolBox(self.data, today=lambda: today, enabled=config.tools)
        log = RunLog(
            run_id=uuid.uuid4().hex[:12],
            started_at=datetime.now(UTC),
            agent=config.name,
            agent_version=config.version,
            model=self.llm.model_name,
            question=question,
            today=today.isoformat(),
        )
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": build_user_message(question, today)}
        ]
        run_start = self._clock()

        try:
            self._loop(log, tools, messages, run_start)
        except Exception as exc:  # last line of defence: always produce a log
            log.status, log.error = "error", f"Unexpected error: {exc!r}"
            log.final_answer = FAILURE_ANSWER

        log.latency_s = round(self._clock() - run_start, 3)
        return log

    def _loop(
        self, log: RunLog, tools: ToolBox, messages: list[dict[str, Any]], run_start: float
    ) -> None:
        limits = self.agent.config.limits
        for step in range(1, limits.max_steps + 1):
            if self._clock() - run_start > limits.run_timeout_seconds:
                log.status, log.error = "timeout", "Run timeout exceeded"
                log.final_answer = FAILURE_ANSWER
                return

            t0 = self._clock()
            try:
                response = self.llm.create(self.agent.system_prompt, tools.definitions(), messages)
            except LLMError as exc:
                log.status, log.error, log.final_answer = "error", str(exc), FAILURE_ANSWER
                return
            step_log = self._record_step(log, step, response, self._clock() - t0)

            # Keep the assistant turn exactly as returned (thinking blocks included).
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                results = [self._run_tool(tools, call, step_log) for call in response.tool_uses()]
                messages.append({"role": "user", "content": results})  # ALL results, one turn
                continue
            if response.stop_reason == "pause_turn":
                continue  # server asked us to resend to let it continue
            if response.stop_reason == "end_turn":
                log.final_answer = response.text()
                return
            if response.stop_reason == "refusal":
                log.status, log.final_answer = "refused", REFUSAL_ANSWER
                return
            # max_tokens or anything unexpected: don't trust a cut-off answer.
            log.status = "error"
            log.error = f"Model stopped with stop_reason={response.stop_reason}"
            log.final_answer = FAILURE_ANSWER
            return

        log.status, log.error = "max_steps", f"No final answer after {limits.max_steps} steps"
        log.final_answer = FAILURE_ANSWER

    @staticmethod
    def _record_step(log: RunLog, step: int, response: ModelResponse, latency: float) -> StepLog:
        cost = estimate_cost_usd(response.model, response.usage)
        step_log = StepLog(
            step=step,
            model=response.model,
            stop_reason=response.stop_reason,
            latency_s=round(latency, 3),
            usage=response.usage,
            cost_usd=cost,
        )
        log.steps.append(step_log)
        log.usage = log.usage + response.usage
        log.cost_usd = None if cost is None or log.cost_usd is None else log.cost_usd + cost
        return step_log

    def _run_tool(self, tools: ToolBox, call: dict[str, Any], step_log: StepLog) -> dict[str, Any]:
        t0 = self._clock()
        result = tools.execute(call["name"], call["input"])
        step_log.tool_calls.append(
            ToolCallLog(
                name=call["name"],
                input=call["input"],
                is_error=result.is_error,
                latency_s=round(self._clock() - t0, 3),
                result=result.content[:RESULT_PREVIEW_CHARS],
            )
        )
        return {
            "type": "tool_result",
            "tool_use_id": call["id"],
            "content": result.content,
            "is_error": result.is_error,
        }
