"""Prompt caching: OpenAI's is on (per-agent cache keys), Anthropic's is gone.

The graph tests use a real ChatOpenAI whose HTTP transport is faked, so they
check the actual request bodies OpenAI would receive.
"""

from __future__ import annotations

import json
from unittest import mock

import deepagents.graph as deep_graph
import deepagents.middleware.subagents as deep_subagents
import httpx
import pytest
from deepagents.backends import StateBackend
from genai_agentic_sandbox.agent import create_jobhunter_agent
from genai_agentic_sandbox.caching import (
    OpenAIPromptCachingMiddleware,
    cache_stats,
    disable_anthropic_prompt_caching,
)
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI


def completion(message: dict, *, prompt_tokens: int = 2000, cached: int = 1536) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-5.5",
        "choices": [
            {"index": 0, "message": {"role": "assistant", **message}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 5,
            "total_tokens": prompt_tokens + 5,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


class FakeOpenAI:
    """httpx transport: records request bodies, replays scripted completions."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=self.replies.pop(0))

    def model(self) -> ChatOpenAI:
        return ChatOpenAI(
            model="gpt-5.5",
            api_key="sk-test",
            use_responses_api=False,
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(self)),
            http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(self)),
        )


def agent_for(fake: FakeOpenAI, **kwargs):
    return create_jobhunter_agent(
        model=fake.model(),
        backend=StateBackend(),
        browser_tools=[],
        resume_path="/input/cv.docx",
        memory=False,
        **kwargs,
    )


# -- the middleware ------------------------------------------------------------------


def request_for(model) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=[HumanMessage("hi")],
        system_message=None,
        tools=[],
        tool_choice=None,
        response_format=None,
        state={},
        runtime=None,
        model_settings={"x": 1},
    )


def test_sets_cache_key_and_retention_for_openai_models():
    seen = []
    mw = OpenAIPromptCachingMiddleware("resume-builder", retention="24h")
    mw.wrap_model_call(
        request_for(ChatOpenAI(model="gpt-5.5", api_key="x")), lambda r: seen.append(r)
    )
    assert seen[0].model_settings == {
        "x": 1,
        "prompt_cache_key": "jobhunter:resume-builder",
        "prompt_cache_retention": "24h",
    }


def test_retention_unset_by_default():
    seen = []
    OpenAIPromptCachingMiddleware("job-search").wrap_model_call(
        request_for(ChatOpenAI(model="gpt-5.5", api_key="x")), lambda r: seen.append(r)
    )
    assert "prompt_cache_retention" not in seen[0].model_settings


def test_other_providers_are_left_alone():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    seen = []
    request = request_for(GenericFakeChatModel(messages=iter([])))
    OpenAIPromptCachingMiddleware("x").wrap_model_call(request, lambda r: seen.append(r))
    assert seen[0] is request


def test_cache_stats_reads_usage_metadata():
    messages = [
        AIMessage(
            "a",
            usage_metadata={
                "input_tokens": 2000,
                "output_tokens": 5,
                "total_tokens": 2005,
                "input_token_details": {"cache_read": 1536},
            },
        ),
        AIMessage(
            "b", usage_metadata={"input_tokens": 1000, "output_tokens": 5, "total_tokens": 1005}
        ),
        HumanMessage("c"),
    ]
    assert cache_stats(messages) == {"calls": 2, "input_tokens": 3000, "cached_tokens": 1536}


# -- Anthropic caching removed ------------------------------------------------------------


def test_anthropic_caching_is_removed_from_every_agent():
    """Main agent, general-purpose and all four subagents: no AnthropicPromptCachingMiddleware."""
    built: list[tuple[str | None, list[str]]] = []

    def spy(real):
        def wrapper(*args, **kwargs):
            built.append(
                (kwargs.get("name"), [type(m).__name__ for m in kwargs.get("middleware", [])])
            )
            return real(*args, **kwargs)

        return wrapper

    with (
        mock.patch.object(deep_graph, "create_agent", spy(deep_graph.create_agent)),
        mock.patch.object(deep_subagents, "create_agent", spy(deep_subagents.create_agent)),
    ):
        agent_for(FakeOpenAI([]))
    names = {name for name, _ in built}
    assert {
        "job-search",
        "job-matcher",
        "resume-builder",
        "ats-reviewer",
        "general-purpose",
    } <= names
    for name, middleware in built:
        assert "AnthropicPromptCachingMiddleware" not in middleware, name
    ours = [name for name, mw in built if "OpenAIPromptCachingMiddleware" in mw]
    assert {"job-search", "job-matcher", "resume-builder", "ats-reviewer", "jobhunter"} <= set(ours)


def test_disable_is_idempotent():
    disable_anthropic_prompt_caching()
    disable_anthropic_prompt_caching()


# -- what OpenAI receives --------------------------------------------------------------


def tool_reply(name: str, args: dict, call_id: str) -> dict:
    return completion(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        }
    )


def test_each_agent_sends_its_own_cache_key():
    fake = FakeOpenAI(
        [
            # plan first: `task` is refused until this request's todos exist
            tool_reply(
                "write_todos",
                {"todos": [{"content": "1. score", "status": "in_progress"}]},
                "call_0",
            ),
            completion(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "task",
                                "arguments": json.dumps(
                                    {"subagent_type": "ats-reviewer", "description": "score it"}
                                ),
                            },
                        }
                    ],
                }
            ),
            completion({"content": "sub-agent done"}),  # ats-reviewer
            # complete the todo before answering
            tool_reply(
                "write_todos", {"todos": [{"content": "1. score", "status": "completed"}]}, "call_2"
            ),
            completion({"content": "all done"}),  # orchestrator
        ]
    )
    result = agent_for(fake, cache_retention="24h").invoke(
        {"messages": [{"role": "user", "content": "score my resume"}]}
    )
    assert result["messages"][-1].content == "all done"
    keys = [b.get("prompt_cache_key") for b in fake.bodies]
    assert keys == [
        "jobhunter:orchestrator",  # plans (todos)
        "jobhunter:orchestrator",  # delegates
        "jobhunter:ats-reviewer",
        "jobhunter:orchestrator",  # completes the todo
        "jobhunter:orchestrator",  # answers
    ]
    assert all(b.get("prompt_cache_retention") == "24h" for b in fake.bodies)
    assert "cache_control" not in json.dumps(fake.bodies)  # no Anthropic markers


def test_system_prompt_prefix_is_stable_between_calls():
    """OpenAI caches the longest identical prefix: the orchestrator's system prompt
    and tools must not change from one call to the next within a run."""
    fake = FakeOpenAI(
        [
            completion(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "write_todos",
                                "arguments": json.dumps(
                                    {"todos": [{"content": "a", "status": "completed"}]}
                                ),
                            },
                        }
                    ],
                }
            ),
            completion({"content": "done"}),
        ]
    )
    agent_for(fake).invoke({"messages": [{"role": "user", "content": "plan"}]})
    first, second = fake.bodies
    assert first["messages"][0] == second["messages"][0]  # system message
    assert first["tools"] == second["tools"]


def test_cached_tokens_come_back_as_usage():
    fake = FakeOpenAI([completion({"content": "hi"}, prompt_tokens=4096, cached=3072)])
    result = agent_for(fake).invoke({"messages": [{"role": "user", "content": "hi"}]})
    assert cache_stats(result["messages"]) == {
        "calls": 1,
        "input_tokens": 4096,
        "cached_tokens": 3072,
    }


@pytest.mark.parametrize("retention", [None, "in_memory"])
def test_retention_passthrough(retention):
    fake = FakeOpenAI([completion({"content": "hi"})])
    agent_for(fake, cache_retention=retention).invoke(
        {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert fake.bodies[0].get("prompt_cache_retention") == retention
