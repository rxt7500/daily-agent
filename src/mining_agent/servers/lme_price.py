"""lme-price-mcp — commodity price quotes and trends for mining research.

Tools
-----
``get_price(commodity, date)``   single quote on (or before) a date
``get_trend(commodity, days)``   daily series + change / range / volatility
``list_commodities()``           what this server can quote, with units

Providers
---------
``offline``  bundled daily series (default; no network, no keys)
``http``     a JSON price endpoint set via ``MINING_AGENT_PRICE_BASE_URL``
"""

from __future__ import annotations

import csv
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from dateutil import parser as dateparser
from mcp.server.fastmcp import FastMCP

from ..config import Settings
from ..models import PricePoint
from .common import (
    ProviderError,
    TTLCache,
    configure_logging,
    configure_transport_security,
    get_logger,
    http_json,
    pct_change,
    reference_date,
    stdev,
)

log = get_logger("lme-price-mcp")

mcp = FastMCP(
    "lme-price-mcp",
    instructions=(
        "Battery-material and base-metal price data. Use list_commodities() to see "
        "what is quotable, get_price() for a single day, and get_trend() for a "
        "series with change and volatility statistics."
    ),
)

_CACHE = TTLCache(ttl_seconds=120.0)

# canonical key -> (display name, unit, aliases)
CATALOG: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "lithium-carbonate": (
        "Lithium Carbonate (battery grade)",
        "USD/t",
        ("lithium", "li2co3", "lithium carbonate", "碳酸锂", "lc", "battery grade carbonate"),
    ),
    "lithium-hydroxide": (
        "Lithium Hydroxide (monohydrate)",
        "USD/t",
        ("lioh", "lithium hydroxide", "氢氧化锂", "hydroxide"),
    ),
    "spodumene": (
        "Spodumene concentrate (6% Li2O, CFR China)",
        "USD/t",
        ("spod", "sc6", "spodumene concentrate", "锂辉石", "6%"),
    ),
    "nickel": ("Nickel (LME)", "USD/t", ("ni", "lme nickel", "镍")),
    "copper": ("Copper (LME)", "USD/t", ("cu", "lme copper", "铜")),
}


def normalize_commodity(raw: str) -> str:
    """Map a user-provided name onto a canonical catalog key.

    Short latin aliases (``ni``, ``cu``, ``lc``) are matched exactly rather than
    as substrings — otherwise ``ni`` matches inside "u-ni-obtainium" and every
    unknown commodity silently resolves to nickel.
    """
    if not raw:
        raise ProviderError("commodity is required")
    text = raw.strip().lower()
    if text in CATALOG:
        return text
    squashed = text.replace("_", "-").replace(" ", "-")
    if squashed in CATALOG:
        return squashed

    for key, (name, _unit, aliases) in CATALOG.items():
        if text == name.lower() or text == key.replace("-", " "):
            return key
        for alias in aliases:
            if _alias_matches(alias, text):
                return key
    known = ", ".join(sorted(CATALOG))
    raise ProviderError(f"unknown commodity {raw!r}. Known commodities: {known}")


def _alias_matches(alias: str, text: str) -> bool:
    alias = alias.strip().lower()
    if not alias:
        return False
    if alias == text:
        return True
    # CJK, or a latin alias long enough to not collide with other words.
    if not alias.isascii() or len(alias) >= 4:
        return alias in text
    return False


def _parse_date(raw: Any) -> date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    try:
        return dateparser.parse(str(raw)).date()
    except (ValueError, OverflowError, TypeError) as exc:
        raise ProviderError(f"could not parse date {raw!r} (expected YYYY-MM-DD)") from exc


