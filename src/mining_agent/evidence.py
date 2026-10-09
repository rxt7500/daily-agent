"""Citation registry.

Every fact that reaches the briefing has to be traceable back to the tool call
that produced it.  The registry assigns a stable ``[n]`` to each distinct source
and hands the map back to the model inside every tool result, so the model cites
by number instead of inventing references.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .models import Citation


def looks_like_url(value: str) -> bool:
    return urlparse(str(value)).scheme in ("http", "https")


class EvidenceStore:
    def __init__(self) -> None:
        self._citations: list[Citation] = []
        self._index: dict[tuple[str, str], int] = {}

    def add(self, kind: str, label: str, url: str) -> int:
        """Register a source and return its 1-based citation number."""
        key = (kind, str(url))
        existing = self._index.get(key)
        if existing is not None:
            return existing
        number = len(self._citations) + 1
        self._index[key] = number
        self._citations.append(
            Citation(index=number, label=label.strip() or str(url), url=str(url), kind=kind)
        )
        return number

    def citations(self) -> list[Citation]:
        return list(self._citations)

    def summary(self, limit: int = 40) -> dict[str, str]:
        """Compact ``{"1": "label"}`` map injected into tool results."""
        return {str(c.index): c.label for c in self._citations[:limit]}

    # ------------------------------------------------------------------
    def harvest(self, tool_name: str, payload: Any) -> list[int]:
        """Walk a tool result and register every source it mentions.

        Returns the citation numbers found, in the order encountered, so the
        caller can annotate what the model just received.
        """
        found: list[int] = []
        if not isinstance(payload, dict):
            return found

        def note(number: int) -> None:
            if number not in found:
                found.append(number)

        if tool_name == "search":
            for item in payload.get("results", []) or []:
                if isinstance(item, dict) and item.get("url"):
                    note(self.add("news", str(item.get("title") or item["url"]), item["url"]))

        elif tool_name == "fetch_article":
            if payload.get("url"):
                note(self.add("news", str(payload.get("title") or payload["url"]), payload["url"]))

        elif tool_name == "extract_resources":
            source = str(payload.get("source") or "")
            for table in payload.get("tables", []) or []:
                if not isinstance(table, dict):
                    continue
                project = str(table.get("project") or payload.get("project") or "resource statement")
                note(self.add("document", f"{project} — NI 43-101 resource statement", source))

        elif tool_name in ("get_price", "get_trend"):
            commodity = str(payload.get("commodity") or tool_name)
            source = str(payload.get("source") or "")
            note(self.add("data", f"{commodity} price series — {source}", source))

        elif tool_name == "list_documents":
            for doc in payload.get("documents", []) or []:
                if isinstance(doc, dict) and doc.get("pdf_url"):
                    note(self.add("document", str(doc.get("name") or doc["pdf_url"]), doc["pdf_url"]))

        return found
