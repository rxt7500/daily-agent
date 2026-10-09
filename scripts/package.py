"""Package the deliverable as a zip, excluding anything generated or machine-local.

    python scripts/package.py [--out dist/mining-daily-agent.zip] [--with-output]
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "build", "dist", "output", ".idea", ".vscode",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip", ".tar.gz", ".log"}
EXCLUDE_NAMES = {".env", "mcp-config.local.json"}

# Fixture regenerability check: the PDF is small and required for the demo.
REQUIRED = [
    "RUN.md",
    "README.md",
    "requirements.txt",
    "pyproject.toml",
    "mcp-config.json",
    "mcp-config.docker.json",
    "docker-compose.yml",
    "Dockerfile",
    "data/fixtures/manifest.json",
    "data/fixtures/news_corpus.jsonl",
    "data/fixtures/prices.csv",
    "data/fixtures/ni43-101_pilbara_lithium.pdf",
]


def included(path: Path, with_output: bool) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in relative.parts[:-1]):
        return False
    if relative.parts[0] == "output" and not with_output:
        return False
    if path.suffix in EXCLUDE_SUFFIXES or path.name in EXCLUDE_NAMES:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT.parent / "mining-daily-agent.zip")
    parser.add_argument("--with-output", action="store_true", help="include generated briefings")
    args = parser.parse_args()

    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    if missing:
        print("missing required file(s):")
        for name in missing:
            print(f"  - {name}")
        return 1

    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and included(p, args.with_output))
    args.out.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, arcname=str(Path(ROOT.name) / path.relative_to(ROOT)))

    size_kb = args.out.stat().st_size / 1024
    print(f"packaged {len(files)} files -> {args.out} ({size_kb:,.1f} KB)")
    for path in files:
        print(f"  {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
