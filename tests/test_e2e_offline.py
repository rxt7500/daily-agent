"""End-to-end: the real agent launches the three real MCP servers over stdio."""

from __future__ import annotations

import asyncio
import json

import pytest

from mining_agent.config import Settings
from mining_agent.mcp_client import McpHub, default_server_specs
from mining_agent.orchestrator import run_briefing
from mining_agent.report import render

QUERY = "给我生成一份关于 Pilbara 锂矿的今日简报"

EXPECTED_TOOLS = {
    "mining-news-mcp__search",
    "mining-news-mcp__fetch_article",
    "mineral-pdf-mcp__extract_resources",
    "mineral-pdf-mcp__list_documents",
    "lme-price-mcp__get_price",
    "lme-price-mcp__get_trend",
    "lme-price-mcp__list_commodities",
}


@pytest.fixture(scope="module")
def settings() -> Settings:
    import dataclasses

    return dataclasses.replace(Settings.from_env(), data_mode="offline", llm_enabled=False)


def test_hub_discovers_every_tool_over_stdio(settings):
    async def scenario():
        async with McpHub(default_server_specs(settings)) as hub:
            assert set(hub.servers()) == {
                "mining-news-mcp",
                "mineral-pdf-mcp",
                "lme-price-mcp",
            }
            names = {tool.qualified for tool in hub.tools}
            assert EXPECTED_TOOLS <= names, EXPECTED_TOOLS - names
            # Anthropic requires tool names to match ^[a-zA-Z0-9_-]{1,128}$
            for name in names:
                assert name.replace("_", "").replace("-", "").isalnum(), name
            return await hub.call("lme-price-mcp__get_price", {"commodity": "spodumene"})

    result = asyncio.run(scenario())
    assert result.ok and result.data["close"] > 0


def test_unknown_tool_and_bad_arguments_surface_as_errors(settings):
    async def scenario():
        async with McpHub(default_server_specs(settings)) as hub:
            with pytest.raises(Exception):
                await hub.call("does-not-exist__tool", {})
            bad = await hub.call("lme-price-mcp__get_price", {"commodity": "unobtainium"})
            missing = await hub.call("mining-news-mcp__fetch_article", {"url": "nope"})
            return bad, missing

    bad, missing = asyncio.run(scenario())
    assert not bad.ok and not missing.ok


def test_full_briefing_pipeline_offline(settings, tmp_path):
    async def scenario():
        async with McpHub(default_server_specs(settings)) as hub:
            return await run_briefing(settings, hub, QUERY, days=7, prefer_llm=False)

    ctx = asyncio.run(scenario())

    # Data actually flowed from all three servers
    assert ctx.planner == "deterministic"
    assert ctx.mode == "offline"
    assert ctx.reference_date is not None
    assert len(ctx.news) >= 3
    assert len(ctx.resource_tables) == 3
    assert sum(len(t.rows) for t in ctx.resource_tables) == 10
    assert {t.commodity for t in ctx.price_trends} == {"lithium-carbonate", "spodumene"}
    assert all(len(t.points) > 5 for t in ctx.price_trends)

    # Every citation resolves, and the risk section is populated
    assert ctx.citations and all(c.index > 0 and c.label for c in ctx.citations)
    assert ctx.risks

    markdown = render(ctx)
    assert "## 三、资源储量" in markdown
    assert "2,529.2" in markdown  # thousands-separated, not the raw 2529.2

    out = tmp_path / "briefing.md"
    out.write_text(markdown, encoding="utf-8")
    assert out.stat().st_size > 2000

    # The JSON view must round-trip
    payload = json.loads(ctx.model_dump_json())
    assert payload["query"] == QUERY


def test_briefing_is_reproducible_for_the_same_inputs(settings):
    async def scenario():
        async with McpHub(default_server_specs(settings)) as hub:
            return await run_briefing(settings, hub, QUERY, days=7, prefer_llm=False)

    first = asyncio.run(scenario())
    second = asyncio.run(scenario())

    assert [n.url for n in first.news] == [n.url for n in second.news]
    assert [t.latest for t in first.price_trends] == [t.latest for t in second.price_trends]
    assert [r.title for r in first.risks] == [r.title for r in second.risks]
