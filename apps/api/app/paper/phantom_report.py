"""One-shot report of residual short lots and unlabeled journal rows.

Default is dry-run: print counts and exit. This module never writes the journal,
including when ``--apply`` is passed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.data_paths import data_dir, guarded_path


def journal_path(explicit: str | None = None) -> Path:
    raw = (explicit or os.getenv("PAPER_JOURNAL_STORE") or "").strip()
    if raw:
        return guarded_path(Path(raw).expanduser())
    return guarded_path(data_dir() / "paper_journal.json")


def inspect_payload(raw: dict[str, Any]) -> dict[str, int]:
    lots = raw.get("lots") if isinstance(raw.get("lots"), dict) else {}
    closed = raw.get("closed") if isinstance(raw.get("closed"), list) else []
    open_shorts = 0
    open_lots = 0
    for rows in lots.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            qty = float(row.get("qty") or 0.0)
            if abs(qty) <= 1e-12:
                continue
            open_lots += 1
            if qty < 0:
                open_shorts += 1
    closed_shorts = 0
    missing_source = 0
    for row in closed:
        if not isinstance(row, dict):
            continue
        if str(row.get("side") or "") == "short":
            closed_shorts += 1
        if "market_source" not in row:
            missing_source += 1
    return {
        "open_lots": open_lots,
        "open_short_lots": open_shorts,
        "closed_short_trades": closed_shorts,
        "closed_missing_market_source": missing_source,
        "closed_n": sum(1 for row in closed if isinstance(row, dict)),
    }


def report_file(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {
            "open_lots": 0,
            "open_short_lots": 0,
            "closed_short_trades": 0,
            "closed_missing_market_source": 0,
            "closed_n": 0,
        }
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("journal root must be an object")
    return inspect_payload(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report residual paper shorts (dry-run, never writes).")
    parser.add_argument("--path", default="", help="Journal JSON path. Default PAPER_JOURNAL_STORE or data dir.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Refused. The script only reports and does not rewrite the journal.",
    )
    args = parser.parse_args(argv)
    path = journal_path(args.path or None)
    before = path.read_bytes() if path.is_file() else None
    counts = report_file(path)
    print(f"journal={path}")
    print("mode=dry-run")
    if args.apply:
        print("apply refused: this script never writes the journal")
    for key, value in counts.items():
        print(f"{key}={value}")
    print("missing market_source rows load as legacy_synthetic and stay out of Go")
    after = path.read_bytes() if path.is_file() else None
    if before != after:
        print("error: journal bytes changed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
