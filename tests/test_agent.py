"""Evidence registry, risk engine, orchestrator helpers and the renderer."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from mining_agent.evidence import EvidenceStore
from mining_agent.models import (
    BriefingContext,
    Citation,
    Narrative,
    NewsItem,
    PricePoint,
    PriceQuote,
    PriceTrend,
    ResourceRow,
    ResourceTable,
    RiskItem,
)
from mining_agent.orchestrator import distill_query, infer_commodities
from mining_agent.report import render, suggested_filename
from mining_agent.risks import assess_news, assess_resources, build_risks


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------
def test_citations_are_deduplicated_by_kind_and_url():
    store = EvidenceStore()
    first = store.add("news", "Headline A", "https://example.test/a")
    again = store.add("news", "Headline A (different label)", "https://example.test/a")
    other = store.add("document", "Report", "file:///tmp/r.pdf")

    assert first == again == 1
    assert other == 2
    assert len(store.citations()) == 2


def test_harvest_registers_every_source_in_a_tool_result():
    store = EvidenceStore()
    found = store.harvest(
        "search",
        {
            "results": [
                {"title": "A", "url": "https://example.test/a"},
                {"title": "B", "url": "https://example.test/b"},
                {"title": "no url"},
            ]
        },
    )
    assert found == [1, 2]
    assert store.summary() == {"1": "A", "2": "B"}


def test_harvest_ignores_unrelated_tool_payloads():
    store = EvidenceStore()
    assert store.harvest("list_commodities", {"commodities": [{"key": "copper"}]}) == []
    assert store.citations() == []


# --------------------------------------------------------------------------
# risk engine
# --------------------------------------------------------------------------
def _news(title: str, summary: str = "", url: str = "https://example.test/n") -> NewsItem:
    return NewsItem(
        id=url,
        title=title,
        summary=summary,
        url=url,
        source="Test Wire",
        published_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    ("title", "expect_theme"),
    [
        ("Mt Pilbara expansion clears state environmental approval with conditions", "许可与合规"),
        ("Pilbara Lithium trims FY27 shipment guidance on softer demand", "指引下调"),
        ("Operations halted pending regulator review", "运营中断"),
        ("Community protest blocks access road", "社区与土地权"),
    ],
)
def test_news_risk_themes(title, expect_theme):
    store = EvidenceStore()
    store.add("news", title, "https://example.test/n")
    risks = assess_news([_news(title)], store)
    assert [r.title for r in risks] == [f"{expect_theme}（新闻面）"]


def test_neutral_copy_does_not_trigger_a_guidance_downgrade():
    """'executed' contains 'cut' and 'revised closure plan' contains 'revis'."""
    store = EvidenceStore()
    items = [
        _news(
            "Westralian Resources raises A$180m placement to fund Yarrie Ridge",
            "The company executed a binding agreement and adopted a revised closure plan.",
        )
    ]
    themes = [r.title for r in assess_news(items, store)]
    assert "指引下调（新闻面）" not in themes


def test_inferred_heavy_resource_raises_a_high_severity_risk():
    store = EvidenceStore()
    store.add("document", "Coongan Gap — NI 43-101 resource statement", "file:///tmp/x.pdf")
    table = ResourceTable(
        project="Coongan Gap Lithium Project",
        commodity="Li2O",
        source="file:///tmp/x.pdf",
        rows=[
            ResourceRow(project="CG", category="Inferred", raw_label="Inferred",
                        tonnes_mt=11.2, grade=0.86, contained_lce_kt=238.2),
            ResourceRow(project="CG", category="Total", raw_label="Total",
                        tonnes_mt=11.2, grade=0.86, contained_lce_kt=238.2),
        ],
    )
    risks = assess_resources([table], store)
    titles = {r.title: r.severity for r in risks}
    assert titles["Coongan Gap Lithium Project 无 Measured/Indicated 资源"] == "high"
    assert all(r.evidence == [1] for r in risks)


def test_freshness_warns_when_the_fixture_anchor_is_old():
    risks = build_risks(
        news=[],
        tables=[],
        trends=[],
        evidence=EvidenceStore(),
        reference=date.today() - timedelta(days=200),
        today=date.today(),
        mode="offline",
        provider_summary={"news_provider": "offline", "price_provider": "offline", "pdf_provider": "offline"},
    )
    titles = [r.title for r in risks]
    assert "数据时效性" in titles
    assert "数据来源为离线夹具" in titles


# --------------------------------------------------------------------------
# orchestrator helpers
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("给我生成一份关于 Pilbara 锂矿的今日简报", "Pilbara 锂矿"),
        ("帮我看看今天锂矿的新闻", "锂矿"),
        ("Pilbara lithium", "Pilbara lithium"),
    ],
)
def test_distill_query_drops_instruction_wording(query, expected):
    assert distill_query(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Pilbara 锂矿", ["lithium-carbonate", "spodumene"]),
        ("copper outlook", ["copper"]),
        ("nickel supply", ["nickel"]),
        ("something else", ["lithium-carbonate"]),
    ],
)
def test_infer_commodities(query, expected):
    assert infer_commodities(query) == expected


# --------------------------------------------------------------------------
# report renderer
# --------------------------------------------------------------------------
def _context() -> BriefingContext:
    published = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
    news = NewsItem(
        id="n1", title="Mt Pilbara Lithium lifts total resource",
        summary="Resource upgrade to 99.3 Mt.", url="https://example.test/a",
        source="Test Wire", published_at=published,
    )
    table = ResourceTable(
        project="Mt Pilbara Lithium Project", commodity="Li2O",
        effective_date="30 September 2026", cut_off_grade="0.40% Li2O",
        source="file:///tmp/report.pdf",
        rows=[
            ResourceRow(project="Mt Pilbara", category="Indicated", raw_label="Indicated",
                        tonnes_mt=54.2, grade=1.03, contained_metal_t=558_260, contained_lce_kt=1380.5),
            ResourceRow(project="Mt Pilbara", category="Total", raw_label="Total",
                        tonnes_mt=99.3, grade=1.03, contained_metal_t=1_022_790, contained_lce_kt=2529.2),
        ],
    )
    trend = PriceTrend(
        commodity="lithium-carbonate", unit="USD/t", days=30,
        start=date(2026, 9, 8), end=date(2026, 10, 8),
        points=[PricePoint(date=date(2026, 9, 8) + timedelta(days=i), close=8000 + i * 10) for i in range(30)],
        latest=8290.0, change_pct=3.63, change_abs=290.0, high=8290.0, low=8000.0,
        volatility_pct=1.2, source="fixture", mode="offline",
    )
    return BriefingContext(
        query="Pilbara 锂矿",
        generated_at=datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc),
        reference_date=date(2026, 10, 8),
        days=7,
        mode="offline",
        planner="deterministic",
        news=[news],
        resource_tables=[table],
        price_trends=[trend],
        quotes=[PriceQuote(commodity="lithium-carbonate", date=date(2026, 10, 8), close=8290.0,
                           unit="USD/t", source="fixture", mode="offline")],
        risks=[RiskItem(title="价格波动", severity="high", detail="区间波动加大", evidence=[1])],
        narrative=Narrative(
            executive_summary="摘要内容 [1]",
            key_findings=["发现一 [1]"],
            news_highlights=[{"citation_index": 1, "note": "要点"}],
            outlook="跟踪锂价。",
        ),
        citations=[
            Citation(index=1, label=news.title, url=news.url, kind="news"),
            # Matches the label EvidenceStore.harvest() builds for documents.
            Citation(index=2, label="Mt Pilbara Lithium Project — NI 43-101 resource statement",
                     url="file:///tmp/report.pdf", kind="document"),
        ],
        tool_calls=[{"tool": "mining-news-mcp__search", "arguments": {"days": 7}, "ok": True}],
        warnings=["数据窗口已扩大"],
    )


def test_render_produces_every_required_section():
    markdown = render(_context())
    for heading in [
        "# 矿权日报",
        "## 一、摘要",
        "## 二、新闻动态",
        "## 三、资源储量",
        "## 四、价格走势",
        "## 五、风险提示",
        "## 六、观察点",
        "## 七、引用源",
        "## 八、运行记录",
        "免责声明",
    ]:
        assert heading in markdown, heading


def test_render_keeps_citation_numbers_and_links_consistent():
    markdown = render(_context())
    assert "[Mt Pilbara Lithium lifts total resource](https://example.test/a)" in markdown
    assert "| [1] |" in markdown                              # inline marker in the news table
    assert "1. `新闻`" in markdown                            # reference list resolves [1]
    assert "2. `技术报告`" in markdown
    assert "Mt Pilbara Lithium Project — NI 43-101 resource statement" in markdown
    assert "▁" in markdown or "█" in markdown                 # sparkline rendered


def test_render_survives_a_completely_empty_context():
    markdown = render(BriefingContext(query="空"))
    assert "# 矿权日报 · 空" in markdown
    assert "本次运行未检索到相关新闻" in markdown
    assert "未识别到显著风险信号" in markdown


def test_suggested_filename_is_filesystem_safe():
    name = suggested_filename(_context())
    assert name.endswith(".md")
    assert "/" not in name and "\\" not in name and ":" not in name
