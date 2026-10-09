"""Domain models shared by the servers, the agent and the report renderer.

These are deliberately plain pydantic models so that the same shape can be
serialised over MCP (JSON), consumed by the agent and rendered into Markdown
without a translation layer.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["Measured", "Indicated", "Inferred", "Total"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NewsItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    summary: str
    url: str
    source: str
    published_at: datetime
    tags: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)
    body: str | None = None
    score: float | None = None


class ResourceRow(BaseModel):
    """One line of a NI 43-101 / JORC mineral resource statement."""

    model_config = ConfigDict(extra="ignore")

    project: str
    category: Category
    raw_label: str | None = None            # label exactly as printed in the PDF
    commodity: str = "Li2O"
    tonnes_mt: float | None = None          # million tonnes of ore
    grade: float | None = None              # in `grade_unit`
    grade_unit: str = "%"
    contained_metal_t: float | None = None  # tonnes of contained metal (Li2O unless stated)
    contained_lce_kt: float | None = None   # thousand tonnes Li2CO3 equivalent
    effective_date: str | None = None
    source_page: int | None = None


class ResourceTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project: str
    commodity: str
    effective_date: str | None = None
    cut_off_grade: str | None = None
    rows: list[ResourceRow] = Field(default_factory=list)
    source: str = ""
    source_page: int | None = None


class PricePoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: date
    close: float


class PriceQuote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    commodity: str
    date: date
    close: float
    unit: str
    currency: str = "USD"
    source: str = ""
    mode: str = "offline"


class PriceTrend(BaseModel):
    model_config = ConfigDict(extra="ignore")

    commodity: str
    unit: str
    currency: str = "USD"
    days: int
    start: date
    end: date
    points: list[PricePoint] = Field(default_factory=list)
    latest: float
    change_pct: float
    change_abs: float
    high: float
    low: float
    volatility_pct: float = 0.0
    source: str = ""
    mode: str = "offline"

    def sparkline(self, width: int = 24) -> str:
        """Compact unicode sparkline — deterministic, no plotting dependency."""
        blocks = "▁▂▃▄▅▆▇█"
        values = [p.close for p in self.points]
        if not values:
            return ""
        if len(values) > width:  # downsample by averaging buckets
            step = len(values) / width
            buckets: list[float] = []
            for i in range(width):
                chunk = values[int(i * step): max(int((i + 1) * step), int(i * step) + 1)]
                buckets.append(sum(chunk) / len(chunk))
            values = buckets
        lo, hi = min(values), max(values)
        if hi == lo:
            return blocks[3] * len(values)
        span = hi - lo
        return "".join(blocks[min(int((v - lo) / span * (len(blocks) - 1)), len(blocks) - 1)] for v in values)


class RiskItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    severity: Literal["high", "medium", "low"] = "medium"
    detail: str
    evidence: list[int] = Field(default_factory=list)  # citation indices
    source: str = "rule-engine"  # rule-engine | llm


class Citation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    label: str
    url: str
    kind: str = "web"


class Narrative(BaseModel):
    """The LLM-authored part of the briefing (structured, never free-form)."""

    model_config = ConfigDict(extra="ignore")

    executive_summary: str = ""
    key_findings: list[str] = Field(default_factory=list)
    news_highlights: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)
    outlook: str = ""


class BriefingContext(BaseModel):
    """Everything the renderer needs. Produced by the orchestrator."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    query: str
    ticker: str = ""
    generated_at: datetime = Field(default_factory=utcnow)
    reference_date: date | None = None
    days: int = 7
    mode: str = "offline"
    planner: str = "deterministic"       # deterministic | react
    model: str | None = None
    news: list[NewsItem] = Field(default_factory=list)
    articles: list[dict[str, Any]] = Field(default_factory=list)
    resource_tables: list[ResourceTable] = Field(default_factory=list)
    price_trends: list[PriceTrend] = Field(default_factory=list)
    quotes: list[PriceQuote] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)
    narrative: Narrative = Field(default_factory=Narrative)
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