# --------------------------------------------------------------------------
# offline provider
# --------------------------------------------------------------------------
class OfflinePriceProvider:
    name = "offline"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.fixtures_dir / "prices.csv"

    def _series(self) -> dict[str, list[tuple[date, float]]]:
        def load() -> dict[str, list[tuple[date, float]]]:
            if not self.path.is_file():
                raise ProviderError(f"offline price fixtures not found: {self.path}")
            data: dict[str, list[tuple[date, float]]] = {}
            with self.path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    key = row["commodity"].strip()
                    try:
                        entry = (date.fromisoformat(row["date"].strip()), float(row["close"]))
                    except (KeyError, ValueError):
                        continue
                    data.setdefault(key, []).append(entry)
            for points in data.values():
                points.sort(key=lambda p: p[0])
            return data

        return _CACHE.get_or_set("offline-series", load)

    def _unit(self, commodity: str) -> str:
        return CATALOG[commodity][1]

    def get_price(self, commodity: str, on: date | None = None) -> dict[str, Any]:
        series = self._series().get(commodity)
        if not series:
            raise ProviderError(f"no offline series for {commodity!r}")
        target = on or reference_date(self.settings, "offline")
        warnings: list[str] = []
        if target > series[-1][0]:
            warnings.append(
                f"requested {target.isoformat()} is after the last fixture date "
                f"{series[-1][0].isoformat()}; returning the latest available quote"
            )
            target = series[-1][0]
        if target < series[0][0]:
            warnings.append(f"requested {target.isoformat()} predates the series; using the first quote")
            target = series[0][0]

        chosen = series[0]
        for point_date, close in series:
            if point_date <= target:
                chosen = (point_date, close)
            else:
                break
        return {
            "commodity": commodity,
            "date": chosen[0].isoformat(),
            "requested_date": (on or target).isoformat(),
            "close": chosen[1],
            "unit": self._unit(commodity),
            "currency": "USD",
            "source": f"offline fixture series ({self.path.name})",
            "mode": "offline",
            "warnings": warnings,
        }

    def get_trend(self, commodity: str, days: int) -> dict[str, Any]:
        series = self._series().get(commodity)
        if not series:
            raise ProviderError(f"no offline series for {commodity!r}")
        end = min(reference_date(self.settings, "offline"), series[-1][0])
        start = end - timedelta(days=days)
        points = [(d, c) for d, c in series if start <= d <= end]
        if len(points) < 2:
            points = series[-min(days, len(series)) :]
            start = points[0][0]
        else:
            start = points[0][0]
        return _build_trend(commodity, points, start, end, days, self._unit(commodity),
                            f"offline fixture series ({self.path.name})", "offline")


# --------------------------------------------------------------------------
# live provider
# --------------------------------------------------------------------------
class HttpPriceProvider:
    """Thin adapter over an HTTP JSON price API.

    Expected response shapes::

        GET {base}/price?commodity=…&date=…    -> {"close": 10250.0, "date": "…", "unit": "USD/t"}
        GET {base}/trend?commodity=…&days=…    -> {"points": [{"date": "…", "close": …}, …]}
    """

    name = "http"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = (settings.price_base_url or "").rstrip("/")
        if not self.base:
            raise ProviderError("MINING_AGENT_PRICE_BASE_URL is required for the http provider")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.price_api_key}"} if self.settings.price_api_key else {}

    def get_price(self, commodity: str, on: date | None = None) -> dict[str, Any]:
        payload = http_json(
            f"{self.base}/price",
            params={"commodity": commodity, **({"date": on.isoformat()} if on else {})},
            headers=self._headers(),
        )
        return {
            "commodity": commodity,
            "date": str(payload.get("date")),
            "requested_date": (on or _parse_date(payload.get("date")) or date.today()).isoformat(),
            "close": float(payload["close"]),
            "unit": payload.get("unit", CATALOG[commodity][1]),
            "currency": payload.get("currency", "USD"),
            "source": payload.get("source", self.base),
            "mode": "live",
            "warnings": [],
        }

    def get_trend(self, commodity: str, days: int) -> dict[str, Any]:
        payload = http_json(
            f"{self.base}/trend",
            params={"commodity": commodity, "days": days},
            headers=self._headers(),
        )
        raw = payload.get("points") or []
        points = [(_parse_date(p["date"]), float(p["close"])) for p in raw if p.get("close") is not None]
        points = [(d, c) for d, c in points if d is not None]
        if len(points) < 2:
            raise ProviderError(f"upstream returned fewer than 2 points for {commodity!r}")
        points.sort(key=lambda p: p[0])
        return _build_trend(
            commodity, points, points[0][0], points[-1][0], days,
            payload.get("unit", CATALOG[commodity][1]),
            payload.get("source", self.base), "live",
        )


