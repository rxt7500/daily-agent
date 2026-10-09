"""MCP client: connect to N servers, expose their tools to the agent.

The hub owns one :class:`~mcp.ClientSession` per server, discovered either from
``mcp-config.json`` (the same file Claude Desktop / Cursor read) or built from
environment variables.

Tool names are namespaced ``<server>__<tool>`` because Anthropic tool names must
match ``^[a-zA-Z0-9_-]{1,128}$`` and two servers may legitimately both expose a
``search``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters

from .config import Settings

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")

DEFAULT_CALL_TIMEOUT = 90.0


def sanitize(name: str) -> str:
    return _NAME_SAFE.sub("_", name)


class McpError(RuntimeError):
    """Connection or protocol-level failure."""


@dataclass
class ServerSpec:
    name: str
    transport: str = "stdio"            # stdio | sse
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    timeout: float = DEFAULT_CALL_TIMEOUT

    @classmethod
    def from_config_entry(cls, name: str, entry: dict[str, Any]) -> "ServerSpec":
        if "url" in entry:
            return cls(name=name, transport="sse", url=str(entry["url"]),
                       timeout=float(entry.get("timeout", DEFAULT_CALL_TIMEOUT)))
        if "command" not in entry:
            raise McpError(f"server {name!r} needs either 'command' (stdio) or 'url' (sse)")
        return cls(
            name=name,
            transport="stdio",
            command=str(entry["command"]),
            args=[str(a) for a in entry.get("args", [])],
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            timeout=float(entry.get("timeout", DEFAULT_CALL_TIMEOUT)),
        )


@dataclass
class McpTool:
    server: str
    name: str
    qualified: str
    description: str
    input_schema: dict[str, Any]

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.qualified,
            "description": self.description or f"{self.server}.{self.name}",
            "input_schema": self.input_schema
            or {"type": "object", "properties": {}},
        }


@dataclass
class ToolResult:
    ok: bool
    text: str
    data: Any | None = None

    def as_json_text(self) -> str:
        if self.data is not None:
            return json.dumps(self.data, ensure_ascii=False, default=str)
        return self.text


# --------------------------------------------------------------------------
def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_server_specs(settings: Settings) -> list[ServerSpec]:
    """The three servers this project ships, launched as stdio subprocesses."""
    env = {
        "PYTHONPATH": str(_project_root() / "src"),
        "PYTHONUNBUFFERED": "1",
        "MINING_AGENT_DATA_MODE": settings.data_mode,
        "MINING_AGENT_DATA_DIR": str(settings.data_dir),
        "MINING_AGENT_LOG_LEVEL": settings.log_level,
        "MINING_AGENT_NEWS_PROVIDER": settings.news_provider,
        "MINING_AGENT_PDF_PROVIDER": settings.pdf_provider,
        "MINING_AGENT_PRICE_PROVIDER": settings.price_provider,
        "MINING_AGENT_PDF_ALLOW_NETWORK": "1" if settings.pdf_allow_network else "0",
    }
    for key in ("NEWS_API_KEY", "MINING_AGENT_PRICE_BASE_URL", "MINING_AGENT_PRICE_API_KEY"):
        if os.getenv(key):
            env[key] = os.environ[key]

    executable = sys.executable or "python"
    return [
        ServerSpec(
            name="mining-news-mcp",
            command=executable,
            args=["-m", "mining_agent.servers.news"],
            env=env,
        ),
        ServerSpec(
            name="mineral-pdf-mcp",
            command=executable,
            args=["-m", "mining_agent.servers.mineral_pdf"],
            env=env,
        ),
        ServerSpec(
            name="lme-price-mcp",
            command=executable,
            args=["-m", "mining_agent.servers.lme_price"],
            env=env,
        ),
    ]


def load_config_specs(path: str | Path) -> list[ServerSpec]:
    """Read an ``mcp-config.json`` (Claude Desktop / Cursor layout)."""
    config_path = Path(path)
    if not config_path.is_file():
        raise McpError(f"mcp config not found: {config_path}")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    servers = payload.get("mcpServers") or payload.get("servers") or {}
    if not servers:
        raise McpError(f"no 'mcpServers' block in {config_path}")
    return [ServerSpec.from_config_entry(name, entry) for name, entry in servers.items()]


# --------------------------------------------------------------------------
class McpHub:
    """Owns the live sessions and routes tool calls."""

    def __init__(self, specs: list[ServerSpec]) -> None:
        self.specs = specs
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._tools: list[McpTool] = []
        self._by_qualified: dict[str, McpTool] = {}
        self._timeouts: dict[str, float] = {spec.name: spec.timeout for spec in specs}

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "McpHub":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def start(self) -> None:
        """Connect to every server, in order.

        Sequential on purpose: anyio task groups must be entered and exited from
        the same task, so the sessions cannot be opened from ``gather``ed
        coroutines and still be closed cleanly from here.
        """
        self._stack = AsyncExitStack()
        for spec in self.specs:
            try:
                session = await self._connect(spec)
            except Exception as exc:  # noqa: BLE001 - one bad server must not kill the run
                raise McpError(f"failed to connect to MCP server {spec.name!r}: {exc}") from exc
            self._sessions[spec.name] = session
            await self._register_tools(spec, session)

    async def _connect(self, spec: ServerSpec) -> ClientSession:
        assert self._stack is not None
        if spec.transport == "sse":
            from mcp.client.sse import sse_client

            read, write = await self._stack.enter_async_context(sse_client(spec.url))  # type: ignore[arg-type]
        else:
            params = StdioServerParameters(
                command=spec.command or "python",
                args=spec.args,
                env={**os.environ, **spec.env},
            )
            read, write = await self._stack.enter_async_context(stdio_client_safe(params))

        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _register_tools(self, spec: ServerSpec, session: ClientSession) -> None:
        listing = await session.list_tools()
        for tool in listing.tools:
            qualified = f"{sanitize(spec.name)}__{sanitize(tool.name)}"
            entry = McpTool(
                server=spec.name,
                name=tool.name,
                qualified=qualified,
                description=(tool.description or "").strip(),
                input_schema=dict(tool.inputSchema or {}),
            )
            self._tools.append(entry)
            self._by_qualified[qualified] = entry

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self._sessions.clear()

    # -- introspection -----------------------------------------------------
    @property
    def tools(self) -> list[McpTool]:
        return list(self._tools)

    def servers(self) -> list[str]:
        return list(self._sessions)

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [tool.to_anthropic() for tool in self._tools]

    def tool(self, qualified: str) -> McpTool | None:
        return self._by_qualified.get(qualified)

    def describe_tools(self) -> str:
        """Human/LLM readable inventory — handy in prompts and debug output."""
        lines = []
        for tool in self._tools:
            params = ", ".join((tool.input_schema.get("properties") or {}).keys())
            lines.append(f"- {tool.qualified}({params}): {tool.description.splitlines()[0] if tool.description else ''}")
        return "\n".join(lines)

    # -- calls -------------------------------------------------------------
    async def call(self, qualified: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        tool = self._by_qualified.get(qualified)
        if tool is None:
            raise McpError(f"unknown tool {qualified!r}; available: {sorted(self._by_qualified)}")
        session = self._sessions.get(tool.server)
        if session is None:
            raise McpError(f"server {tool.server!r} is not connected")

        timeout = self._timeouts.get(tool.server, DEFAULT_CALL_TIMEOUT)
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool.name, arguments or {}), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise McpError(f"{qualified} timed out after {timeout:.0f}s") from exc

        return _to_tool_result(result)


def _to_tool_result(result: Any) -> ToolResult:
    """Flatten an MCP ``CallToolResult`` into text plus parsed JSON when possible."""
    is_error = bool(getattr(result, "isError", False))

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        # FastMCP wraps non-dict returns as {"result": ...}
        payload = structured.get("result", structured) if len(structured) == 1 else structured
        return ToolResult(ok=not is_error, text=json.dumps(payload, ensure_ascii=False, default=str),
                          data=payload)

    chunks: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            chunks.append(text)
        else:  # images / embedded resources — keep a marker so nothing is silently lost
            chunks.append(f"[{getattr(block, 'type', 'unknown')} content omitted]")
    text = "\n".join(chunks)

    data: Any | None = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    return ToolResult(ok=not is_error, text=text, data=data)


def stdio_client_safe(params: StdioServerParameters):
    """``stdio_client`` with a stderr sink that cannot break the transport.

    The MCP stdio client hands ``errlog`` straight to ``anyio.open_process``,
    which requires a real file object, and then decodes the child's stderr using
    the *platform default* encoding — ``gbk`` on a Chinese Windows install.  Any
    UTF-8 byte pair that is not valid GBK raises inside the reader task and takes
    the session down with it.

    So: discard child stderr by default (``os.devnull`` is a real file object),
    and opt back in with ``MINING_AGENT_MCP_STDERR=1`` when debugging.  Server
    logs stay available either way via ``MINING_AGENT_LOG_FILE``.
    """
    from mcp.client.stdio import stdio_client

    if os.getenv("MINING_AGENT_MCP_STDERR", "").strip().lower() in ("1", "true", "yes", "on"):
        return stdio_client(params)
    try:
        return stdio_client(params, errlog=open(os.devnull, "w"))  # noqa: SIM115 - lives for the session
    except TypeError:  # older MCP versions have no errlog parameter
        return stdio_client(params)
