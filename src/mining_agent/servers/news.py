"""mining-news-mcp — news aggregation for mining / mineral-rights research.

Tools
-----
``search(query, days, limit)``
    Keyword + recency ranked search over a news corpus.
``fetch_article(url)``
    Full text of a single article, for deeper reading.

Providers
---------
``offline``  bundled JSONL corpus (default; no network, no keys)
``newsapi``  NewsAPI.org ``/v2/everything`` (set ``NEWS_API_KEY``)
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import Settings
from .common import (
    ProviderError,
    configure_logging,
    configure_transport_security,
    get_logger,
    http_json,
    read_jsonl,
    reference_date,
)

log = get_logger("mining-news-mcp")

mcp = FastMCP(
    "mining-news-mcp",
    instructions=(
        "Mining and mineral-rights news. Use search() to find recent coverage for a "
        "region, commodity, project or ticker, then fetch_article() to read the full "
        "text of the most relevant hits."
    ),
)

_CJK = re.compile(r"[一-鿿]+")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.\-]*")

_PROJECT_ALIASES: dict[str, list[str]] = {
    "pilbara": ["Mt Pilbara Lithium Project", "Yarrie Ridge Lithium Project"],
    "lithium": ["Mt Pilbara Lithium Project", "Yarrie Ridge Lithium Project", "Coongan Gap Lithium Project"],
    "锂": ["Mt Pilbara Lithium Project", "Yarrie Ridge Lithium Project"],
    "锂矿": ["Mt Pilbara Lithium Project", "Yarrie Ridge Lithium Project"],
    "spodumene": ["Yarrie Ridge Lithium Project"],
    "hard rock": ["Mt Pilbara Lithium Project"],
}


# --------------------------------------------------------------------------
# query understanding
# --------------------------------------------------------------------------
def tokenize(query: str) -> list[str]:
    """Split a query into latin tokens plus CJK characters and bigrams.

    Bigrams let ``锂矿`` match ``锂矿项目`` without pulling in a full segmenter.
    """
    lowered = query.lower()
    tokens: set[str] = set(_TOKEN.findall(lowered))
    for run in _CJK.findall(lowered):
        tokens.update(run)
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return sorted(t for t in tokens if t)


def expand_projects(tokens: list[str]) -> set[str]:
    projects: set[str] = set()
    for token in tokens:
        projects.update(_PROJECT_ALIASES.get(token, []))
    return projects


def score_item(item: dict[str, Any], tokens: list[str], extra_projects: set[str]) -> float:
    title = str(item.get("title", "")).lower()
    summary = str(item.get("summary", "")).lower()
    body = str(item.get("body") or "").lower()
    tags = [str(t).lower() for t in item.get("tags", [])]
    projects = [str(p).lower() for p in item.get("projects", [])]
    tickers = [str(t).lower() for t in item.get("tickers", [])]

    score = 0.0
    for token in tokens:
        if token in title:
            score += 3.0
        if any(token in tag for tag in tags):
            score += 2.5
        if any(token in project for project in projects) or any(token in ticker for ticker in tickers):
            score += 2.5
        if token in summary:
            score += 1.5
        if token in body:
            score += 0.5

    for project in extra_projects:
        if project.lower() in projects:
            score += 2.0
        if project.lower() in title:
            score += 1.0
    return score


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------
class OfflineNewsProvider:
    """Ranked search over the bundled corpus."""

    name = "offline"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.fixtures_dir / "news_corpus.jsonl"

    def _corpus(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def search(self, query: str, days: int, limit: int) -> dict[str, Any]:
        items = self._corpus()
        if not items:
            raise ProviderError(f"offline news corpus not found: {self.path}")

        ref = reference_date(self.settings, "offline")
        cutoff = ref - timedelta(days=days)
        tokens = tokenize(query)
        projects = expand_projects(tokens)

        windowed, expanded = [], False
        for item in items:
            published = _parse_dt(item["published_at"]).date()
            if cutoff <= published <= ref:
                windowed.append(item)
        if not windowed:
            # Corpus is older than the requested window — widen it rather than
            # returning an empty briefing, and say so explicitly.
            expanded = True
            windowed = sorted(items, key=lambda i: str(i["published_at"]), reverse=True)[:limit]

        scored: list[dict[str, Any]] = []
        for item in windowed:
            hit = score_item(item, tokens, projects)
            age = max((ref - _parse_dt(item["published_at"]).date()).days, 0)
            recency = max(0.5 * (1 - age / max(days, 1)), 0.0)
            item = dict(item)
            item["published_at"] = _parse_dt(item["published_at"]).isoformat()
            item["score"] = round(hit + recency, 3)
            item.pop("body", None)  # bodies are fetched explicitly
            scored.append(item)

        matched = [i for i in scored if (i["score"] or 0) > 0.5]
        pool = matched or scored
        pool.sort(key=lambda i: (i["score"] or 0, str(i["published_at"])), reverse=True)

        return {
            "query": query,
            "days": days,
            "reference_date": ref.isoformat(),
            "window_expanded": expanded,
            "matched": bool(matched),
            "count": min(len(pool), limit),
            "results": pool[:limit],
            "source": f"offline corpus ({self.path.name})",
            "mode": "offline",
        }

    def fetch(self, url: str) -> dict[str, Any]:
        for item in self._corpus():
            if item.get("url") == url or item.get("id") == url:
                published = _parse_dt(item["published_at"]).isoformat()
                return {
                    "url": item["url"],
                    "title": item["title"],
                    "source": item["source"],
                    "published_at": published,
                    "body": item.get("body") or item.get("summary", ""),
                    "mode": "offline",
                }
        raise ProviderError(
            f"article not in the offline corpus: {url}. "
            "Call search() first, or set MINING_AGENT_DATA_MODE=live with NEWS_API_KEY."
        )


class NewsApiProvider:
    """Live search through NewsAPI.org."""

    name = "newsapi"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cache: dict[str, dict[str, Any]] = {}

    def search(self, query: str, days: int, limit: int) -> dict[str, Any]:
        ref = reference_date(self.settings, "newsapi")
        payload = http_json(
            self.settings.news_api_url,
            params={
                "q": query,
                "from": (ref - timedelta(days=days)).isoformat(),
                "to": ref.isoformat(),
                "sortBy": "publishedAt",
                "pageSize": min(limit, 100),
                "language": "en",
            },
            headers={"X-Api-Key": self.settings.news_api_key or ""},
        )
        results = []
        for article in payload.get("articles", [])[:limit]:
            item = {
                "id": article.get("url", ""),
                "title": article.get("title") or "",
                "summary": article.get("description") or "",
                "url": article.get("url") or "",
                "source": (article.get("source") or {}).get("name") or "unknown",
                "published_at": _parse_dt(article.get("publishedAt")).isoformat(),
                "tags": [],
                "projects": [],
                "tickers": [],
                "score": None,
            }
            self.cache[item["url"]] = item
            results.append(item)
        return {
            "query": query,
            "days": days,
            "reference_date": ref.isoformat(),
            "window_expanded": False,
            "matched": bool(results),
            "count": len(results),
            "results": results,
            "source": "newsapi.org",
            "mode": "live",
        }

    def fetch(self, url: str) -> dict[str, Any]:
        from .common import http_bytes

        try:
            raw = http_bytes(url, max_bytes=4 * 1024 * 1024).decode("utf-8", errors="replace")
        except (ProviderError, UnicodeDecodeError) as exc:
            raise ProviderError(f"could not download article: {exc}") from exc

        title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        body = re.sub(r"(?is)<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", raw)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        body = re.sub(r"&nbsp;?", " ", body)
        body = re.sub(r"&(amp|lt|gt|quot|#39);", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        cached = self.cache.get(url, {})
        return {
            "url": url,
            "title": (title.group(1).strip() if title else cached.get("title", url)),
            "source": cached.get("source", "web"),
            "published_at": cached.get("published_at"),
            "body": body[:20_000],
            "mode": "live",
        }


def _provider(settings: Settings) -> OfflineNewsProvider | NewsApiProvider:
    name = settings.resolve_news_provider()
    if name == "newsapi":
        if not settings.news_api_key:
            raise ProviderError("NEWS_API_KEY is not set but news provider is 'newsapi'")
        return NewsApiProvider(settings)
    return OfflineNewsProvider(settings)


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
@mcp.tool()
def search(query: str, days: int = 7, limit: int = 10) -> dict[str, Any]:
    """Search mining news published within the last `days` days.

    Args:
        query: Free-text query, e.g. "Pilbara lithium", "锂矿 增发", a project
            name, or a ticker such as "PLP.AX".
        days: Recency window in days (1-365).
        limit: Maximum number of articles to return (1-50).

    Returns:
        Ranked articles with title, summary, url, source and published_at.
        `reference_date` is the date the window was evaluated against and
        `window_expanded` is true when the corpus held nothing inside the
        requested window and the window was widened.
    """
    settings = Settings.from_env()
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 50))
    result = _provider(settings).search(query, days, limit)
    log.info("search q=%r days=%s -> %s hits (%s)", query, days, result["count"], result["mode"])
    return result


@mcp.tool()
def fetch_article(url: str) -> dict[str, Any]:
    """Fetch the full text of one article.

    Args:
        url: The `url` field of an item returned by `search`.
    """
    settings = Settings.from_env()
    result = _provider(settings).fetch(url)
    log.info("fetch_article %s -> %s chars", url, len(result.get("body") or ""))
    return result


# --------------------------------------------------------------------------
def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_json)
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        configure_transport_security(mcp)
        mcp.settings.host = os.getenv("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8001"))
    log.info(
        "mining-news-mcp starting (transport=%s provider=%s)",
        transport,
        settings.resolve_news_provider(),
    )
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
