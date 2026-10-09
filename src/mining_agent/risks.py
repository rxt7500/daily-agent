"""Rule-based risk engine.

Runs on the collected evidence and produces :class:`RiskItem` objects with a
severity and the citation numbers that support them.  It deliberately does not
call a model: the same risk section is produced whether the briefing was written
by the ReAct agent or by the deterministic planner, and every claim is traceable
to a number that came out of a tool.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable

from .evidence import EvidenceStore
from .models import NewsItem, PriceTrend, ResourceTable, RiskItem

# Headline/summary language that maps onto a named risk.  Every alternative is
# word-bounded: without it "cut" matches inside "executed" and every offtake
# announcement gets filed as a guidance downgrade.
RISK_KEYWORDS: list[tuple[str, str, str]] = [
    (r"\b(?:halt|halts|halted|suspend|suspended|suspension|shut\s?down|force majeure)\b", "运营中断", "high"),
    (r"\b(?:delay|delayed|delay(?:s|ed)?|postpone[ds]?|defer(?:red|s)?|slip(?:ped|s)?)\b", "项目延期", "medium"),
    (r"\b(?:downtime|outage|breakdown|unplanned|disruption)\b", "非计划停产", "medium"),
    # Bare "cut"/"revised" appear constantly in neutral copy ("a revised closure
    # plan", "cut-off grade"), so the verb form only counts next to an outlook noun.
    (
        r"\b(?:guidance|downgrade[ds]?|profit warning|earnings warning)\b"
        r"|\b(?:cuts?|trims?|trimmed|lower(?:ed|s)?|revis(?:e|ed|es))\s+"
        r"(?:\w+\s+){0,2}(?:guidance|forecast|outlook|target|estimates?|production|shipments?)\b",
        "指引下调",
        "medium",
    ),
    (r"\b(?:lawsuit|litigation|dispute[ds]?|arbitration|legal action)\b", "法律纠纷", "medium"),
    (r"\b(?:strike|strikes|industrial action|union)\b", "劳资行动", "medium"),
    (r"\b(?:appeal(?:s|ed)?|objection|challenge[ds]?|judicial review)\b", "审批申诉", "medium"),
    (r"\b(?:permit(?:s|ting)?|approvals?|approved|licen[cs]es?|regulator|regulatory|conditions?|water)\b", "许可与合规", "medium"),
    (r"\b(?:royalt(?:y|ies)|tax(?:es|ation)?|polic(?:y|ies)|consultation|regulation(?:s)?)\b", "政策与税费", "low"),
    (r"\b(?:protest(?:s|ers)?|community|native title|heritage|land access)\b", "社区与土地权", "medium"),
    (r"\b(?:financing|placement|capital raise|dilution|debt|raise[sd]?)\b", "融资与摊薄", "low"),
    (r"\b(?:m&a|takeover|consolidat(?:e|es|ed|ion)|merger|acquisition)\b", "并购与整合", "low"),
    (r"\b(?:congestion|logistics|port|shipping|rail|queue|vessel(?:s)?)\b", "物流与运输", "low"),
    (r"\b(?:power|transmission|grid|energy)\b", "电力与基础设施", "low"),
    (r"\b(?:cost(?:s)?|inflation|margin(?:s)?|capital expenditure|capex|cost pressure)\b", "成本压力", "medium"),
]

_COMPILED = [(re.compile(p, re.I), label, sev) for p, label, sev in RISK_KEYWORDS]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _pct(value: float) -> str:
    return f"{value:+.1f}%"


def assess_prices(
    trends: Iterable[PriceTrend],
    evidence: EvidenceStore,
) -> list[RiskItem]:
    items: list[RiskItem] = []
    for trend in trends:
        cite = [
            c.index
            for c in evidence.citations()
            if c.kind == "data" and trend.commodity in c.label
        ][:1]

        if abs(trend.change_pct) >= 12:
            items.append(
                RiskItem(
                    title=f"{trend.commodity} 价格大幅波动",
                    severity="high" if abs(trend.change_pct) >= 20 else "medium",
                    detail=(
                        f"近 {trend.days} 天 {trend.commodity} 收于 {trend.latest:,.0f} {trend.unit}，"
                        f"区间变动 {_pct(trend.change_pct)}"
                        f"（区间 {trend.low:,.0f}–{trend.high:,.0f}）。"
                        "价格波动直接影响项目现金流与估值假设。"
                    ),
                    evidence=cite,
                )
            )
        if trend.volatility_pct >= 2.0:
            items.append(
                RiskItem(
                    title=f"{trend.commodity} 波动率偏高",
                    severity="medium",
                    detail=(
                        f"近 {trend.days} 天日收益率标准差为 {trend.volatility_pct:.2f}%，"
                        "短期定价不确定性较高，套保与承购定价条款需重新评估。"
                    ),
                    evidence=cite,
                )
            )
    return items


def assess_resources(
    tables: Iterable[ResourceTable],
    evidence: EvidenceStore,
) -> list[RiskItem]:
    items: list[RiskItem] = []
    for table in tables:
        by_label = {row.raw_label or row.category: row for row in table.rows}
        total = next(
            (r for r in table.rows if r.category == "Total" and (r.raw_label or "").strip().lower() == "total"),
            None,
        )
        if total is None:
            totals = [r for r in table.rows if r.category == "Total"]
            total = totals[-1] if totals else (table.rows[-1] if table.rows else None)
        if total is None or not total.contained_lce_kt:
            continue

        # Matching on the resolved document URI is exact; matching on the label
        # is not, because the citation label is rewritten by the harvester.
        cite = [
            c.index
            for c in evidence.citations()
            if c.kind == "document" and c.url == table.source
        ][:1]

        inferred = next((r for r in table.rows if r.category == "Inferred"), None)
        if inferred and inferred.contained_lce_kt:
            share = inferred.contained_lce_kt / total.contained_lce_kt
            if share >= 0.25:
                items.append(
                    RiskItem(
                        title=f"{table.project} 资源类别偏低",
                        severity="medium" if share < 0.4 else "high",
                        detail=(
                            f"Inferred 资源占 Li2CO3 当量的 {share * 100:.0f}%"
                            f"（{inferred.contained_lce_kt:,.0f} kt / {total.contained_lce_kt:,.0f} kt）。"
                            "Inferred 类别置信度低，通常不能直接进入储量与可行性研究，"
                            "需要加密钻探后方可转化。"
                        ),
                        evidence=cite,
                    )
                )

        measured = by_label.get("Measured")
        indicated = by_label.get("Indicated")
        if not measured and not indicated and total.contained_lce_kt:
            items.append(
                RiskItem(
                    title=f"{table.project} 无 Measured/Indicated 资源",
                    severity="high",
                    detail=(
                        "该技术报告中的资源量全部为 Inferred 类别，尚无 Measured 或 Indicated "
                        "级别资源，项目处于早期勘探阶段。"
                    ),
                    evidence=cite,
                )
            )
        elif not measured and total.contained_lce_kt:
            items.append(
                RiskItem(
                    title=f"{table.project} 缺少 Measured 类别",
                    severity="low",
                    detail="资源量中无 Measured 类别，最高置信度级别为 Indicated，地质置信度略低。",
                    evidence=cite,
                )
            )

        if len(table.rows) >= 2 and table.cut_off_grade:
            items.append(
                RiskItem(
                    title=f"{table.project} 边界品位口径",
                    severity="low",
                    detail=(
                        f"资源量按边界品位 {table.cut_off_grade} 报告（生效日 {table.effective_date or '未知'}）。"
                        "跨项目比较资源量时必须先对齐边界品位口径。"
                    ),
                    evidence=cite,
                )
            )
    return items


def assess_news(items: Iterable[NewsItem], evidence: EvidenceStore) -> list[RiskItem]:
    """Group headlines by the risk theme they trigger."""
    buckets: dict[tuple[str, str], dict[str, object]] = {}
    for item in items:
        haystack = f"{item.title} {item.summary}"
        for pattern, label, severity in _COMPILED:
            if not pattern.search(haystack):
                continue
            cite = [c.index for c in evidence.citations() if c.url == item.url][:1]
            bucket = buckets.setdefault((label, severity), {"titles": [], "cites": []})
            titles = bucket["titles"]
            if isinstance(titles, list) and item.title not in titles:
                titles.append(item.title)
            cites = bucket["cites"]
            if isinstance(cites, list):
                cites.extend(c for c in cite if c not in cites)
            break  # first matching theme wins; avoids one headline listing under 5 risks

    risks: list[RiskItem] = []
    for (label, severity), bucket in buckets.items():
        titles = bucket["titles"] if isinstance(bucket["titles"], list) else []
        cites = bucket["cites"] if isinstance(bucket["cites"], list) else []
        shown = "; ".join(str(t) for t in titles[:2])
        more = f" 等 {len(titles)} 条" if len(titles) > 2 else ""
        risks.append(
            RiskItem(
                title=f"{label}（新闻面）",
                severity=severity,  # type: ignore[arg-type]
                detail=f"相关报道：{shown}{more}。",
                evidence=sorted(int(c) for c in cites)[:4],
            )
        )
    return risks


def assess_freshness(
    reference: date | None,
    today: date,
    mode: str,
    provider_summary: dict[str, str],
) -> list[RiskItem]:
    items: list[RiskItem] = []
    if reference and (today - reference).days > 7:
        stale_days = (today - reference).days
        items.append(
            RiskItem(
                title="数据时效性",
                severity="medium" if stale_days > 30 else "low",
                detail=(
                    f"本次简报的数据锚定日期为 {reference.isoformat()}，"
                    f"距实际运行日期 {today.isoformat()} 已有 {stale_days} 天。"
                    "价格与新闻结论需按最新行情重新校验。"
                ),
            )
        )
    if mode == "offline":
        items.append(
            RiskItem(
                title="数据来源为离线夹具",
                severity="medium",
                detail=(
                    "本次运行使用仓库内置的合成演示数据集（"
                    f"news={provider_summary.get('news_provider')}, "
                    f"price={provider_summary.get('price_provider')}, "
                    f"pdf={provider_summary.get('pdf_provider')}）。"
                    "所有数字仅用于验证流程，不可用于投资决策。"
                ),
            )
        )
    return items


def build_risks(
    *,
    news: Iterable[NewsItem],
    tables: Iterable[ResourceTable],
    trends: Iterable[PriceTrend],
    evidence: EvidenceStore,
    reference: date | None,
    today: date,
    mode: str,
    provider_summary: dict[str, str],
    limit: int = 8,
) -> list[RiskItem]:
    news = list(news)
    tables = list(tables)
    trends = list(trends)

    risks = [
        *assess_prices(trends, evidence),
        *assess_resources(tables, evidence),
        *assess_news(news, evidence),
        *assess_freshness(reference, today, mode, provider_summary),
    ]
    risks.sort(key=lambda r: SEVERITY_ORDER.get(r.severity, 9))
    return risks[:limit]
