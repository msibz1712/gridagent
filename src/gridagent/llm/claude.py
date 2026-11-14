"""The real model: Claude via the official `anthropic` SDK (Messages API).

Notes for current models (Claude Sonnet 5.5 / Haiku 5.5):
- Thinking is adaptive and on by default; we don't send `thinking`.
  How much the model thinks is set with `output_config.effort`.
- `tool_choice` stays "auto" (forcing a tool returns HTTP 400 on Sonnet 5.5).
- Retries for 429 / 5xx / connection errors are done by the SDK itself
  (`max_retries`), with exponential backoff, so we don't reimplement them.
- Optional server-side refusal fallback (beta): if the safety classifier
  declines, the API re-runs the request on a fallback model in the same call.
"""

from typing import Any

import anthropic

from gridagent.llm.base import LLMError, ModelResponse, TokenUsage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models for which we enable `fallbacks: "default"` when asked to.
MODELS_WITH_SERVER_FALLBACK = {"claude-sonnet-5-5"}


class ClaudeLLM:
    """Calls Claude and converts the reply into our `ModelResponse`."""

    def __init__(
        self,
        model_name: str,
        max_tokens: int = 16000,
        effort: str = "medium",
        refusal_fallback: bool = True,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model_name = model_name
        self.max_tokens = max_tokens
        self.effort = effort
        self.use_fallback = refusal_fallback and model_name in MODELS_WITH_SERVER_FALLBACK
        # Reads ANTHROPIC_API_KEY from the environment.
        self.client = client or anthropic.Anthropic(
            timeout=timeout_seconds, max_retries=max_retries
        )

    def create(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelResponse:
        params: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            # Prompt caching, two breakpoints (render order: tools -> system -> messages):
            # 1. on the system prompt: tools + system are identical for every run,
            #    so back-to-back runs (e.g. an eval) read them from cache;
            # 2. automatic (top-level): caches the growing conversation, so later
            #    steps of the same run reuse the earlier steps.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": messages,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        try:
            if self.use_fallback:
                response = self.client.beta.messages.create(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                response = self.client.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Authentication failed: check ANTHROPIC_API_KEY.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Bad request to the model API: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Rate limited by the model API (after SDK retries).") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Model API error (HTTP {exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise LLMError(f"Could not reach the model API: {exc}") from exc

        return to_model_response(response)


def to_model_response(response: Any) -> ModelResponse:
    """Convert an SDK Message (or BetaMessage) into plain, loggable data."""
    usage = response.usage
    return ModelResponse(
        # Plain dicts in API shape: safe to log and to send back unchanged.
        content=[block.model_dump(mode="json", exclude_none=True) for block in response.content],
        stop_reason=response.stop_reason or "unknown",
        usage=TokenUsage(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
        ),
        model=response.model,
        request_id=getattr(response, "_request_id", None),
    )
