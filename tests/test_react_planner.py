"""The ReAct loop, driven by a stubbed Anthropic client.

These tests exercise the loop's real control flow — tool dispatch, citation
injection, ``submit_briefing`` capture, refusal handling, iteration exhaustion —
without spending tokens or needing a key.  The MCP hub underneath is the real
one, so tool calls actually reach the three servers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest

from mining_agent import orchestrator
from mining_agent.config import Settings
from mining_agent.mcp_client import McpHub, default_server_specs
from mining_agent.orchestrator import ReActPlanner, run_briefing


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------
def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_use(block_id: str, name: str, payload: dict):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=payload)


def message(stop_reason: str, content: list, *, input_tokens: int = 100, output_tokens: int = 20):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
        ),
        stop_details=None,
    )


class ScriptedMessages:
    """Returns queued responses; records every request body it was handed."""

    def __init__(self, script: list, requests: list, label: str) -> None:
        self.script = script
        self.requests = requests
        self.label = label

    async def create(self, **kwargs):
        # Snapshot `messages` — the loop mutates its list in place, so recording
        # it by reference would make every request look like the final state.
        recorded = {"endpoint": self.label, **kwargs}
        recorded["messages"] = [dict(m) for m in kwargs.get("messages", [])]
        self.requests.append(recorded)
        if not self.script:
            raise AssertionError(f"{self.label} called more times than the script allows")
        return self.script.pop(0)


class ScriptedClient:
    def __init__(self, script: list) -> None:
        self.requests: list[dict] = []
        self.messages = ScriptedMessages(script, self.requests, "messages")
        self.beta = SimpleNamespace(messages=ScriptedMessages(script, self.requests, "beta"))


@pytest.fixture(scope="module")
def settings() -> Settings:
    return dataclasses.replace(
        Settings.from_env(),
        data_mode="offline",
        llm_enabled=True,
        anthropic_api_key="sk-ant-stub",
        max_iterations=4,
    )


def _run(settings: Settings, script: list, monkeypatch, *, query="Pilbara 锂矿", days=7):
    client = ScriptedClient(script)
    monkeypatch.setattr(orchestrator.anthropic, "AsyncAnthropic", lambda **_: client)

    async def scenario():
        async with McpHub(default_server_specs(settings)) as hub:
            ctx = await run_briefing(settings, hub, query, days, prefer_llm=True)
            return ctx, client

    return asyncio.run(scenario())


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------
def test_react_loop_executes_mcp_tools_then_captures_the_briefing(settings, monkeypatch):
    script = [
        message(
            "tool_use",
            [
                text_block("先搜索新闻。"),
                tool_use("tu_1", "mining-news-mcp__search", {"query": "Pilbara", "days": 7, "limit": 3}),
                tool_use("tu_2", "lme-price-mcp__get_trend", {"commodity": "碳酸锂", "days": 30}),
            ],
        ),
        message(
            "tool_use",
            [
                tool_use(
                    "tu_3",
                    "submit_briefing",
                    {
                        "executive_summary": "锂价企稳，Mt Pilbara 资源量上修。[1]",
                        "key_findings": ["资源量 99.3 Mt @ 1.03% Li2O [1]"],
                        "news_highlights": [{"citation_index": 1, "note": "资源量上修"}],
                        "risks": [
                            {
                                "title": "价格波动",
                                "severity": "high",
                                "detail": "30 天区间震荡",
                                "evidence": [1],
                            }
                        ],
                        "outlook": "跟踪碳酸锂现货价。",
                    },
                )
            ],
        ),
    ]

    ctx, client = _run(settings, script, monkeypatch)

    # The loop used the ReAct planner, not the deterministic fallback.
    assert ctx.planner == "react"
    assert ctx.model == settings.model
    assert not any("回退" in w for w in ctx.warnings)

    # Both MCP tools actually ran against the live servers.
    called = [c["tool"] for c in ctx.tool_calls]
    assert "mining-news-mcp__search" in called
    assert "lme-price-mcp__get_trend" in called
    assert all(c["ok"] for c in ctx.tool_calls)
    assert len(ctx.news) >= 1
    assert ctx.price_trends and ctx.price_trends[0].commodity == "lithium-carbonate"

    # The narrative came from submit_briefing, risks included.
    assert "Mt Pilbara" in ctx.narrative.executive_summary
    assert [r.title for r in ctx.risks] == ["价格波动"]
    assert ctx.risks[0].severity == "high"

    # Token accounting accumulated across both turns.
    assert ctx.usage["input_tokens"] == 200
    assert ctx.usage["output_tokens"] == 40

    # Server-side refusal fallbacks were requested on the beta endpoint.
    assert client.requests[0]["endpoint"] == "beta"
    assert client.requests[0]["betas"] == ["server-side-fallback-2026-07-01"]
    assert client.requests[0]["fallbacks"] == "default"
    assert client.requests[0]["thinking"] == {"type": "adaptive"}
    assert client.requests[0]["output_config"] == {"effort": settings.effort}
    # Turn 1 was sent with only the opening user prompt.
    assert [m["role"] for m in client.requests[0]["messages"]] == ["user"]

    # Tool results were fed back with citation numbers attached, and every
    # tool_use got exactly one matching tool_result.
    assert [m["role"] for m in client.requests[1]["messages"]] == ["user", "assistant", "user"]
    follow_up = client.requests[1]["messages"][-1]
    assert follow_up["role"] == "user"
    results = [b for b in follow_up["content"] if b["type"] == "tool_result"]
    assert {r["tool_use_id"] for r in results} == {"tu_1", "tu_2"}
    search_result = next(r for r in results if r["tool_use_id"] == "tu_1")
    payload = json.loads(search_result["content"])
    assert "results" in payload and "_new_citations" in payload
    assert payload["_new_citations"], "the model must be told which citation numbers it just earned"


def test_submit_briefing_is_never_dispatched_to_an_mcp_server(settings, monkeypatch):
    script = [
        message("tool_use", [tool_use("tu_1", "submit_briefing", {"executive_summary": "s", "key_findings": []})]),
    ]
    ctx, _ = _run(settings, script, monkeypatch)
    assert ctx.tool_calls == []           # nothing was sent to a server
    assert ctx.planner == "react"
    assert ctx.narrative.executive_summary == "s"


# --------------------------------------------------------------------------
# failure and boundary paths
# --------------------------------------------------------------------------
def test_refusal_stop_reason_degrades_to_the_deterministic_planner(settings, monkeypatch):
    script = [message("refusal", [])]
    ctx, _ = _run(settings, script, monkeypatch)

    assert ctx.planner == "deterministic"
    assert any("回退" in w for w in ctx.warnings)
    assert ctx.resource_tables and ctx.price_trends   # the fallback still did the work


def test_pause_turn_is_resumed_rather_than_treated_as_final(settings, monkeypatch):
    script = [
        message("pause_turn", [text_block("pausing")]),
        message(
            "tool_use",
            [tool_use("tu_1", "submit_briefing", {"executive_summary": "恢复后完成", "key_findings": []})],
        ),
    ]
    ctx, client = _run(settings, script, monkeypatch)
    assert ctx.planner == "react"
    assert ctx.narrative.executive_summary == "恢复后完成"
    assert len(client.requests) == 2


def test_running_out_of_iterations_still_yields_a_briefing(settings, monkeypatch):
    # Four turns that never submit, matching settings.max_iterations.
    script = [
        message("tool_use", [tool_use(f"tu_{i}", "lme-price-mcp__get_price", {"commodity": "nickel"})])
        for i in range(settings.max_iterations)
    ]
    ctx, _ = _run(settings, script, monkeypatch)

    assert ctx.planner == "react"
    assert any("最大工具调用轮次" in w for w in ctx.warnings)
    # The rule-based narrative filled the gap.
    assert ctx.narrative.executive_summary
    assert ctx.risks


def test_missing_api_key_uses_the_deterministic_planner(settings, monkeypatch):
    no_key = dataclasses.replace(settings, anthropic_api_key=None)
    called = {"n": 0}

    def explode(**_):
        called["n"] += 1
        raise AssertionError("no API client should be constructed without a key")

    monkeypatch.setattr(orchestrator.anthropic, "AsyncAnthropic", explode)

    async def scenario():
        async with McpHub(default_server_specs(no_key)) as hub:
            return await run_briefing(no_key, hub, "Pilbara 锂矿", 7, prefer_llm=True)

    ctx = asyncio.run(scenario())
    assert called["n"] == 0
    assert ctx.planner == "deterministic"
    assert any("ANTHROPIC_API_KEY" in w for w in ctx.warnings)


def test_planner_sends_the_distilled_subject_to_the_model(settings, monkeypatch):
    captured = {}

    class Capturing(ReActPlanner):
        def _user_prompt(self) -> str:
            captured["prompt"] = super()._user_prompt()
            return captured["prompt"]

    script = [
        message(
            "tool_use",
            [tool_use("tu_1", "submit_briefing", {"executive_summary": "ok", "key_findings": []})],
        )
    ]
    client = ScriptedClient(script)
    monkeypatch.setattr(orchestrator.anthropic, "AsyncAnthropic", lambda **_: client)

    async def scenario():
        async with McpHub(default_server_specs(settings)) as hub:
            runner = orchestrator.BriefingRunner(settings, hub, "给我生成一份关于 Pilbara 锂矿的今日简报", 7)
            return await Capturing(runner).run()

    ctx = asyncio.run(scenario())
    assert "Pilbara 锂矿" in captured["prompt"]
    assert ctx.planner == "react"
