"""NI 43-101 / JORC mineral resource statement extraction.

This module is deliberately free of any PDF or network dependency: it takes
plain text or a list-of-lists table and returns :class:`ResourceRow` objects.
That keeps the parsing rules unit-testable without shipping binary fixtures.

Two extraction strategies are applied in order:

1. **Ruled tables** — ``pdfplumber``'s ``extract_tables()`` output.  Preferred,
   because column boundaries are unambiguous.
2. **Text lines** — a line-oriented regex fallback for PDFs whose tables have
   no ruling lines (very common in ASX/NI 43-101 filings).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from ..models import ResourceRow

# --------------------------------------------------------------------------
# chemistry
# --------------------------------------------------------------------------
LI2O_TO_LI = 0.46457      # molar mass ratio Li2O -> Li
LI_TO_LCE = 5.32279       # molar mass ratio Li -> Li2CO3
LI2O_TO_LCE = LI2O_TO_LI * LI_TO_LCE  # 2.4728

# Order matters: the combined / total labels are tested first, otherwise
# "Measured + Indicated" would be classified as plain "Measured".
CATEGORY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Total", re.compile(r"^\s*(?:total|measured\s*(?:&|and|\+)\s*indicated|m\s*&\s*i|合计|总计)\b", re.I)),
    ("Measured", re.compile(r"^\s*(?:measured|探明)\b", re.I)),
    ("Indicated", re.compile(r"^\s*(?:indicated|控制)\b", re.I)),
    ("Inferred", re.compile(r"^\s*(?:inferred|推断)\b", re.I)),
]

# "Measured + Indicated" appears both as a label and inline in text lines.
_LABEL = r"(?:measured(?:\s*(?:&|and|\+)\s*indicated)?|indicated|inferred|total|m\s*&\s*i)"

_CATEGORY_SCAN = re.compile(
    r"\b(measured|indicated|inferred|total|m\s*&\s*i)\b", re.I
)

_NUMBER = re.compile(r"-?\d[\d\s,.'’]*")
_UNIT_TONNES = re.compile(r"\b(Mt|Mtpa|kt|ktpa|t|Bnt|Bt)\b")
_UNIT_GRADE = re.compile(r"(%\s*(?:Li2O|Li₂O|Cu|Ni|Co|Fe|Au|Sn|Ta2O5)?|ppm|g/t|gt)\b", re.I)

# "Indicated  74.5 Mt @ 1.12% Li2O  834,400 t Li2O"
_LINE_RE = re.compile(
    rf"(?P<label>{_LABEL})\b"
    r"(?P<mid>.*?)"
    r"(?P<tonnes>\d[\d,\.]*)\s*(?P<tonnes_unit>Mt|kt|t|Bt)\b"
    r"(?P<tail>.*)$",
    re.I,
)

# Unit-implied variant: "Measured   18.4   1.16   213,440   527.8" — the tonnage
# and grade units come from the column header above the row.
_UNITLESS_LINE_RE = re.compile(
    rf"^(?P<label>{_LABEL})\b\s*[:.]?\s+"
    r"(?P<tonnes>\d[\d,\.]*)\s+"
    r"(?P<grade>\d[\d,\.]*)\s*"
    r"(?P<contained>\d[\d,\.]*)?",
    re.I,
)

_HEADER_LINE_RE = re.compile(r"(classification|category|resource|类别|资源量)", re.I)


def parse_number(raw: Any) -> float | None:
    """Parse ``1,234.5`` / ``1 234,5`` / ``1'234.5`` → float. ``None`` on junk."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text or text.lower() in {"-", "–", "—", "n/a", "na", "nil", "none"}:
        return None

    cleaned = re.sub(r"[^\d,.\-']", "", text)
    if not cleaned:
        return None

    has_comma, has_dot = "," in cleaned, "." in cleaned
    if has_comma and has_dot:
        # Whichever separator appears last is the decimal separator.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        head, _, tail = cleaned.rpartition(",")
        # "1,234" / "1,234,567" are thousands; "1,5" is a decimal comma.
        cleaned = cleaned.replace(",", "") if len(tail) == 3 and head else cleaned.replace(",", ".")
    cleaned = cleaned.replace("'", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def lce_kt(tonnes_mt: float | None, grade_pct: float | None) -> float | None:
    """Thousand tonnes of Li2CO3 equivalent from ore tonnage and % Li2O."""
    if tonnes_mt is None or grade_pct is None:
        return None
    contained_li2o_t = tonnes_mt * 1_000_000 * (grade_pct / 100.0)
    return contained_li2o_t * LI2O_TO_LCE / 1000.0


def classify(label: str) -> str | None:
    for category, pattern in CATEGORY_PATTERNS:
        if pattern.search(label):
            return category
    return None


# --------------------------------------------------------------------------
# table-shaped extraction
# --------------------------------------------------------------------------
def _norm(cell: Any) -> str:
    return re.sub(r"\s+", " ", str(cell or "")).strip()


def _find_header(table: Sequence[Sequence[Any]]) -> int | None:
    """Locate the header row of a resource table.

    Most filings put units in the header (``Tonnes (Mt)``, ``Grade (% Li2O)``)
    and the classification word in the *data* rows below it, so a header is
    accepted either when it names a category itself or when the row beneath it
    does.
    """
    for idx, row in enumerate(table[:6]):
        joined = " ".join(_norm(c) for c in row).lower()
        if not any(k in joined for k in ("tonne", "grade", "mt", "kt", "resource")):
            continue
        if _CATEGORY_SCAN.search(joined):
            return idx
        for following in table[idx + 1 : idx + 4]:
            if any(classify(_norm(cell)) for cell in following if _norm(cell)):
                return idx
    return None


def _column_roles(header: Sequence[Any]) -> dict[str, int]:
    roles: dict[str, int] = {}
    for idx, cell in enumerate(header):
        text = _norm(cell).lower()
        if not text:
            continue
        if "category" in text or "classification" in text or text in {"resource", "class"}:
            roles.setdefault("category", idx)
        elif "contained" in text or "metal" in text or "lce" in text or "li2co3" in text:
            roles.setdefault("contained", idx)
        elif "tonne" in text or re.search(r"\b(mt|kt|bt)\b", text) or "million" in text:
            roles.setdefault("tonnes", idx)
        elif "grade" in text or "li2o" in text or "%" in text or "ppm" in text:
            roles.setdefault("grade", idx)
        elif "project" in text or "deposit" in text or "asset" in text:
            roles.setdefault("project", idx)
    return roles


def rows_from_tables(
    tables: Iterable[Sequence[Sequence[Any]]],
    *,
    project: str,
    effective_date: str | None = None,
    source_page: int | None = None,
) -> list[ResourceRow]:
    """Convert ``pdfplumber`` tables into normalised resource rows."""
    rows: list[ResourceRow] = []
    for table in tables:
        header_idx = _find_header(table)
        if header_idx is None:
            continue
        roles = _column_roles(table[header_idx])
        for raw_row in table[header_idx + 1 :]:
            cells = [_norm(c) for c in raw_row]
            if not any(cells):
                continue
            label_cell = cells[roles["category"]] if "category" in roles and roles["category"] < len(cells) else cells[0]
            category = classify(label_cell) or classify(" ".join(cells))
            if category is None:
                continue

            tonnes = grade = contained = None
            tonnes_unit, grade_unit = "Mt", "%"
            if "tonnes" in roles and roles["tonnes"] < len(cells):
                tonnes = parse_number(cells[roles["tonnes"]])
                match = _UNIT_TONNES.search(cells[roles["tonnes"]])
                if match:
                    tonnes_unit = match.group(1)
            if "grade" in roles and roles["grade"] < len(cells):
                grade = parse_number(cells[roles["grade"]])
                match = _UNIT_GRADE.search(cells[roles["grade"]])
                if match:
                    grade_unit = match.group(1).replace(" ", "")
            if "contained" in roles and roles["contained"] < len(cells):
                contained = parse_number(cells[roles["contained"]])

            if tonnes is None and grade is None:
                continue

            tonnes_mt = _to_mt(tonnes, tonnes_unit)
            grade_pct = _to_pct(grade, grade_unit)
            rows.append(
                ResourceRow(
                    project=project,
                    category=category,  # type: ignore[arg-type]
                    raw_label=label_cell,
                    tonnes_mt=tonnes_mt,
                    grade=grade,
                    grade_unit=grade_unit,
                    contained_metal_t=contained,
                    contained_lce_kt=round(lce_kt(tonnes_mt, grade_pct), 1) if grade_pct else None,
                    effective_date=effective_date,
                    source_page=source_page,
                )
            )
    return rows


def _to_mt(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    factor = {"Bt": 1000.0, "Bnt": 1000.0, "Mt": 1.0, "kt": 0.001, "t": 0.000001}.get(unit, 1.0)
    return round(value * factor, 4)


def _to_pct(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    if unit.lower() in ("ppm",):
        return value / 10_000.0
    if unit.lower() in ("g/t", "gt"):
        return value / 10_000.0
    return value


# --------------------------------------------------------------------------
# text-line extraction
# --------------------------------------------------------------------------
def _clean_grade_unit(raw: str | None, default: str = "%") -> str:
    if not raw:
        return default
    text = re.sub(r"\s+", "", raw)
    if text.startswith("%"):
        return "%"
    lowered = text.lower()
    if lowered.startswith("ppm"):
        return "ppm"
    if lowered.startswith("g/t") or lowered.startswith("gt"):
        return "g/t"
    return default


def rows_from_text(
    text: str,
    *,
    project: str,
    effective_date: str | None = None,
    source_page: int | None = None,
) -> list[ResourceRow]:
    """Line-oriented fallback for PDFs with borderless tables.

    Two layouts are handled: rows that carry their own unit (``18.4 Mt @ 1.16%
    Li2O``) and rows whose units live in a column header above them
    (``Measured   18.4   1.16``).
    """
    rows: list[ResourceRow] = []
    tonnes_unit, grade_unit = "Mt", "%"

    for line in text.splitlines():
        flat = _norm(line)
        if len(flat) < 8:
            continue

        if _HEADER_LINE_RE.search(flat) and re.search(r"tonne|grade|\bmt\b|%", flat, re.I):
            unit_match = _UNIT_TONNES.search(flat)
            if unit_match:
                tonnes_unit = unit_match.group(1)
            grade_unit = _clean_grade_unit(
                (_UNIT_GRADE.search(flat).group(1) if _UNIT_GRADE.search(flat) else None),
                grade_unit,
            )
            continue

        match = _LINE_RE.search(flat)
        if match:
            label = match.group("label")
            row_tonnes_unit = match.group("tonnes_unit")
            tonnes = parse_number(match.group("tonnes"))
            tail = match.group("tail")

            grade = None
            row_grade_unit = grade_unit
            grade_match = re.search(r"(\d[\d,\.]*)\s*(%\s*[A-Za-z0-9₂]*|ppm|g/t)", tail, re.I)
            if grade_match:
                grade = parse_number(grade_match.group(1))
                row_grade_unit = _clean_grade_unit(grade_match.group(2), grade_unit)

            contained = None
            contained_match = re.search(r"([\d][\d,\.]*)\s*(kt|Mt|t)\b", tail, re.I)
            if contained_match:
                value = parse_number(contained_match.group(1))
                unit = contained_match.group(2)
                contained = value * {"Mt": 1_000_000.0, "kt": 1_000.0, "t": 1.0}.get(unit, 1.0)
        else:
            match = _UNITLESS_LINE_RE.match(flat)
            if not match:
                continue
            label = match.group("label")
            row_tonnes_unit = tonnes_unit
            tonnes = parse_number(match.group("tonnes"))
            grade = parse_number(match.group("grade"))
            row_grade_unit = grade_unit
            contained = parse_number(match.group("contained"))

        category = classify(label)
        if category is None:
            continue
        # A grade outside 0–100% is almost certainly a mis-parse (a year, a
        # hole ID, a contained-metal figure). Drop the grade, keep the tonnage.
        if grade is not None and not (0 < grade < 100) and row_grade_unit in ("%", "ppm"):
            grade = None

        tonnes_mt = _to_mt(tonnes, row_tonnes_unit)
        grade_pct = _to_pct(grade, row_grade_unit)
        rows.append(
            ResourceRow(
                project=project,
                category=category,  # type: ignore[arg-type]
                raw_label=label.strip(),
                tonnes_mt=tonnes_mt,
                grade=grade,
                grade_unit=row_grade_unit,
                contained_metal_t=contained,
                contained_lce_kt=round(lce_kt(tonnes_mt, grade_pct), 1) if grade_pct else None,
                effective_date=effective_date,
                source_page=source_page,
            )
        )
    return rows


def dedupe(rows: Sequence[ResourceRow]) -> list[ResourceRow]:
    """Drop exact duplicates, preserving document order.

    Order is preserved rather than sorted: filings present Measured / Indicated
    / Measured+Indicated / Inferred / Total in that sequence and re-sorting by
    category would scramble the subtotal rows.
    """
    seen: set[tuple[Any, ...]] = set()
    out: list[ResourceRow] = []
    for row in rows:
        key = (row.project, row.category, row.raw_label, row.tonnes_mt, row.grade, row.grade_unit)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


# --------------------------------------------------------------------------
# document-level helpers
# --------------------------------------------------------------------------
def guess_project_name(text: str, fallback: str) -> str:
    """Best-effort project name from the first page of a technical report."""
    patterns = [
        r"([A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+){0,4}\s+(?:Lithium|Gold|Copper|Nickel|Iron|Mineral|Mining)\s+Project)",
        r"([A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+){0,3}\s+Project)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _norm(match.group(1))
    return fallback


def find_effective_date(text: str) -> str | None:
    """Locate the 'effective date' of the resource statement."""
    match = re.search(
        r"effective\s+date[^0-9]{0,40}(\d{1,2}\s+\w+\s+\d{4}|\d{4}-\d{2}-\d{2}|\w+\s+\d{1,2},\s*\d{4})",
        text,
        re.I,
    )
    return _norm(match.group(1)) if match else None


def find_cutoff_grade(text: str) -> str | None:
    match = re.search(r"cut[\s\-]?off\s+grade[^0-9]{0,30}([\d.]+\s*%\s*[A-Za-z0-9₂]*)", text, re.I)
    return _norm(match.group(1)) if match else None
