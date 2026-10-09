"""SSE transport — the path docker-compose uses.

Regression cover for a real failure: MCP's SSE transport validates the ``Host``
header against a DNS-rebinding allow-list that accepts only localhost by
default, so every docker-compose connection was rejected with 421 before the
handshake.  ``configure_transport_security`` widens the list.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mining_agent.config import Settings
from mining_agent.mcp_client import McpHub, ServerSpec
from mining_agent.servers.common import configure_transport_security

ROOT = Path(__file__).resolve().parents[1]


class _FakeMcp:
    def __init__(self) -> None:
        self.settings = type("S", (), {"transport_security": None})()


def test_configure_transport_security_is_a_noop_without_configuration(monkeypatch):
    for key in ("MCP_ALLOWED_HOSTS", "MCP_ALLOWED_ORIGINS", "MCP_DNS_REBINDING_PROTECTION"):
        monkeypatch.delenv(key, raising=False)
    fake = _FakeMcp()
    configure_transport_security(fake)
    assert fake.settings.transport_security is None, "must not override the library default"


def test_configure_transport_security_parses_hosts(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mining-news-mcp:8001, localhost:8001 ,")
    monkeypatch.delenv("MCP_DNS_REBINDING_PROTECTION", raising=False)
    fake = _FakeMcp()
    configure_transport_security(fake)

    security = fake.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["mining-news-mcp:8001", "localhost:8001"]


def test_configure_transport_security_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MCP_DNS_REBINDING_PROTECTION", "0")
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    fake = _FakeMcp()
    configure_transport_security(fake)
    assert fake.settings.transport_security.enable_dns_rebinding_protection is False


# --------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def test_sse_rejects_an_unlisted_host_and_accepts_a_listed_one():
    """Mirrors the docker case: only the configured hostname gets through.

    ``localhost`` and ``127.0.0.1`` both resolve to the same socket, but the
    ``Host`` headers differ — and the Host header is what the transport
    validates.  Allow-listing only ``localhost:<port>`` therefore proves both
    that the guard is active and that an explicit entry clears it.
    """
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "MINING_AGENT_DATA_MODE": "offline",
        "MINING_AGENT_LOG_LEVEL": "WARNING",
        "MCP_TRANSPORT": "sse",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_ALLOWED_HOSTS": f"localhost:{port}",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "mining_agent.servers.news"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(ROOT),
    )
    try:
        if not _wait_for_port(port):
            pytest.fail("SSE server never started listening")

        async def blocked():
            hub = McpHub([ServerSpec(name="blocked", transport="sse", url=f"http://127.0.0.1:{port}/sse")])
            with pytest.raises(Exception):
                async with hub:
                    pass

        asyncio.run(blocked())

        async def allowed():
            hub = McpHub(
                [ServerSpec(name="mining-news-mcp", transport="sse", url=f"http://localhost:{port}/sse")]
            )
            async with hub:
                names = {t.name for t in hub.tools}
                result = await hub.call(
                    "mining-news-mcp__search", {"query": "Pilbara", "days": 7, "limit": 2}
                )
                return names, result

        names, result = asyncio.run(allowed())
        assert names == {"search", "fetch_article"}
        assert result.ok and result.data["count"] == 2
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
