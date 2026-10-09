"""Generate the offline fixture set.

Everything under ``data/fixtures`` is **synthetic demo data** produced by this
script — see ``data/fixtures/manifest.json`` for the disclaimer.  Regenerating
is deterministic: the price walks are seeded and the PDF layout is fixed.

    python scripts/make_fixtures.py [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "fixtures"

DEFAULT_AS_OF = date(2026, 10, 8)

DISCLAIMER = (
    "SYNTHETIC DEMO DATA. Company names, project names, resource figures, news "
    "headlines and prices in this fixture set are fabricated for demonstration "
    "purposes. They are modelled on real hard-rock lithium mining but do not "
    "describe any real company, project or market price."
)


# --------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------
# key: (start_price, daily_vol, annual_drift, seed, floor)
PRICE_SPECS = {
    "lithium-carbonate": (14_200.0, 0.0140, -0.30, 11, 8_000.0),
    "lithium-hydroxide": (13_400.0, 0.0150, -0.28, 12, 7_800.0),
    "spodumene": (1_050.0, 0.0160, -0.32, 13, 620.0),
    "nickel": (16_500.0, 0.0110, -0.08, 14, 13_000.0),
    "copper": (8_450.0, 0.0090, 0.06, 15, 7_200.0),
}

PRICE_START = date(2024, 1, 1)
PRICE_END = date(2027, 12, 31)


def business_days(start: date, end: date):
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def generate_prices() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    days = list(business_days(PRICE_START, PRICE_END))
    for commodity, (start_price, vol, drift, seed, floor) in PRICE_SPECS.items():
        rng = random.Random(seed)
        price = start_price
        daily_drift = drift / 252.0
        for day in days:
            shock = rng.gauss(0.0, vol)
            # mild mean reversion keeps the walk inside a believable band
            pull = (start_price - price) / start_price * 0.002
            price = max(price * (1.0 + daily_drift + shock + pull), floor)
            rows.append(
                {
                    "commodity": commodity,
                    "date": day.isoformat(),
                    "close": round(price, 1),
                    "unit": "USD/t",
                    "currency": "USD",
                    "source": "synthetic demo series",
                }
            )
    return rows


def write_prices(rows: list[dict[str, object]]) -> Path:
    path = FIXTURES / "prices.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["commodity", "date", "close", "unit", "currency", "source"]
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------
# news
# --------------------------------------------------------------------------
NEWS_SPECS: list[dict[str, object]] = [
    {
        "days_ago": 0,
        "slug": "plp-resource-upgrade",
        "title": "Mt Pilbara Lithium lifts total resource to 99.3 Mt at 1.03% Li2O",
        "summary": (
            "Pilbara Lithium Ltd reported an updated mineral resource estimate for its "
            "flagship Mt Pilbara Lithium Project, with 72.6 Mt now classified Measured "
            "and Indicated. The company said drilling along the eastern corridor "
            "extended mineralisation 600 m beyond the previous pit shell."
        ),
        "source": "ASX Announcements Wire",
        "tags": ["resource-upgrade", "drilling", "lithium", "pilbara"],
        "projects": ["Mt Pilbara Lithium Project"],
        "tickers": ["PLP.AX"],
    },
    {
        "days_ago": 0,
        "slug": "carbonate-price-firm",
        "title": "Battery-grade lithium carbonate holds above US$10,000/t on restocking",
        "summary": (
            "Asian spot assessments for battery-grade lithium carbonate were unchanged "
            "week-on-week as cathode makers resumed restocking ahead of the fourth "
            "quarter. Traders flagged thin liquidity and widening spreads between "
            "prompt and forward cargoes."
        ),
        "source": "Lithium Markets Weekly",
        "tags": ["pricing", "lithium", "carbonate", "market"],
        "projects": [],
        "tickers": [],
    },
    {
        "days_ago": 1,
        "slug": "yrl-offtake",
        "title": "Yarrie Ridge signs binding offtake for 120,000 t/y of spodumene concentrate",
        "summary": (
            "Westralian Resources Ltd executed a five-year binding offtake covering "
            "120,000 dry metric tonnes per year of 6% Li2O spodumene concentrate from "
            "the Yarrie Ridge Lithium Project, priced against a published concentrate "
            "index with a floor and ceiling mechanism."
        ),
        "source": "Pilbara Mining Journal",
        "tags": ["offtake", "spodumene", "of-take", "contract"],
        "projects": ["Yarrie Ridge Lithium Project"],
        "tickers": ["WRL.AX"],
    },
    {
        "days_ago": 1,
        "slug": "pilbara-port-queue",
        "title": "Port Hedland queue lengthens as lithium concentrate shipments bunch",
        "summary": (
            "Vessel waiting times at Port Hedland extended to four days as a cluster of "
            "concentrate cargoes arrived together. Operators said the congestion was "
            "scheduling-related rather than a capacity constraint."
        ),
        "source": "Resource Sector Monitor",
        "tags": ["logistics", "port", "shipping", "pilbara"],
        "projects": [],
        "tickers": [],
    },
    {
        "days_ago": 2,
        "slug": "cgl-drilling-assay",
        "title": "Coongan Gap returns 41 m at 1.34% Li2O in first diamond hole",
        "summary": (
            "Hardey Minerals Ltd reported assays from the first two diamond holes at the "
            "Coongan Gap Lithium Project, including 41 m at 1.34% Li2O from 96 m. The "
            "company cautioned that the intercepts are too widely spaced to support a "
            "resource classification upgrade."
        ),
        "source": "Resource Sector Monitor",
        "tags": ["exploration", "drilling", "assays", "lithium"],
        "projects": ["Coongan Gap Lithium Project"],
        "tickers": ["HDY.AX"],
    },
    {
        "days_ago": 3,
        "slug": "plp-approval",
        "title": "Mt Pilbara expansion clears state environmental approval with conditions",
        "summary": (
            "Western Australian regulators granted approval for the Mt Pilbara "
            "expansion, attaching conditions covering groundwater drawdown monitoring, "
            "dust suppression and a revised closure plan. The approval triggers a "
            "four-week appeals window."
        ),
        "source": "ASX Announcements Wire",
        "tags": ["permitting", "approval", "environment", "regulation", "pilbara"],
        "projects": ["Mt Pilbara Lithium Project"],
        "tickers": ["PLP.AX"],
    },
    {
        "days_ago": 3,
        "slug": "lithium-inventory",
        "title": "Chinese converter inventories fall for a third straight week",
        "summary": (
            "Inventories of lithium carbonate at major Chinese converters declined for a "
            "third consecutive week, according to survey data, although stocks remain "
            "well above the five-year seasonal average."
        ),
        "source": "Asia Battery Materials",
        "tags": ["inventory", "lithium", "china", "market"],
        "projects": [],
        "tickers": [],
    },
    {
        "days_ago": 4,
        "slug": "wrl-financing",
        "title": "Westralian Resources raises A$180m placement to fund Yarrie Ridge",
        "summary": (
            "Westralian Resources Ltd completed a A$180 million institutional placement "
            "at A$1.42 per share to fund construction at Yarrie Ridge. The raise was "
            "upsized from A$150 million on strong demand."
        ),
        "source": "ASX Announcements Wire",
        "tags": ["financing", "placement", "capital-raise"],
        "projects": ["Yarrie Ridge Lithium Project"],
        "tickers": ["WRL.AX"],
    },
    {
        "days_ago": 5,
        "slug": "spodumene-index",
        "title": "Spodumene concentrate index edges up on tight prompt supply",
        "summary": (
            "The 6% Li2O spodumene concentrate index rose modestly week-on-week, with "
            "buyers reporting limited prompt availability from Australian producers as "
            "several operations entered planned maintenance."
        ),
        "source": "Lithium Markets Weekly",
        "tags": ["pricing", "spodumene", "index", "market"],
        "projects": [],
        "tickers": [],
    },
    {
        "days_ago": 6,
        "slug": "hdy-heritage-survey",
        "title": "Hardey Minerals completes heritage survey over Coongan Gap access road",
        "summary": (
            "Hardey Minerals Ltd said a heritage survey over the proposed Coongan Gap "
            "access corridor has been completed, clearing the way for civil works to "
            "start next quarter subject to final approvals."
        ),
        "source": "Pilbara Mining Journal",
        "tags": ["heritage", "approvals", "community", "permitting"],
        "projects": ["Coongan Gap Lithium Project"],
        "tickers": ["HDY.AX"],
    },
    {
        "days_ago": 7,
        "slug": "plp-guidance",
        "title": "Pilbara Lithium trims FY27 shipment guidance after plant downtime",
        "summary": (
            "Pilbara Lithium Ltd narrowed FY27 shipment guidance to 620,000-660,000 dry "
            "metric tonnes from 680,000-720,000 previously, citing an unplanned 11-day "
            "outage at the primary crushing circuit in September."
        ),
        "source": "ASX Announcements Wire",
        "tags": ["guidance", "production", "downtime", "risk"],
        "projects": ["Mt Pilbara Lithium Project"],
        "tickers": ["PLP.AX"],
    },
    {
        "days_ago": 8,
        "slug": "royalty-review",
        "title": "State government opens consultation on mineral royalty settings",
        "summary": (
            "The state government opened a six-week consultation on the mineral royalty "
            "framework, floating a tiered structure for battery minerals. Industry "
            "groups warned that increased royalty rates would defer marginal projects."
        ),
        "source": "Resource Sector Monitor",
        "tags": ["regulation", "royalty", "policy", "risk"],
        "projects": [],
        "tickers": [],
    },
    {
        "days_ago": 9,
        "slug": "yrl-drill-results",
        "title": "Yarrie Ridge infill drilling supports conversion to Indicated",
        "summary": (
            "Westralian Resources reported infill results from Yarrie Ridge that it says "
            "support conversion of a substantial portion of the current Inferred "
            "tonnage into the Indicated category in the next estimate."
        ),
        "source": "Pilbara Mining Journal",
        "tags": ["drilling", "resource-upgrade", "infill"],
        "projects": ["Yarrie Ridge Lithium Project"],
        "tickers": ["WRL.AX"],
    },
    {
        "days_ago": 11,
        "slug": "ev-demand-outlook",
        "title": "Analysts cut 2027 EV sales forecasts, keep lithium demand intact",
        "summary": (
            "Several brokerages lowered 2027 electric vehicle sales forecasts while "
            "leaving lithium demand estimates broadly unchanged, arguing that average "
            "battery pack sizes are rising faster than unit volumes are falling."
        ),
        "source": "Asia Battery Materials",
        "tags": ["demand", "forecast", "ev", "market"],
        "projects": [],
        "tickers": [],
    },
    {
        "days_ago": 13,
        "slug": "mna-speculation",
        "title": "Speculation builds over consolidation among mid-cap lithium developers",
        "summary": (
            "Market participants pointed to consolidation among mid-cap lithium "
            "developers as balance sheets tighten, with two Pilbara-focused names "
            "singled out as potential targets. No approach has been confirmed."
        ),
        "source": "Resource Sector Monitor",
        "tags": ["m&a", "consolidation", "speculation"],
        "projects": ["Mt Pilbara Lithium Project", "Coongan Gap Lithium Project"],
        "tickers": ["PLP.AX", "HDY.AX"],
    },
    {
        "days_ago": 16,
        "slug": "power-supply",
        "title": "Grid connection study flags transmission constraint for new mines",
        "summary": (
            "A regional grid connection study identified transmission constraints that "
            "could delay power supply for new mining loads in the eastern Pilbara, "
            "prompting operators to evaluate on-site gas and battery storage."
        ),
        "source": "Pilbara Mining Journal",
        "tags": ["infrastructure", "power", "risk", "pilbara"],
        "projects": [],
        "tickers": [],
    },
    {
        "days_ago": 19,
        "slug": "spodumene-contract-reset",
        "title": "Producers reset spodumene contract terms toward spot-linked pricing",
        "summary": (
            "A growing share of spodumene offtake is being renegotiated toward "
            "spot-linked pricing with shorter tenors, reducing producers' revenue "
            "visibility compared with legacy fixed-price contracts."
        ),
        "source": "Lithium Markets Weekly",
        "tags": ["offtake", "pricing", "contract", "spodumene"],
        "projects": [],
        "tickers": [],
    },
    {
        "days_ago": 23,
        "slug": "cgl-tenement",
        "title": "Coongan Gap tenement expanded by 84 km2 along strike",
        "summary": (
            "Hardey Minerals Ltd was granted additional tenure adjoining Coongan Gap, "
            "expanding the project area by 84 km2 over a strike extension the company "
            "considers prospective for pegmatite-hosted mineralisation."
        ),
        "source": "ASX Announcements Wire",
        "tags": ["tenement", "exploration", "lithium"],
        "projects": ["Coongan Gap Lithium Project"],
        "tickers": ["HDY.AX"],
    },
    {
        "days_ago": 28,
        "slug": "water-licence",
        "title": "Water licence allocation for Mt Pilbara under review",
        "summary": (
            "The water regulator confirmed it is reviewing the allocation associated "
            "with the Mt Pilbara expansion, a process the company said is expected to "
            "conclude before the end of the year."
        ),
        "source": "Resource Sector Monitor",
        "tags": ["water", "regulation", "permitting", "risk"],
        "projects": ["Mt Pilbara Lithium Project"],
        "tickers": ["PLP.AX"],
    },
]


def generate_news(as_of: date) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for spec in NEWS_SPECS:
        days_ago = int(spec["days_ago"])  # type: ignore[arg-type]
        published = as_of - timedelta(days=days_ago)
        url = f"https://pilbara-mining-journal.example/{published.isoformat()}/{spec['slug']}"
        summary = str(spec["summary"])
        items.append(
            {
                "id": str(spec["slug"]),
                "title": spec["title"],
                "summary": summary,
                "url": url,
                "source": spec["source"],
                "published_at": f"{published.isoformat()}T0{7 + days_ago % 3}:{days_ago * 7 % 60:02d}:00+08:00",
                "tags": spec["tags"],
                "projects": spec["projects"],
                "tickers": spec["tickers"],
                "body": (
                    f"{summary} "
                    f"This article is part of the synthetic demo corpus shipped with "
                    f"mining-daily-agent and does not describe a real event. "
                    f"Reported by the {spec['source']} desk. "
                    f"Related coverage: {', '.join(spec['tags'])}."  # type: ignore[arg-type]
                ),
            }
        )
    return items


def write_news(items: list[dict[str, object]]) -> Path:
    path = FIXTURES / "news_corpus.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return path


# --------------------------------------------------------------------------
# NI 43-101 PDF
# --------------------------------------------------------------------------
RESOURCE_BOOK = [
    {
        "project": "Mt Pilbara Lithium Project",
        "operator": "Pilbara Lithium Ltd (ASX: PLP)",
        "commodity": "Lithium (spodumene, Li2O)",
        "cut_off": "0.40% Li2O",
        "effective": "30 September 2026",
        "rows": [
            ("Measured", 18.4, 1.16),
            ("Indicated", 54.2, 1.03),
            ("Measured + Indicated", 72.6, 1.06),
            ("Inferred", 26.7, 0.91),
            ("Total", 99.3, 1.03),
        ],
    },
    {
        "project": "Yarrie Ridge Lithium Project",
        "operator": "Westralian Resources Ltd (ASX: WRL)",
        "commodity": "Lithium (spodumene, Li2O)",
        "cut_off": "0.45% Li2O",
        "effective": "30 September 2026",
        "rows": [
            ("Indicated", 24.6, 1.19),
            ("Inferred", 15.3, 1.04),
            ("Total", 39.9, 1.13),
        ],
    },
    {
        "project": "Coongan Gap Lithium Project",
        "operator": "Hardey Minerals Ltd (ASX: HDY)",
        "commodity": "Lithium (spodumene, Li2O)",
        "cut_off": "0.50% Li2O",
        "effective": "30 September 2026",
        "rows": [
            ("Inferred", 11.2, 0.86),
            ("Total", 11.2, 0.86),
        ],
    },
]


def _lce_kt(tonnes_mt: float, grade_pct: float) -> float:
    return tonnes_mt * 1_000_000 * (grade_pct / 100.0) * 2.4728 / 1000.0


def write_pdf(as_of: date) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    path = FIXTURES / "ni43-101_pilbara_lithium.pdf"
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, spaceBefore=8, spaceAfter=6)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9.5, leading=13)
    small = ParagraphStyle("small", parent=body, fontSize=8, textColor=colors.HexColor("#555555"))

    doc = SimpleDocTemplate(
        str(path), pagesize=A4, title="NI 43-101 Technical Report (synthetic demo)",
        author="mining-daily-agent fixtures", leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )
    story: list[object] = [
        Paragraph("NI 43-101 Technical Report", h1),
        Paragraph("Mineral Resource Estimate — Pilbara Lithium Portfolio", h2),
        Paragraph(
            "This document is <b>synthetic demonstration data</b> generated by "
            "<font face='Courier'>scripts/make_fixtures.py</font> for the mining-daily-agent "
            "reference implementation. It is formatted like a National Instrument 43-101 "
            "technical report so that the PDF extraction tooling can be exercised "
            "end-to-end, but the projects, operators, tonnages and grades are fabricated "
            "and describe no real mineral property.",
            body,
        ),
        Spacer(1, 6),
        Paragraph(f"Report date: {as_of.isoformat()} &nbsp;|&nbsp; Effective date of estimate: 30 September 2026", body),
        Spacer(1, 4),
        Paragraph(
            "Qualified Person: J. Sample, P.Geo. (fictional). Cut-off grades are stated "
            "per project. Tonnages are dry metric tonnes. Li2CO3 equivalent is derived "
            "from the Li2O grade using the stoichiometric factor 2.4728.",
            small,
        ),
        PageBreak(),
    ]

    for index, book in enumerate(RESOURCE_BOOK):
        if index:
            story.append(PageBreak())
        story.append(Paragraph(f"{index + 1}. {book['project']}", h1))
        story.append(Paragraph(f"Operator: {book['operator']}", body))
        story.append(Paragraph(f"Commodity: {book['commodity']}", body))
        story.append(Paragraph(f"Cut-off grade: {book['cut_off']}", body))
        story.append(Paragraph(f"Effective date: {book['effective']}", body))
        story.append(Spacer(1, 8))
        story.append(Paragraph("Mineral Resource Statement", h2))

        data = [["Classification", "Tonnes (Mt)", "Grade (% Li2O)", "Contained Li2O (t)", "Contained LCE (kt)"]]
        for label, tonnes, grade in book["rows"]:
            data.append(
                [
                    label,
                    f"{tonnes:.1f}",
                    f"{grade:.2f}",
                    f"{tonnes * 1_000_000 * grade / 100:,.0f}",
                    f"{_lce_kt(tonnes, grade):,.1f}",
                ]
            )

        table = Table(data, colWidths=[46 * mm, 26 * mm, 30 * mm, 36 * mm, 32 * mm], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#333333")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 8))
        story.append(
            Paragraph(
                "Notes: Mineral resources are not mineral reserves and do not have "
                "demonstrated economic viability. Figures are rounded and may not sum "
                "exactly. The estimate is reported above the stated cut-off grade within "
                "a constraining pit shell.",
                small,
            )
        )

    doc.build(story)
    return path


# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic fixtures")
    parser.add_argument("--as-of", default=DEFAULT_AS_OF.isoformat(), help="fixture anchor date")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of)

    FIXTURES.mkdir(parents=True, exist_ok=True)

    prices = generate_prices()
    prices_path = write_prices(prices)

    news = generate_news(as_of)
    news_path = write_news(news)

    pdf_path = write_pdf(as_of)

    manifest = {
        "fixture_as_of": as_of.isoformat(),
        "generated_by": "scripts/make_fixtures.py",
        "synthetic": True,
        "disclaimer": DISCLAIMER,
        "files": {
            "prices.csv": {"rows": len(prices), "commodities": sorted(PRICE_SPECS)},
            "news_corpus.jsonl": {"rows": len(news)},
            "ni43-101_pilbara_lithium.pdf": {
                "projects": [b["project"] for b in RESOURCE_BOOK],
                "bytes": pdf_path.stat().st_size,
            },
        },
    }
    (FIXTURES / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"prices      -> {prices_path} ({len(prices)} rows)")
    print(f"news        -> {news_path} ({len(news)} rows)")
    print(f"ni43-101    -> {pdf_path} ({pdf_path.stat().st_size} bytes)")
    print(f"manifest    -> {FIXTURES / 'manifest.json'} (as of {as_of.isoformat()})")


if __name__ == "__main__":
    main()
