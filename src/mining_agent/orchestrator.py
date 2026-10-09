"""Agent orchestration.

Two planners drive the same MCP tool surface and produce the same
:class:`~mining_agent.models.BriefingContext`:

``ReActPlanner``
    A model-driven tool-use loop against Claude.  The model decides what to
    search, read and price, then calls a client-side ``submit_briefing`` tool
    with a *structured* narrative.  Free-form Markdown is never trusted as the
    deliverable — the renderer owns the layout, the model owns the prose.

``DeterministicPlanner``
    The same pipeline, hard-coded.  Used when there is no API key, when
    ``--no-llm`` is passed, and as the automatic fallback if the model loop
    fails.  A briefing always comes out.

Both share :class:`BriefingRunner`, which records every tool call, harvests
citations for every source it sees, and normalises results into domain models.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import anthropic

from .config import Settings
from .evidence import EvidenceStore
from .mcp_client import McpError, McpHub
from .models import (
    BriefingContext,
    Narrative,
    NewsItem,
    PriceQuote,
    PriceTrend,
    ResourceTable,
    utcnow,
)
from .risks import build_risks

log = logging.getLogger("mining-agent.orchestrator")

SUBMIT_TOOL_NAME = "submit_briefing"

SUBMIT_TOOL: dict[str, Any] = {
    "name": SUBMIT_TOOL_NAME,
    "description": (
        "提交最终简报。数据收集完成后必须调用且只调用一次。"
        "所有数字必须来自工具返回结果；引用请使用工具结果中 _new_citations 给出的编号。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "executive_summary": {
                "type": "string",
                "description": "3-5 句话的执行摘要，包含关键数字与引用编号。",
            },
            "key_findings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "4-6 条要点，每条一个事实，带引用编号。",
            },
            "news_highlights": {
                "type": "array",
                "description": "最重要的 2-4 条新闻及其对项目/价格的含义。",
                "items": {
                    "type": "object",
                    "properties": {
                        "citation_index": {"type": "integer"},
                        "note": {"type": "string"},
                    },
                    "required": ["citation_index", "note"],
                },
            },
            "risks": {
                "type": "array",
                "description": "风险提示。severity 取 high/medium/low，evidence 为引用编号数组。",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "detail": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["title", "severity", "detail"],
                },
            },
            "outlook": {"type": "string", "description": "未来 1-4 周需要跟踪的观察点。"},
        },
        "required": ["executive_summary", "key_findings", "risks"],
    },
}

SYSTEM_PROMPT = """你是「矿权日报」研究 Agent，为矿业投资与商务团队生成某矿区/矿种的每日简报。

你可以使用的工具分属三个 MCP server：
- mining-news-mcp：新闻搜索与全文抓取
- mineral-pdf-mcp：NI 43-101 / JORC 技术报告的资源量提取
- lme-price-mcp：大宗商品价格与走势

标准工作流（如有充分理由可调整）：
1. mining-news-mcp__search 搜索目标矿区/矿种近几天的新闻，挑出最相关的几条；
2. 对其中 2-3 条调用 mining-news-mcp__fetch_article 读全文，确认事实细节；
3. mineral-pdf-mcp__list_documents 找到可用技术报告，再用 mineral-pdf-mcp__extract_resources
   提取 Measured/Indicated/Inferred 资源量；
4. lme-price-mcp__list_commodities 确认可报价品种，再对核心品种调用
   lme-price-mcp__get_price 与 lme-price-mcp__get_trend（趋势窗口至少 30 天）。

硬性规则：
- 绝不编造数据。所有数字、项目名、日期都必须来自工具返回结果；
- 工具返回的 _new_citations 给出本次结果新增的引用编号，正文引用时写成 [编号]；
- 若某类数据无法获取，在简报中说明缺口，不要用常识补全；
- 数据收集完成后，调用 submit_briefing 提交结构化简报，这是唯一的交付方式；
- submit_briefing 只调用一次，收到确认后立即结束回复，不要再调用其它工具。

