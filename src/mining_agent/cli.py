"""Command line entry point.

    python -m mining_agent "给我生成一份关于 Pilbara 锂矿的今日简报"
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

from .config import Settings
from .mcp_client import McpError, McpHub, default_server_specs, load_config_specs
from .orchestrator import run_briefing
from .report import render, render_json, suggested_filename
from .servers.common import configure_logging, get_logger

log = get_logger("mining-agent")

DEFAULT_QUERY = "给我生成一份关于 Pilbara 锂矿的今日简报"

TAGLINE = "mining-daily-agent · 3 MCP servers + 1 agent client"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mining-daily-agent",
        description=TAGLINE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            f'  python -m mining_agent "{DEFAULT_QUERY}"\n'
            '  python -m mining_agent "Pilbara lithium" --days 14 --out report.md\n'
            '  python -m mining_agent "锂矿" --no-llm --json --out briefing.json\n'
        ),
    )
    parser.add_argument("query", nargs="?", default=DEFAULT_QUERY, help="研究标的（自然语言）")
    parser.add_argument("--days", type=int, default=7, help="新闻窗口天数（默认 7）")
    parser.add_argument("--out", type=Path, help="输出文件路径（默认写到 ./output/）")
    parser.add_argument("--json", action="store_true", help="额外输出结构化 JSON")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM，使用规则化规划器")
    parser.add_argument("--mcp-config", type=Path, help="从 mcp-config.json 读取 server 定义")
    parser.add_argument("--model", help="覆盖模型 ID（默认 claude-opus-5）")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="覆盖 reasoning effort")
    parser.add_argument("--max-iterations", type=int, help="LLM 工具调用最大轮次")
    parser.add_argument("--print", dest="print_report", action="store_true", help="同时把简报打印到 stdout")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    base = Settings.from_env()
    overrides: dict[str, object] = {}
    if args.model:
        overrides["model"] = args.model
    if args.effort:
        overrides["effort"] = args.effort
    if args.max_iterations:
        overrides["max_iterations"] = args.max_iterations
    if args.no_llm:
        overrides["llm_enabled"] = False
    if args.verbose:
        overrides["log_level"] = "DEBUG"
    return dataclasses.replace(base, **overrides) if overrides else base


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    specs = (
        load_config_specs(args.mcp_config)
        if args.mcp_config
        else default_server_specs(settings)
    )
    log.info("connecting to %s MCP server(s): %s", len(specs), ", ".join(s.name for s in specs))

    try:
        async with McpHub(specs) as hub:
            log.info("tools available: %s", len(hub.tools))
            ctx = await run_briefing(
                settings, hub, args.query, max(1, args.days), prefer_llm=not args.no_llm
            )
    except McpError as exc:
        log.error("MCP connection failed: %s", exc)
        print(f"❌ MCP 连接失败：{exc}", file=sys.stderr)
        return 2

    markdown = render(ctx)

    out_path = args.out
    if out_path is None:
        out_path = Path.cwd() / "output" / suggested_filename(ctx)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")

    json_path = None
    if args.json:
        json_path = out_path.with_suffix(".json")
        json_path.write_text(render_json(ctx), encoding="utf-8")

    if args.print_report:
        print(markdown)

    summary = {
        "planner": ctx.planner,
        "mode": ctx.mode,
        "reference_date": ctx.reference_date.isoformat() if ctx.reference_date else None,
        "news": len(ctx.news),
        "resource_tables": len(ctx.resource_tables),
        "resource_rows": sum(len(t.rows) for t in ctx.resource_tables),
        "price_trends": len(ctx.price_trends),
        "risks": len(ctx.risks),
        "citations": len(ctx.citations),
        "tool_calls": len(ctx.tool_calls),
        "warnings": len(ctx.warnings),
        "output": str(out_path),
    }
    if json_path:
        summary["json"] = str(json_path)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for warning in ctx.warnings:
        print(f"⚠️  {warning}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings_from_args(args)
    configure_logging(settings.log_level, settings.log_json)

    if sys.platform == "win32":
        # Keep the Chinese report readable in a cp936 console.
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    try:
        return asyncio.run(_run(args, settings))
    except KeyboardInterrupt:
        print("\n中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
