from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = ROOT / "data" / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _offline_env():
    """Every test runs against the bundled fixtures, never the network."""
    os.environ.setdefault("MINING_AGENT_DATA_MODE", "offline")
    os.environ.setdefault("MINING_AGENT_LOG_LEVEL", "WARNING")
    os.environ.pop("ANTHROPIC_API_KEY", None)
    yield


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def sample_pdf(fixture_dir: Path) -> Path:
    pdf = fixture_dir / "ni43-101_pilbara_lithium.pdf"
    if not pdf.is_file():
        pytest.skip("run scripts/make_fixtures.py to generate the sample PDF")
    return pdf