用中文撰写正文。风格：简洁、结论先行、数字精确。"""


@dataclass
class CallOutcome:
    tool: str
    ok: bool
    data: Any | None
    text: str
    new_citations: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# query understanding
# --------------------------------------------------------------------------
COMMODITY_HINTS: list[tuple[tuple[str, ...], list[str]]] = [
    (("lithium", "锂", "spodumene", "锂辉石", "pilbara", "碳酸锂", "lce"), ["lithium-carbonate", "spodumene"]),
    (("nickel", "镍"), ["nickel"]),
    (("copper", "铜"), ["copper"]),
    (("hydroxide", "氢氧化锂"), ["lithium-hydroxide"]),
]


def infer_commodities(query: str) -> list[str]:
    lowered = query.lower()
    for needles, commodities in COMMODITY_HINTS:
        if any(needle in lowered for needle in needles):
            return commodities
    return ["lithium-carbonate"]


# Instruction words that carry no search signal. Longest-first so that
# "今日简报" is removed before "简报".
_INSTRUCTION_WORDS = re.compile(
    r"给我|帮我|我要|请|生成|做一份|来一份|一份|一个|关于|有关|的|今日|今天|每日|日报|简报|"
    r"报告|汇总|总结|看看|查一下|搜索|最新|动态|情况|新闻|资讯|消息|行情|"
    r"news|report|briefing|daily|today|"
    r"generate|create|please|give me|about|summary|latest",
    re.I,
)


def distill_query(query: str) -> str:
    """Strip instruction wording so the search query is just the subject.

    ``"给我生成一份关于 Pilbara 锂矿的今日简报"`` → ``"Pilbara 锂矿"``.  Without
    this the offline scorer spends its token budget matching "一份" and "今日"
    against the corpus.
    """
    stripped = _INSTRUCTION_WORDS.sub(" ", query)
    stripped = re.sub(r"[，。、,.]|\s+", " ", stripped).strip()
    return stripped or query.strip()


def _matches_query(text: str, query: str) -> bool:
    lowered, hay = query.lower(), text.lower()
    words = [w for w in lowered.replace("矿", " ").replace("的", " ").split() if len(w) >= 3]
    return any(w in hay for w in words) or "pilbara" in hay


# --------------------------------------------------------------------------
class BriefingRunner:
    """Shared tool plumbing: call, record, harvest citations, ingest artifacts."""

    def __init__(self, settings: Settings, hub: McpHub, query: str, days: int) -> None:
        self.settings = settings
        self.hub = hub
        self.query = query
        self.days = days
        self.evidence = EvidenceStore()

        self.news: list[NewsItem] = []
        self.articles: list[dict[str, Any]] = []
        self.tables: list[ResourceTable] = []
        self.trends: list[PriceTrend] = []
        self.quotes: list[PriceQuote] = []
        self.warnings: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.reference_date: date | None = None
        self.mode = "offline"
        self._seen_news: set[str] = set()

    # -- tool invocation ---------------------------------------------------
    async def call(self, qualified: str, arguments: dict[str, Any]) -> CallOutcome:
        tool = self.hub.tool(qualified)
        name = tool.name if tool else qualified.rsplit("__", 1)[-1]
        try:
            result = await self.hub.call(qualified, arguments)
        except (McpError, Exception) as exc:  # noqa: BLE001 - a failed tool must not end the run
            log.warning("tool %s failed: %s", qualified, exc)
            self.warnings.append(f"{qualified}: {exc}")
            self.tool_calls.append({"tool": qualified, "arguments": arguments, "ok": False, "error": str(exc)})
            return CallOutcome(tool=name, ok=False, data=None, text=str(exc))

        before = {c.index for c in self.evidence.citations()}
        if result.data is not None:
            self.evidence.harvest(name, result.data)
        new_citations = {
            str(c.index): c.label
            for c in self.evidence.citations()
            if c.index not in before
        }

        self._ingest(name, result.data)
        self.tool_calls.append(
            {"tool": qualified, "arguments": arguments, "ok": result.ok, "citations": sorted(int(k) for k in new_citations)}
        )
        log.info("tool %s(%s) -> ok=%s", qualified, json.dumps(arguments, ensure_ascii=False)[:120], result.ok)
        return CallOutcome(tool=name, ok=result.ok, data=result.data, text=result.text, new_citations=new_citations)

    # -- artifact ingestion ------------------------------------------------
    def _ingest(self, name: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if data.get("reference_date"):
            try:
                self.reference_date = date.fromisoformat(str(data["reference_date"]))
            except ValueError:
                pass
        if data.get("mode"):
            self.mode = str(data["mode"])

        if name == "search":
            if data.get("window_expanded"):
                self.warnings.append(
                    "新闻窗口内无结果，已自动扩大到语料库最近条目（window_expanded=true）"
                )
            for raw in data.get("results", []) or []:
                url = str(raw.get("url") or "")
                if not url or url in self._seen_news:
                    continue
                try:
                    self.news.append(NewsItem.model_validate(raw))
                    self._seen_news.add(url)
                except Exception as exc:  # noqa: BLE001 - tolerate a malformed item
                    log.debug("skipping malformed news item: %s", exc)

        elif name == "fetch_article":
            if data.get("url"):
                self.articles.append(data)

        elif name == "extract_resources":
            for raw in data.get("tables", []) or []:
                try:
                    table = ResourceTable.model_validate(raw)
                except Exception as exc:  # noqa: BLE001
                    log.debug("skipping malformed resource table: %s", exc)
                    continue
                if table.rows:
                    self.tables.append(table)
            for warning in data.get("warnings", []) or []:
                self.warnings.append(f"{data.get('source', 'pdf')}: {warning}")

        elif name == "get_price":
            try:
                self.quotes.append(PriceQuote.model_validate(data))
            except Exception as exc:  # noqa: BLE001
                log.debug("skipping malformed quote: %s", exc)

        elif name == "get_trend":
            try:
                self.trends.append(PriceTrend.model_validate(data))
            except Exception as exc:  # noqa: BLE001
                log.debug("skipping malformed trend: %s", exc)

    # -- assembly ----------------------------------------------------------
    def build_risks(self):
        return build_risks(
            news=self.news,
            tables=self.tables,
            trends=self.trends,
            evidence=self.evidence,
            reference=self.reference_date,
            today=datetime.now(timezone.utc).date(),
            mode=self.mode,
            provider_summary=self.settings.describe(),
        )

    def build_context(
        self,
        *,
        planner: str,
        narrative: Narrative,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> BriefingContext:
        risks = narrative.risks or self.build_risks()
        return BriefingContext(
            query=self.query,
            ticker=self._ticker(),
            generated_at=utcnow(),
            reference_date=self.reference_date,
            days=self.days,
            mode=self.mode,
            planner=planner,
            model=model,
            news=self.news,
            articles=self.articles,
            resource_tables=self.tables,
            price_trends=self.trends,
            quotes=self.quotes,
            risks=risks,
            narrative=narrative,
            citations=self.evidence.citations(),
            tool_calls=self.tool_calls,
            warnings=self.warnings,
            usage=usage or {},
        )

    def _ticker(self) -> str:
        for item in self.news:
            if item.tickers:
                return ", ".join(item.tickers[:4])
        return ""


# --------------------------------------------------------------------------
class DeterministicPlanner:
    """Fixed pipeline — no model, no API key, same output contract."""

    def __init__(self, runner: BriefingRunner) -> None:
        self.r = runner
        self.settings = runner.settings

    async def run(self) -> BriefingContext:
        r = self.r
        await r.call(
            "mining-news-mcp__search",
            {"query": distill_query(r.query), "days": r.days, "limit": 8},
        )

        for item in r.news[:3]:
            await r.call("mining-news-mcp__fetch_article", {"url": item.url})

        docs = await r.call("mineral-pdf-mcp__list_documents", {})
        pdf_url = self._pick_document(docs.data)
        if pdf_url:
            await r.call("mineral-pdf-mcp__extract_resources", {"pdf_url": pdf_url})
        else:
            r.warnings.append("未找到可用的技术报告文档，简报将不含资源量数据")

        window = max(r.days, 30)
        for commodity in infer_commodities(r.query):
            await r.call("lme-price-mcp__get_price", {"commodity": commodity})
            await r.call("lme-price-mcp__get_trend", {"commodity": commodity, "days": window})

        narrative = self._compose_narrative()
        return r.build_context(planner="deterministic", narrative=narrative)

    def _pick_document(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        documents = payload.get("documents") or []
        if not documents:
            return None
        for doc in documents:
            name = str(doc.get("name", ""))
            if _matches_query(name.replace("-", " ").replace("_", " "), self.r.query):
                return str(doc.get("pdf_url"))
        return str(documents[0].get("pdf_url"))

    # -- prose -------------------------------------------------------------
    def _compose_narrative(self) -> Narrative:
        r = self.r
        findings: list[str] = []
        highlights: list[dict[str, Any]] = []

        cite_of = {c.url: c.index for c in r.evidence.citations()}
        for item in r.news[:3]:
            index = cite_of.get(item.url)
            if index:
                highlights.append(
                    {
                        "citation_index": index,
                        "note": f"{item.source}：{item.summary[:120]}",
                    }
                )

        if r.news:
            findings.append(f"近 {r.days} 天共检索到 {len(r.news)} 条相关新闻，其中 {len(r.articles)} 条已读取全文。")

        for trend in r.trends:
            findings.append(
                f"{trend.commodity} 最新价 {trend.latest:,.0f} {trend.unit}，"
                f"近 {trend.days} 天 {trend.change_pct:+.1f}%，"
                f"区间 {trend.low:,.0f}–{trend.high:,.0f}，日波动率 {trend.volatility_pct:.2f}%。"
            )

        for table in r.tables:
            total = next(
                (row for row in table.rows if row.category == "Total" and (row.raw_label or "").lower().strip() == "total"),
                next((row for row in reversed(table.rows) if row.category == "Total"), None),
            )
            if total and total.contained_lce_kt:
                findings.append(
                    f"{table.project} 总资源量 {total.tonnes_mt:,.1f} Mt @ {total.grade}% Li2O"
                    f"（约 {total.contained_lce_kt:,.0f} kt LCE，生效日 {table.effective_date or '未标注'}）。"
                )

        risks = r.build_risks()
        top = "、".join(item.title for item in risks[:3]) or "暂无显著风险信号"

        if r.news or r.trends or r.tables:
            summary = (
                f"报告期锚定 {r.reference_date.isoformat() if r.reference_date else '未知'}，"
                f"共执行 {len(r.tool_calls)} 次工具调用。"
                + (findings[0] if findings else "")
                + f" 主要风险集中在：{top}。"
            )
        else:
            summary = "本次运行未能获取到有效数据，请检查 MCP server 状态与数据模式配置。"

        return Narrative(
            executive_summary=summary.strip(),
            key_findings=findings[:6],
            news_highlights=highlights,
            risks=risks,
            outlook=(
                "跟踪要点："
                + "；".join(
                    [
                        "锂盐现货价与锂辉石精矿指数的周度变化",
                        "审批与许可进程（含申诉窗口与水资源分配）",
                        "下一次资源量更新与 Inferred→Indicated 的转化进度",
                    ][: 2 + (1 if r.tables else 0)]
                )
                + "。"
            ),
        )


# --------------------------------------------------------------------------
class ReActPlanner:
    """Model-driven tool-use loop."""

    def __init__(self, runner: BriefingRunner) -> None:
        self.r = runner
        self.settings = runner.settings
        self._use_fallback = (
            settings_supports_fallback(runner.settings.model)
            and runner.settings.enable_refusal_fallback
        )

    async def run(self) -> BriefingContext:
        settings = self.settings
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        runner = self.r

        tools = runner.hub.anthropic_tools() + [SUBMIT_TOOL]
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._user_prompt()}
        ]
        narrative: Narrative | None = None
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

        for step in range(1, settings.max_iterations + 1):
            response = await self._create(client, messages, tools)
            messages.append({"role": "assistant", "content": response.content})

            for field_name in usage:
                usage[field_name] += int(getattr(response.usage, field_name, 0) or 0)

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise RuntimeError(
                    "模型拒绝了本次请求"
                    f"（category={getattr(details, 'category', None)}）"
                )

            if response.stop_reason == "pause_turn":
                log.info("step %s: pause_turn — resuming", step)
                continue

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                if text and narrative is None:
                    log.warning("model ended the turn without submit_briefing; using its text as the summary")
                    narrative = Narrative(executive_summary=text)
                break

            results: list[dict[str, Any]] = []
            for block in tool_uses:
                if block.name == SUBMIT_TOOL_NAME:
                    try:
                        narrative = Narrative.model_validate(dict(block.input))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("submit_briefing payload failed validation: %s", exc)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "简报已接收，请结束本轮回复。",
                        }
                    )
                    continue

                outcome = await runner.call(block.name, dict(block.input))
                results.append(self._tool_result_block(block.id, outcome))

            messages.append({"role": "user", "content": results})
            if narrative is not None:
                break
        else:
            runner.warnings.append(
                f"达到最大工具调用轮次（{settings.max_iterations}）仍未收到 submit_briefing，"
                "已回退到规则化摘要"
            )

        if narrative is None:
            narrative = DeterministicPlanner(runner)._compose_narrative()

        return runner.build_context(
            planner="react", narrative=narrative, model=settings.model, usage=usage
        )

    # -- plumbing ----------------------------------------------------------
    def _tool_result_block(self, tool_use_id: str, outcome: CallOutcome) -> dict[str, Any]:
        payload: Any = outcome.data if outcome.data is not None else {"raw_text": outcome.text[:4000]}
        if isinstance(payload, dict) and outcome.new_citations:
            payload = {**payload, "_new_citations": outcome.new_citations}
        elif outcome.new_citations:
            payload = {"result": payload, "_new_citations": outcome.new_citations}
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps(payload, ensure_ascii=False, default=str),
            "is_error": not outcome.ok,
        }

    async def _create(
        self,
        client: anthropic.AsyncAnthropic,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ):
        """One API call, with server-side refusal fallbacks when available."""
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "tools": tools,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.settings.effort},
        }
        if self._use_fallback:
            try:
                return await client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    **kwargs,
                )
            except (TypeError, anthropic.BadRequestError) as exc:
                log.warning("server-side refusal fallback unavailable (%s); disabling it", exc)
                self._use_fallback = False
        return await client.messages.create(**kwargs)

    def _user_prompt(self) -> str:
        r = self.r
        return (
            f"请生成一份今日矿权简报。\n\n"
            f"研究标的：{r.query}\n"
            f"新闻时间窗口：最近 {r.days} 天\n\n"
            "要求：先收集新闻、资源量与价格数据，再调用 submit_briefing 提交结构化简报。"
            "简报需覆盖：新闻摘要、储量数据、价格走势、风险提示。"
        )


def settings_supports_fallback(model: str) -> bool:
    """Server-side refusal fallbacks are only offered on these models."""
    return model in {"claude-opus-5", "claude-fable-5", "claude-mythos-5"}


# --------------------------------------------------------------------------
async def run_briefing(
    settings: Settings,
    hub: McpHub,
    query: str,
    days: int,
    *,
    prefer_llm: bool = True,
) -> BriefingContext:
    """Run the best available planner, degrading gracefully to the deterministic one."""
    runner = BriefingRunner(settings, hub, query, days)

    use_llm = prefer_llm and settings.llm_enabled and bool(settings.anthropic_api_key)
    if prefer_llm and settings.llm_enabled and not settings.anthropic_api_key:
        log.info("ANTHROPIC_API_KEY not set — using the deterministic planner")
        runner.warnings.append("未配置 ANTHROPIC_API_KEY，本次使用规则化规划器（--no-llm 等价）")

    if use_llm:
        try:
            return await ReActPlanner(runner).run()
        except Exception as exc:  # noqa: BLE001 - never fail to produce a briefing
            log.warning("ReAct planner failed (%s); falling back to deterministic", exc)
            runner.warnings.append(f"LLM 规划器失败，已回退到规则化规划器：{exc}")
            runner.tool_calls.clear()
            runner.news.clear()
            runner.articles.clear()
            runner.tables.clear()
            runner.trends.clear()
            runner.quotes.clear()
            runner._seen_news.clear()
            runner.evidence = EvidenceStore()

    return await DeterministicPlanner(runner).run()