def _build_trend(
    commodity: str,
    points: list[tuple[date, float]],
    start: date,
    end: date,
    days: int,
    unit: str,
    source: str,
    mode: str,
) -> dict[str, Any]:
    closes = [c for _, c in points]
    returns = [pct_change(closes[i], closes[i - 1]) / 100.0 for i in range(1, len(closes))]
    latest, first = closes[-1], closes[0]
    return {
        "commodity": commodity,
        "unit": unit,
        "currency": "USD",
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "points": [PricePoint(date=d, close=c).model_dump(mode="json") for d, c in points],
        "latest": latest,
        "change_pct": round(pct_change(latest, first), 2),
        "change_abs": round(latest - first, 2),
        "high": max(closes),
        "low": min(closes),
        "volatility_pct": round(stdev(returns) * 100.0, 2),
        "source": source,
        "mode": mode,
    }


def _provider(settings: Settings) -> OfflinePriceProvider | HttpPriceProvider:
    return HttpPriceProvider(settings) if settings.resolve_price_provider() == "http" else OfflinePriceProvider(settings)


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
@mcp.tool()
def get_price(commodity: str, date: str | None = None) -> dict[str, Any]:
    """Get the closing price for a commodity.

    Args:
        commodity: e.g. "lithium-carbonate", "spodumene", "nickel", "copper".
            Aliases such as "碳酸锂" or "SC6" are accepted.
        date: ISO date (YYYY-MM-DD). Defaults to the latest available session.
            If the market was closed, the most recent prior session is returned.
    """
    settings = Settings.from_env()
    key = normalize_commodity(commodity)
    result = _provider(settings).get_price(key, _parse_date(date))
    log.info("get_price %s %s -> %s", key, result["date"], result["close"])
    return result


@mcp.tool()
def get_trend(commodity: str, days: int = 30) -> dict[str, Any]:
    """Get a daily price series with change, range and volatility statistics.

    Args:
        commodity: e.g. "lithium-carbonate", "spodumene".
        days: Look-back window in days (2-365).

    Returns:
        ``points`` (date/close), ``change_pct`` over the window,
        ``volatility_pct`` (standard deviation of daily returns) and the
        ``high``/``low`` range.
    """
    settings = Settings.from_env()
    key = normalize_commodity(commodity)
    days = max(2, min(int(days), 365))
    result = _provider(settings).get_trend(key, days)
    log.info(
        "get_trend %s %sd -> %s%% vol=%s%%",
        key,
        days,
        result["change_pct"],
        result["volatility_pct"],
    )
    return result


@mcp.tool()
def list_commodities() -> dict[str, Any]:
    """List every commodity this server can quote, with unit and aliases."""
    settings = Settings.from_env()
    return {
        "mode": settings.resolve_price_provider(),
        "reference_date": reference_date(settings, settings.resolve_price_provider()).isoformat(),
        "commodities": [
            {"key": key, "name": name, "unit": unit, "aliases": list(aliases)}
            for key, (name, unit, aliases) in sorted(CATALOG.items())
        ],
    }


# --------------------------------------------------------------------------
def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_json)
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        configure_transport_security(mcp)
        mcp.settings.host = os.getenv("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8003"))
    log.info(
        "lme-price-mcp starting (transport=%s provider=%s)",
        transport,
        settings.resolve_price_provider(),
    )
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
