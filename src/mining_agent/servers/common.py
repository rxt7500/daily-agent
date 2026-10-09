"""Shared helpers for the three MCP servers.

Two rules matter here:

1. **stdout belongs to the MCP transport.**  Under the ``stdio`` transport a
   stray ``print()`` corrupts the JSON-RPC frame and the client hangs.  All
   logging therefore goes to *stderr* — :func:`configure_logging` is the only
   place a handler is attached, and it never touches ``sys.stdout``.
2. **Offline must be a first-class mode, not a fallback.**  Fixtures ship with
   the repo, so the whole pipeline is demoable (and testable) with no network
   and no API keys.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar  # noqa: F401  (Any used by configure_transport_security)

import httpx

from ..config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

T = TypeVar("T")


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------
def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Attach stderr (and optionally file) handlers. Safe to call repeatedly."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = _JsonFormatter() if json_logs else _AsciiSafeFormatter(LOG_FORMAT)

    handler = logging.StreamHandler(sys.stderr)  # <- stderr, never stdout
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Optional file sink. The agent discards a child server's stderr by default
    # (see mcp_client.stdio_client_safe), so this is how server logs stay
    # retrievable during a debugging session.
    log_file = os.getenv("MINING_AGENT_LOG_FILE")
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            pass

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # httpx logs every request at INFO; keep server logs readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)


class _AsciiSafeFormatter(logging.Formatter):
    """Keep log output pure ASCII.

    The MCP stdio client captures the child's stderr and decodes it with the
    platform default encoding (``gbk`` on a Chinese Windows install).  Emitting
    UTF-8 Chinese into that stream produces mojibake at best and a decode error
    at worst, so non-ASCII characters are escaped instead.
    """

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).encode("ascii", "backslashreplace").decode("ascii")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)  # ASCII on purpose — see above


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def configure_transport_security(mcp: Any) -> None:
    """Extend the SSE transport's Host allow-list.

    MCP's SSE transport validates the ``Host`` header to block DNS rebinding,
    and ships with an empty allow-list that only accepts localhost.  A client
    reaching the server as ``http://mining-news-mcp:8001/sse`` therefore gets a
    421 before the MCP handshake even starts — which is exactly what happens
    under docker-compose.

    Configure with ``MCP_ALLOWED_HOSTS`` (comma-separated, ``host:port``) and
    ``MCP_ALLOWED_ORIGINS``.  Set ``MCP_DNS_REBINDING_PROTECTION=0`` to switch
    the check off entirely (only appropriate on a trusted network).
    """
    hosts = [h.strip() for h in (os.getenv("MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    origins = [o.strip() for o in (os.getenv("MCP_ALLOWED_ORIGINS") or "").split(",") if o.strip()]
    disabled = (os.getenv("MCP_DNS_REBINDING_PROTECTION") or "").strip().lower() in ("0", "false", "no", "off")

    if not (hosts or origins or disabled):
        return  # keep the library default (localhost only)

    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:  # older mcp without the hardening layer
        return

    settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=not disabled,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )
    mcp.settings.transport_security = settings
    get_logger(__name__).info(
        "sse transport security: rebinding_protection=%s hosts=%s",
        not disabled,
        hosts or "(default)",
    )


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def load_manifest(settings: Settings) -> dict[str, Any]:
    """Read ``data/fixtures/manifest.json``; empty dict when absent."""
    path = settings.fixtures_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        get_logger(__name__).warning("fixture manifest is not valid JSON: %s", path)
        return {}


def fixture_as_of(settings: Settings) -> date:
    """The date the bundled fixtures were generated for.

    Offline mode anchors "today" to this date so that a briefing generated
    months after the fixtures were built still shows a coherent, non-empty
    window instead of silently returning nothing.
    """
    raw = load_manifest(settings).get("fixture_as_of")
    if raw:
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def reference_date(settings: Settings, provider: str) -> date:
    """Provider-aware 'today'. Offline pins to the fixture anchor."""
    if provider == "offline":
        return fixture_as_of(settings)
    return datetime.now(timezone.utc).date()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            get_logger(__name__).warning("skipping malformed JSONL line in %s", path)
    return rows


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------
class ProviderError(RuntimeError):
    """Raised when an upstream data provider fails in a way we can't paper over."""


def http_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    retries: int = 2,
    backoff: float = 0.6,
) -> Any:
    """GET a JSON document with bounded retries on transport/5xx errors."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.get(url, params=params, headers=headers)
                if response.status_code >= 500:
                    raise ProviderError(f"upstream {response.status_code}: {url}")
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ProviderError, ValueError) as exc:  # noqa: PERF203
            last = exc
            if attempt < retries:
                time.sleep(backoff * (2**attempt))
    raise ProviderError(f"request failed after {retries + 1} attempts: {url} ({last})")


def http_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int = 40 * 1024 * 1024,
) -> bytes:
    """Stream a binary document, refusing anything over ``max_bytes``."""
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > max_bytes:
                raise ProviderError(f"document too large: {declared} bytes > {max_bytes}")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ProviderError(f"document exceeded {max_bytes} bytes while streaming")
                chunks.append(chunk)
            return b"".join(chunks)


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------
class TTLCache:
    """Tiny in-process TTL cache — keeps repeated agent calls cheap."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get_or_set(self, key: str, factory: Callable[[], T]) -> T:
        now = time.monotonic()
        hit = self._store.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        value = factory()
        self._store[key] = (now, value)
        return value


def date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return variance**0.5
