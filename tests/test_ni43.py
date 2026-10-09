"""NI 43-101 parsing rules."""

from __future__ import annotations

import pytest

from mining_agent.servers import ni43

BORDERLESS = """\
Mineral Resource Statement
Classification Tonnes (Mt) Grade (% Li2O) Contained Li2O (t) Contained LCE (kt)
Measured 18.4 1.16 213,440 527.8
Indicated 54.2 1.03 558,260 1,380.5
Measured + Indicated 72.6 1.06 769,560 1,903.0
Inferred 26.7 0.91 242,970 600.8
Total 99.3 1.03 1,022,790 2,529.2
"""

INLINE_UNITS = """\
Indicated 74.5 Mt @ 1.12% Li2O 834,400 t Li2O
Inferred 22.1 Mt @ 0.88% Li2O 194,480 t Li2O
"""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.5", 1234.5),
        ("1,234,567", 1234567.0),
        ("18.4", 18.4),
        ("1 234,5", 1234.5),
        ("1'234.5", 1234.5),
        ("—", None),
        ("n/a", None),
        ("", None),
        (None, None),
        (42, 42.0),
    ],
)
def test_parse_number(raw, expected):
    assert ni43.parse_number(raw) == expected


def test_lce_conversion_matches_stoichiometry():
    # 1 Mt at 1% Li2O is the industry rule of thumb: ~24.7 kt LCE.
    assert ni43.lce_kt(1.0, 1.0) == pytest.approx(24.728, rel=1e-3)
    assert ni43.lce_kt(None, 1.0) is None
    assert ni43.lce_kt(1.0, None) is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Measured", "Measured"),
        ("Indicated", "Indicated"),
        ("Inferred", "Inferred"),
        ("Total", "Total"),
        ("Measured + Indicated", "Total"),
        ("measured and indicated", "Total"),
        ("M & I", "Total"),
        ("Operator", None),
    ],
)
def test_classify_prefers_combined_labels(label, expected):
    assert ni43.classify(label) == expected


def test_rows_from_text_borderless_table():
    rows = ni43.rows_from_text(BORDERLESS, project="Demo")
    assert [r.category for r in rows] == [
        "Measured",
        "Indicated",
        "Total",
        "Inferred",
        "Total",
    ]
    assert rows[0].tonnes_mt == 18.4
    assert rows[0].grade == 1.16
    assert rows[0].grade_unit == "%"
    assert rows[0].contained_lce_kt == pytest.approx(527.8, abs=0.5)
    assert rows[2].raw_label == "Measured + Indicated"


def test_rows_from_text_inline_units():
    rows = ni43.rows_from_text(INLINE_UNITS, project="Demo")
    assert len(rows) == 2
    assert rows[0].category == "Indicated"
    assert rows[0].tonnes_mt == 74.5
    assert rows[0].grade == 1.12
    assert rows[0].contained_metal_t == pytest.approx(834_400)


def test_rows_from_tables_header_units():
    table = [
        ["Classification", "Tonnes (Mt)", "Grade (% Li2O)", "Contained Li2O (t)", "Contained LCE (kt)"],
        ["Measured", "18.4", "1.16", "213,440", "527.8"],
        ["Inferred", "26.7", "0.91", "242,970", "600.8"],
    ]
    rows = ni43.rows_from_tables([table], project="Demo")
    assert [r.category for r in rows] == ["Measured", "Inferred"]
    assert rows[1].tonnes_mt == 26.7
    assert rows[1].contained_metal_t == pytest.approx(242_970)


def test_rows_from_tables_ignores_unrelated_tables():
    table = [["Item", "Cost"], ["Drilling", "1,200"]]
    assert ni43.rows_from_tables([table], project="Demo") == []


def test_rows_from_text_rejects_implausible_grade():
    # "2026" parsed as a grade would poison the LCE figure; it is dropped instead.
    text = "Inferred 12.5 Mt @ 2026 % Li2O\n"
    rows = ni43.rows_from_text(text, project="Demo")
    assert rows and rows[0].grade is None
    assert rows[0].contained_lce_kt is None


def test_dedupe_preserves_document_order():
    rows = ni43.rows_from_text(BORDERLESS, project="Demo")
    deduped = ni43.dedupe([*rows, *rows])
    assert len(deduped) == len(rows)
    assert [r.raw_label for r in deduped] == [r.raw_label for r in rows]


def test_document_metadata_helpers():
    text = (
        "Mt Pilbara Lithium Project\n"
        "Cut-off grade: 0.40% Li2O\n"
        "The effective date of the estimate is 30 September 2026.\n"
    )
    assert ni43.guess_project_name(text, fallback="x") == "Mt Pilbara Lithium Project"
    assert ni43.find_cutoff_grade(text) == "0.40% Li2O"
    assert ni43.find_effective_date(text) == "30 September 2026"
