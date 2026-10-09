"""Server-level behaviour: news ranking, price maths, PDF extraction."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from mining_agent.config import Settings
from mining_agent.models import PriceTrend
from mining_agent.servers import lme_price, news
from mining_agent.servers.mineral_pdf import extract, load_document


# --------------------------------------------------------------------------
# mining-news-mcp
# --------------------------------------------------------------------------
def test_tokenize_splits_cjk_into_chars_and_bigrams():
    tokens = news.tokenize("Pilbara 锂矿")
    assert "pilbara" in tokens
    assert "锂" in tokens
    assert "锂矿" in tokens


def test_search_ranks_project_news_first():
    result = news.search("Pilbara lithium", days=7, limit=5)
    assert result["mode"] == "offline"
    assert result["count"] > 0
    titles = [item["title"] for item in result["results"]]
    assert any("Mt Pilbara Lithium" in t for t in titles)
    scores = [item["score"] for item in result["results"]]
    assert scores == sorted(scores, reverse=True)


def test_search_window_is_anchored_to_the_fixture_date():
    result = news.search("Pilbara", days=3, limit=10)
    reference = date.fromisoformat(result["reference_date"])
    for item in result["results"]:
        published = date.fromisoformat(item["published_at"][:10])
        assert reference - timedelta(days=3) <= published <= reference


def test_search_widens_the_window_instead_of_returning_nothing():
    result = news.search("Pilbara", days=1, limit=3)
    assert result["count"] > 0
    # The corpus has no item on the anchor date for a 1-day window, so the
    # server widens and says so rather than silently returning an empty list.
    assert isinstance(result["window_expanded"], bool)


def test_fetch_article_returns_body_and_rejects_unknown_url():
    url = news.search("Pilbara lithium", days=7, limit=1)["results"][0]["url"]
    article = news.fetch_article(url)
    assert article["title"]
    assert len(article["body"]) > 50

    with pytest.raises(Exception):
        news.fetch_article("https://not-in-the-corpus.example/x")


# --------------------------------------------------------------------------
# lme-price-mcp
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("lithium-carbonate", "lithium-carbonate"),
        ("Lithium Carbonate", "lithium-carbonate"),
        ("Li2CO3", "lithium-carbonate"),
        ("碳酸锂", "lithium-carbonate"),
        ("SC6", "spodumene"),
        ("spodumene concentrate", "spodumene"),
        ("Ni", "nickel"),
        ("铜", "copper"),
    ],
)
def test_normalize_commodity_aliases(raw, expected):
    assert lme_price.normalize_commodity(raw) == expected


def test_normalize_commodity_rejects_unknown():
    with pytest.raises(Exception):
        lme_price.normalize_commodity("unobtainium")


def test_get_price_clamps_to_the_series_and_reports_it():
    latest = lme_price.get_price("lithium-carbonate")
    # The fixture series runs to 2027-12-31; go well past it to force a clamp.
    far_future = (date.fromisoformat(latest["date"]) + timedelta(days=900)).isoformat()
    clamped = lme_price.get_price("lithium-carbonate", far_future)
    assert clamped["warnings"], "clamping must be disclosed, not silent"
    assert clamped["date"] < far_future
    assert clamped["requested_date"] == far_future


def test_get_trend_statistics_are_internally_consistent():
    payload = lme_price.get_trend("spodumene", 30)
    trend = PriceTrend.model_validate(payload)

    assert len(trend.points) >= 2
    assert trend.low <= trend.latest <= trend.high
    assert trend.low == min(p.close for p in trend.points)
    assert trend.high == max(p.close for p in trend.points)
    first = trend.points[0].close
    assert trend.change_abs == pytest.approx(trend.latest - first, abs=0.05)
    assert trend.change_pct == pytest.approx((trend.latest - first) / first * 100, abs=0.05)
    assert trend.volatility_pct > 0


def test_sparkline_is_deterministic_and_width_bounded():
    trend = PriceTrend.model_validate(lme_price.get_trend("copper", 90))
    line = trend.sparkline(width=20)
    assert line == trend.sparkline(width=20)
    assert len(line) == 20
    assert all(ch in "▁▂▃▄▅▆▇█" for ch in line)


def test_list_commodities_exposes_aliases_and_units():
    payload = lme_price.list_commodities()
    keys = {c["key"] for c in payload["commodities"]}
    assert {"lithium-carbonate", "spodumene", "nickel", "copper"} <= keys
    assert all(c["unit"] == "USD/t" for c in payload["commodities"])


# --------------------------------------------------------------------------
# mineral-pdf-mcp
# --------------------------------------------------------------------------
def test_extract_resources_from_the_fixture_pdf(sample_pdf):
    settings = Settings.from_env()
    payload_bytes, source = load_document(str(sample_pdf), settings)
    result = extract(payload_bytes, source, settings)

    assert result["strategy"] == "table"
    assert result["row_count"] == 10
    assert result["projects"] == [
        "Mt Pilbara Lithium Project",
        "Yarrie Ridge Lithium Project",
        "Coongan Gap Lithium Project",
    ]

    flagship = result["tables"][0]
    total = next(r for r in flagship["rows"] if r["raw_label"] == "Total")
    assert total["tonnes_mt"] == 99.3
    assert total["grade"] == 1.03
    assert total["contained_lce_kt"] == pytest.approx(2529.2, abs=1.0)
    # Per-project cut-off grades must not leak between properties.
    assert flagship["cut_off_grade"] == "0.40% Li2O"
    assert result["tables"][1]["cut_off_grade"] == "0.45% Li2O"


def test_extract_rejects_a_document_with_no_text():
    settings = Settings.from_env()
    with pytest.raises(Exception):
        extract(b"not a pdf at all", "broken.pdf", settings)


def test_list_documents_emits_usable_file_uris():
    from mining_agent.servers.mineral_pdf import _local_path

    settings = Settings.from_env()
    from mining_agent.servers.mineral_pdf import list_documents

    payload = list_documents()
    assert payload["count"] >= 1
    for doc in payload["documents"]:
        assert doc["pdf_url"].startswith("file://")
        assert _local_path(doc["pdf_url"], settings) is not None
