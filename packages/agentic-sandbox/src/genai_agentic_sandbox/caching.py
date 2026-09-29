"""Prompt caching: OpenAI's, not Anthropic's.

`create_deep_agent` appends `AnthropicPromptCachingMiddleware` to every agent
unconditionally (a no-op for other providers). This project runs on OpenAI, so
`disable_anthropic_prompt_caching()` removes it through a harness profile for the
`openai` provider - the documented way to drop middleware from the stack.

OpenAI caches prompts itself: any request of 1024+ tokens whose beginning
(tools + system prompt + earlier messages) matches a recent request is served
from cache - cheaper and faster, no code needed. What code can do:

- `prompt_cache_key`: requests with the same key and prefix are routed to the
  same cache. Each agent here has its own system prompt and tools, so each gets
  its own key (`jobhunter:<agent>`) - keeping unrelated prefixes from competing.
- `prompt_cache_retention`: `"24h"` keeps cached prefixes up to a day (supported
  models only); default is in-memory (minutes).
- A stable prefix: system prompts, skills lists and tools do not change between
  calls; memory is appended at the END of the system prompt, so updating memory
  only invalidates the tail.

`OpenAIPromptCachingMiddleware` sets the key/retention on every OpenAI model
call; `cache_stats` reads the cached-token counts OpenAI returns.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_openai.chat_models.base import BaseChatOpenAI

CacheRetention = Literal["in_memory", "24h"]
CACHE_KEY_PREFIX = "jobhunter"


class OpenAIPromptCachingMiddleware(AgentMiddleware):
    """Route an agent's OpenAI calls to its own prompt cache.

    Args:
        agent: Agent name; the cache key is `jobhunter:<agent>`.
        retention: `"24h"` for extended caching on models that support it,
            `"in_memory"` (OpenAI's default) or None to leave it unset.
    """

    def __init__(self, agent: str, retention: CacheRetention | None = None) -> None:
        super().__init__()
        self.cache_key = f"{CACHE_KEY_PREFIX}:{agent}"
        self.retention = retention

    def _apply(self, request: ModelRequest) -> ModelRequest:
        if not isinstance(request.model, BaseChatOpenAI):
            return request  # other providers: nothing to do
        settings = {**request.model_settings, "prompt_cache_key": self.cache_key}
        if self.retention is not None:
            settings["prompt_cache_retention"] = self.retention
        return request.override(model_settings=settings)

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> Any:
        return handler(self._apply(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> Any:
        return await handler(self._apply(request))


def disable_anthropic_prompt_caching(provider: str = "openai") -> None:
    """Drop `AnthropicPromptCachingMiddleware` from deep agents on `provider` models.

    Registers (merges into) the provider's harness profile; applies to the main
    agent, the general-purpose subagent and every declared subagent. Call before
    `create_deep_agent`. Idempotent.
    """
    from deepagents import HarnessProfile, register_harness_profile
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

    register_harness_profile(
        provider, HarnessProfile(excluded_middleware=frozenset({AnthropicPromptCachingMiddleware}))
    )


def cache_stats(messages: Iterable[Any]) -> dict[str, int]:
    """Sum input and cached input tokens over AI messages' `usage_metadata`."""
    total = cached = calls = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            continue
        calls += 1
        total += usage.get("input_tokens", 0) or 0
        cached += (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
    return {"calls": calls, "input_tokens": total, "cached_tokens": cached}


__all__ = [
    "CACHE_KEY_PREFIX",
    "CacheRetention",
    "OpenAIPromptCachingMiddleware",
    "cache_stats",
    "disable_anthropic_prompt_caching",
]
