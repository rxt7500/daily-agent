"""mineral-pdf-mcp — extract NI 43-101 / JORC mineral resources from a PDF.

Tools
-----
``extract_resources(pdf_url)``
    Parse Indicated / Inferred (/ Measured / Total) resource rows out of a
    technical report, with Li2CO3-equivalent tonnage computed.
``list_documents()``
    Enumerate the documents available to this server.  In offline mode this is
    the bundled fixture set, which lets the agent discover a valid ``pdf_url``
    instead of guessing one.

Inputs
------
``pdf_url`` accepts ``https://…``, ``file:///…`` or a plain filesystem path.
``offline`` mode resolves the bundled fixtures only; ``http`` mode downloads.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from mcp.server.fastmcp import FastMCP

from ..config import Settings
from ..models import ResourceRow, ResourceTable
from . import ni43
from .common import (
    ProviderError,
    configure_logging,
    configure_transport_security,
    fixture_as_of,
    get_logger,
    http_bytes,
)

log = get_logger("mineral-pdf-mcp")

mcp = FastMCP(
    "mineral-pdf-mcp",
    instructions=(
        "Reads NI 43-101 / JORC technical reports and returns mineral resource "
        "statements (Measured / Indicated / Inferred) with tonnage, grade and "
        "Li2CO3-equivalent content. Call list_documents() first when you do not "
        "already have a pdf_url."
    ),
)


# --------------------------------------------------------------------------
# document loading
# --------------------------------------------------------------------------
def _local_path(pdf_url: str, settings: Settings) -> Path | None:
    """Resolve a URL/path to an on-disk file, or None if it is remote.

    Handles both ``file:///E:/x.pdf`` and the ``file://E:/x.pdf`` form that
    naive string concatenation produces on Windows.
    """
    parsed = urlparse(pdf_url)
    if parsed.scheme in ("http", "https"):
        return None
    if parsed.scheme == "file":
        raw = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            raw = f"{parsed.netloc}{raw}"  # file://E:/x -> E: + /x
        return Path(raw)
    candidate = Path(pdf_url)
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    # Bare fixture name, e.g. "ni43-101_pilbara_lithium.pdf"
    fixture = settings.fixtures_dir / Path(pdf_url).name
    if fixture.is_file():
        return fixture
    root = Path(settings.data_dir).parent / Path(pdf_url)
    return root if root.is_file() else None


def load_document(pdf_url: str, settings: Settings) -> tuple[bytes, str]:
    """Return ``(pdf_bytes, resolved_source)`` for a URL or path.

    A local file is reported back as a ``file://`` URI so that the same document
    reached via ``list_documents()`` and via an explicit path registers as one
    citation instead of two.
    """
    local = _local_path(pdf_url, settings)
    if local is not None:
        return local.read_bytes(), local.resolve().as_uri()

    if urlparse(pdf_url).scheme not in ("http", "https"):
        raise ProviderError(
            f"document not found: {pdf_url}. Call list_documents() to see what is available."
        )
    if not settings.pdf_allow_network or settings.resolve_pdf_provider() == "offline":
        raise ProviderError(
            "network access is disabled for mineral-pdf-mcp "
            "(MINING_AGENT_DATA_MODE=offline). Use a bundled document from list_documents()."
        )
    try:
        return http_bytes(pdf_url, max_bytes=settings.pdf_max_bytes), pdf_url
    except Exception as exc:  # noqa: BLE001 - surface a useful message instead of a raw httpx error
        raise ProviderError(f"could not download {pdf_url}: {exc}") from exc


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
def _page_texts(pdf_bytes: bytes) -> tuple[list[str], list[list[list[list[str]]]]]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise ProviderError("pdfplumber is not installed; run `pip install -r requirements.txt`") from exc

    texts: list[str] = []
    tables: list[list[list[list[str]]]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
            try:
                tables.append(page.extract_tables() or [])
            except Exception as exc:  # pragma: no cover - malformed table geometry
                log.warning("table extraction failed on page %s: %s", page.page_number, exc)
                tables.append([])
    return texts, tables


def extract(pdf_bytes: bytes, source: str, settings: Settings) -> dict[str, Any]:
    texts, tables = _page_texts(pdf_bytes)
    full_text = "\n".join(texts)
    if not full_text.strip() and not any(tables):
        raise ProviderError(
            f"no extractable text in {source} — the PDF is probably a scanned image "
            "and would need OCR"
        )

    doc_project = ni43.guess_project_name(full_text, fallback=Path(source).stem)
    effective_date = ni43.find_effective_date(full_text)
    cut_off = ni43.find_cutoff_grade(full_text)
    warnings: list[str] = []

    def collect(use_tables: bool) -> dict[str, dict[str, Any]]:
        """Rows grouped by project, in first-seen document order.

        A single report often covers several properties, so the project name,
        cut-off grade and effective date are re-read per page and carried
        forward onto continuation pages that have no heading of their own.
        """
        grouped: dict[str, dict[str, Any]] = {}
        current = doc_project
        for page_no, text in enumerate(texts, start=1):
            heading = ni43.guess_project_name(text, fallback=current)
            page_effective = ni43.find_effective_date(text) or effective_date
            page_cutoff = ni43.find_cutoff_grade(text) or cut_off
            if use_tables:
                page_rows = ni43.rows_from_tables(
                    tables[page_no - 1],
                    project=heading,
                    effective_date=page_effective,
                    source_page=page_no,
                )
            else:
                page_rows = ni43.rows_from_text(
                    text, project=heading, effective_date=page_effective, source_page=page_no
                )
            if page_rows:
                current = heading
                bucket = grouped.setdefault(
                    heading,
                    {"rows": [], "effective_date": page_effective, "cut_off_grade": page_cutoff},
                )
                bucket["rows"].extend(page_rows)
                bucket["effective_date"] = bucket["effective_date"] or page_effective
                bucket["cut_off_grade"] = bucket["cut_off_grade"] or page_cutoff
        return grouped

    grouped = collect(use_tables=True)
    strategy = "table"
    if not grouped:
        strategy = "text"
        warnings.append("no ruled table matched; fell back to line-oriented extraction")
        grouped = collect(use_tables=False)

    resource_tables: list[ResourceTable] = []
    for project, bucket in grouped.items():
        rows = ni43.dedupe(bucket["rows"])
        pages = sorted({r.source_page for r in rows if r.source_page})
        resource_tables.append(
            ResourceTable(
                project=project,
                commodity="Li2O",
                effective_date=bucket["effective_date"],
                cut_off_grade=bucket["cut_off_grade"],
                rows=rows,
                source=source,
                source_page=pages[0] if pages else None,
            )
        )

    total_rows = sum(len(t.rows) for t in resource_tables)
    if not total_rows:
        warnings.append(
            "no Measured/Indicated/Inferred resource rows were recognised in this document"
        )

    return {
        "source": source,
        "pages": len(texts),
        "strategy": strategy,
        "project": resource_tables[0].project if resource_tables else doc_project,
        "projects": [t.project for t in resource_tables],
        "effective_date": effective_date,
        "cut_off_grade": cut_off,
        "row_count": total_rows,
        "tables": [t.model_dump(mode="json") for t in resource_tables],
        "warnings": warnings,
        "mode": "offline" if _local_path(source, settings) is not None else "live",
    }


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
@mcp.tool()
def extract_resources(pdf_url: str) -> dict[str, Any]:
    """Extract the NI 43-101 / JORC mineral resource statement from a PDF.

    Args:
        pdf_url: ``https://`` URL, ``file://`` URL, or a path to a local PDF.
            Use list_documents() to discover bundled documents.

    Returns:
        ``tables`` — a list of resource statements, each with ``rows`` carrying
        ``project``, ``category`` (Measured/Indicated/Inferred/Total),
        ``tonnes_mt``, ``grade`` + ``grade_unit``, and ``contained_lce_kt``
        (thousand tonnes Li2CO3 equivalent, derived from the Li2O grade).
        ``strategy`` reports whether a ruled table or the text fallback was used.
    """
    settings = Settings.from_env()
    pdf_bytes, source = load_document(pdf_url, settings)
    result = extract(pdf_bytes, source, settings)
    log.info(
        "extract_resources %s -> %s rows (%s)", source, result["row_count"], result["strategy"]
    )
    return result


@mcp.tool()
def list_documents() -> dict[str, Any]:
    """List the PDF documents this server can read without network access."""
    settings = Settings.from_env()
    fixtures = sorted(settings.fixtures_dir.glob("*.pdf"))
    documents = []
    for path in fixtures:
        documents.append(
            {
                "name": path.name,
                "pdf_url": path.as_uri(),  # correct file:// URI on every platform
                "bytes": path.stat().st_size,
            }
        )
    return {
        "fixture_as_of": fixture_as_of(settings).isoformat(),
        "count": len(documents),
        "documents": documents,
        "mode": "offline" if settings.resolve_pdf_provider() == "offline" else "live",
    }


# --------------------------------------------------------------------------
def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_json)
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        configure_transport_security(mcp)
        mcp.settings.host = os.getenv("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8002"))
    log.info(
        "mineral-pdf-mcp starting (transport=%s provider=%s)",
        transport,
        settings.resolve_pdf_provider(),
    )
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
