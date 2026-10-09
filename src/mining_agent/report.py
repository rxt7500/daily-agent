"""Markdown briefing renderer.

The layout is owned by this module, not by the model.  The model (or the
rule-based planner) supplies a structured :class:`~mining_agent.models.Narrative`
and the renderer composes it with the raw tool output, so a citation number in
the prose always resolves to a real source in the reference list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .evidence import looks_like_url
from .models import BriefingContext, Citation, PriceTrend, ResourceTable, RiskItem

SEVERITY_BADGE = {"high": "🔴 高", "medium": "🟠 中", "low": "🟡 低"}

DISCLAIMER = (
    "> **免责声明**：本简报由 mining-daily-agent 自动生成，仅用于技术演示与研究辅助，"
    "不构成任何投资建议。数据可能来自合成演示数据集，请以官方公告与持牌数据源为准。"
)


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def fmt(value: float | None, digits: int = 2, unit: str = "") -> str:
    if value is None:
        return "—"
    formatted = f"{value:,.{digits}f}"
    return f"{formatted} {unit}".strip()


def fmt_signed(value: float | None, digits: int = 1, suffix: str = "%") -> str:
    if value is None:
        return "—"
    return f"{value:+,.{digits}f}{suffix}"


def link(label: str, url: str) -> str:
    return f"[{label}]({url})" if looks_like_url(url) else label


def cite(indices: Iterable[int]) -> str:
    numbers = sorted({int(i) for i in indices})
    return "".join(f"[{n}]" for n in numbers)


def _escape(text: str) -> str:
    """Keep model prose from breaking out of the surrounding Markdown blocks."""
    return text.replace("|", "\\|").replace("\r", " ").strip()


def _with_cite(text: str, indices: Iterable[int]) -> str:
    return " ".join(part for part in (text.strip(), cite(indices)) if part)


# --------------------------------------------------------------------------
def render(ctx: BriefingContext) -> str:
    parts: list[str] = []
    parts.append(_header(ctx))
    parts.append(_summary(ctx))
    parts.append(_news(ctx))
    parts.append(_resources(ctx))
    parts.append(_prices(ctx))
    parts.append(_risks(ctx))
    parts.append(_outlook(ctx))
    parts.append(_references(ctx))
    parts.append(_appendix(ctx))
    parts.append(DISCLAIMER)
    return "\n\n".join(section for section in parts if section.strip()) + "\n"


# --------------------------------------------------------------------------
def _header(ctx: BriefingContext) -> str:
    mode_label = {
        "offline": "离线夹具数据（合成演示集）",
        "live": "实时数据源",
    }.get(ctx.mode, ctx.mode)
    generated = ctx.generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    anchor = ctx.reference_date.isoformat() if ctx.reference_date else "—"

    lines = [
        f"# 矿权日报 · {ctx.query}",
        "",
        f"- **生成时间**：{generated}",
        f"- **数据锚定日**：{anchor} ｜ 新闻窗口：最近 {ctx.days} 天",
        f"- **数据模式**：{mode_label}",
        f"- **规划器**：`{ctx.planner}`" + (f" ｜ 模型：`{ctx.model}`" if ctx.model else ""),
    ]
    if ctx.ticker:
        lines.append(f"- **相关标的**：{ctx.ticker}")
    if ctx.usage:
        total = ctx.usage.get("input_tokens", 0) + ctx.usage.get("output_tokens", 0)
        if total:
            lines.append(
                f"- **Token 用量**：输入 {ctx.usage.get('input_tokens', 0):,} ／ "
                f"输出 {ctx.usage.get('output_tokens', 0):,}"
            )
    return "\n".join(lines)


def _summary(ctx: BriefingContext) -> str:
    lines = ["## 一、摘要", "", ctx.narrative.executive_summary.strip() or "_（无摘要）_"]
    if ctx.narrative.key_findings:
        lines.append("")
        lines.append("**关键发现**")
        lines.append("")
        for finding in ctx.narrative.key_findings:
            lines.append(f"- {_escape(finding)}")
    return "\n".join(lines)


def _news(ctx: BriefingContext) -> str:
    lines = ["## 二、新闻动态", ""]
    if not ctx.news:
        lines.append("_本次运行未检索到相关新闻。_")
        return "\n".join(lines)

    by_url = {c.url: c.index for c in ctx.citations}
    notes = {
        int(h["citation_index"]): str(h.get("note", ""))
        for h in ctx.narrative.news_highlights
        if isinstance(h, dict) and h.get("citation_index")
    }
    bodies = {a.get("url"): a.get("body") or "" for a in ctx.articles}

    lines.append("| 时间 | 来源 | 标题 | 引用 |")
    lines.append("| --- | --- | --- | --- |")
    for item in ctx.news:
        # Keep the publisher's local date rather than shifting into UTC — an
        # 08:00 +08:00 announcement is that day's news, not the previous day's.
        published = item.published_at.strftime("%m-%d")
        index = by_url.get(item.url)
        lines.append(
            f"| {published} | {_escape(item.source)} | {link(_escape(item.title), item.url)} "
            f"| {cite([index]) if index else '—'} |"
        )

    highlight_notes = [f"- {cite([n])} {_escape(text)}" for n, text in notes.items() if text]
    if highlight_notes:
        lines += ["", "**要点解读**", ""] + highlight_notes

    deep = [a for a in ctx.articles if bodies.get(a.get("url"))]
    if deep:
        lines += ["", f"**全文阅读**（{len(deep)} 篇）", ""]
        for article in deep:
            excerpt = " ".join(str(article.get("body", "")).split())
            lines.append(f"- {_escape(article.get('title', ''))} — {_escape(excerpt[:180])}…")
    return "\n".join(lines)


def _resources(ctx: BriefingContext) -> str:
    lines = ["## 三、资源储量（NI 43-101 / JORC）", ""]
    if not ctx.resource_tables:
        lines.append("_本次运行未提取到资源量数据。_")
        return "\n".join(lines)

    by_source = {c.url: c.index for c in ctx.citations}
    for table in ctx.resource_tables:
        index = by_source.get(table.source)
        lines.append(f"### {_escape(table.project)}")
        lines.append("")
        lines.append(
            f"- 生效日期：{table.effective_date or '未标注'} ｜ "
            f"边界品位：{table.cut_off_grade or '未标注'} ｜ "
            f"数据源：{link('技术报告 PDF', table.source) if looks_like_url(table.source) else table.source} "
            f"{cite([index]) if index else ''}"
        )
        lines.append("")
        lines.append("| 类别 | 吨位 (Mt) | 品位 (% Li2O) | 含 Li2O (t) | LCE 当量 (kt) |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for row in table.rows:
            lines.append(
                f"| {_escape(row.raw_label or row.category)} | {fmt(row.tonnes_mt, 1)} "
                f"| {fmt(row.grade, 2)} | {fmt(row.contained_metal_t, 0)} "
                f"| {fmt(row.contained_lce_kt, 1)} |"
            )
        lines.append("")

    lines.append(
        "> LCE（碳酸锂当量）由 Li2O 品位按化学计量换算："
        "Li2O → Li × 0.4646，Li → Li₂CO₃ × 5.3228，即 1% Li2O ≈ 2.47% LCE。"
    )
    return "\n".join(lines)


def _prices(ctx: BriefingContext) -> str:
    lines = ["## 四、价格走势", ""]
    if not ctx.quotes and not ctx.price_trends:
        lines.append("_本次运行未获取到价格数据。_")
        return "\n".join(lines)

    by_source = {c.url: c.index for c in ctx.citations}

    if ctx.quotes:
        lines.append("**最新报价**")
        lines.append("")
        lines.append("| 品种 | 日期 | 收盘价 | 单位 | 引用 |")
        lines.append("| --- | --- | ---: | --- | --- |")
        for quote in ctx.quotes:
            index = by_source.get(quote.source)
            lines.append(
                f"| {_escape(quote.commodity)} | {quote.date.isoformat()} "
                f"| {quote.close:,.1f} | {quote.unit} | {cite([index]) if index else '—'} |"
            )
        lines.append("")

    if ctx.price_trends:
        lines.append("**区间走势**")
        lines.append("")
        lines.append("| 品种 | 区间 | 最新 | 变动 | 高低 | 日波动率 | 走势 |")
        lines.append("| --- | --- | ---: | ---: | --- | ---: | --- |")
        for trend in ctx.price_trends:
            lines.append(
                f"| {_escape(trend.commodity)} | {trend.days}d "
                f"| {trend.latest:,.1f} {trend.unit} | {fmt_signed(trend.change_pct)} "
                f"| {trend.low:,.0f}–{trend.high:,.0f} | {trend.volatility_pct:.2f}% "
                f"| `{trend.sparkline()}` |"
            )
        lines.append("")
        lines += _trend_details(ctx.price_trends)

    if any(t.mode == "offline" for t in ctx.price_trends) or any(
        q.mode == "offline" for q in ctx.quotes
    ):
        lines += ["", "> 价格数据来自离线合成序列，仅用于验证走势计算逻辑。"]
    return "\n".join(lines)


def _trend_details(trends: list[PriceTrend], points: int = 5) -> list[str]:
    out: list[str] = []
    for trend in trends:
        recent = trend.points[-points:]
        if len(recent) < 2:
            continue
        rendered = " → ".join(f"{p.date.strftime('%m-%d')} {p.close:,.0f}" for p in recent)
        out.append(f"- **{_escape(trend.commodity)}** 最近 {len(recent)} 个交易日：{rendered}")
    return out


def _risks(ctx: BriefingContext) -> str:
    lines = ["## 五、风险提示", ""]
    if not ctx.risks:
        lines.append("_未识别到显著风险信号。_")
        return "\n".join(lines)

    for risk in ctx.risks:
        badge = SEVERITY_BADGE.get(risk.severity, risk.severity)
        tag = "（模型评估）" if risk.source == "llm" else ""
        lines.append(f"- {badge} **{_escape(risk.title)}**{tag} — {_with_cite(_escape(risk.detail), risk.evidence)}")
    return "\n".join(lines)


def _outlook(ctx: BriefingContext) -> str:
    if not ctx.narrative.outlook.strip():
        return ""
    return f"## 六、观察点\n\n{ctx.narrative.outlook.strip()}"


def _references(ctx: BriefingContext) -> str:
    lines = ["## 七、引用源", ""]
    if not ctx.citations:
        lines.append("_无引用源。_")
        return "\n".join(lines)

    kind_label = {"news": "新闻", "document": "技术报告", "data": "行情数据"}
    for citation in sorted(ctx.citations, key=lambda c: c.index):
        prefix = kind_label.get(citation.kind, citation.kind)
        lines.append(f"{citation.index}. `{prefix}` {link(citation.label, citation.url)}")
    return "\n".join(lines)


def _appendix(ctx: BriefingContext) -> str:
    if not ctx.warnings and not ctx.tool_calls:
        return ""
    lines = ["## 八、运行记录", ""]

    if ctx.tool_calls:
        lines.append("**工具调用轨迹**")
        lines.append("")
        lines.append("| # | MCP 工具 | 参数 | 结果 |")
        lines.append("| ---: | --- | --- | --- |")
        for index, call in enumerate(ctx.tool_calls, start=1):
            arguments = str(call.get("arguments", {}))
            status = "OK" if call.get("ok") else f"FAILED: {call.get('error', '')}"
            lines.append(
                f"| {index} | `{call.get('tool')}` | `{_escape(arguments[:90])}` | {_escape(status[:70])} |"
            )
        lines.append("")

    if ctx.warnings:
        lines.append("**警告**")
        lines.append("")
        for warning in ctx.warnings:
            lines.append(f"- {_escape(str(warning))}")
    return "\n".join(lines)


def render_json(ctx: BriefingContext) -> str:
    return ctx.model_dump_json(indent=2)


def suggested_filename(ctx: BriefingContext, suffix: str = ".md") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    slug = "".join(ch if ch.isalnum() else "-" for ch in ctx.query).strip("-")[:40] or "briefing"
    return f"mining-briefing_{slug}_{stamp}{suffix}"
