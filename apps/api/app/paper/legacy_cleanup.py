"""One-shot cleanup of pre-real-market paper data (dry-run by default).

Usage (from ``apps/api``)::

    python -m app.paper.legacy_cleanup                 # report only, writes nothing
    python -m app.paper.legacy_cleanup --apply         # rewrite, after a backup

What ``--apply`` changes:

* ``paper_journal.json``
  - drops open **short** lots of the long-only ``pump-paper-v1`` strategy
    (residuals of the old notional/price oversell bug);
  - labels every lot / closed row without ``market_source`` as
    ``legacy_synthetic``;
  - flags closed ``short`` rows of ``pump-paper-v1`` with ``phantom_short``.
* ``shadow_compare.json``: labels closed rows without ``market_source`` as
  ``legacy_synthetic``.

Rows are never deleted from ``closed``. Every rewritten file first gets a
byte-for-byte ``.bak-<timestamp>`` copy next to it. Stop the API before
``--apply``: a running process keeps the journal in memory and rewrites it.
Even without this script, rows without a label load as ``legacy_synthetic``
and stay out of real-market stats.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

from app.data_paths import data_dir, guarded_path

LEGACY = "legacy_synthetic"
STRATEGY = "pump-paper-v1"


def journal_path(explicit: Optional[str] = None) -> Path:
    raw = (explicit or os.getenv("PAPER_JOURNAL_STORE") or "").strip()
    return Path(raw).expanduser() if raw else data_dir() / "paper_journal.json"


def shadow_path(explicit: Optional[str] = None) -> Path:
    raw = (explicit or os.getenv("SHADOW_COMPARE_STORE") or "").strip()
    return Path(raw).expanduser() if raw else data_dir() / "shadow_compare.json"


def _is_strategy_row(row: dict[str, Any]) -> bool:
    blob = " ".join(
        str(row.get(k) or "") for k in ("tag", "strategy_id")
    ).lower() + " " + " ".join(str(t) for t in (row.get("tags") or [])).lower()
    return STRATEGY in blob or "autopaper" in blob


def clean_journal(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Return ``(cleaned_copy, counts)``. ``raw`` is not modified."""
    data = json.loads(json.dumps(raw))
    counts = {
        "open_lots": 0,
        "open_short_lots": 0,
        "phantom_short_lots_dropped": 0,
        "other_short_lots_kept": 0,
        "lots_labeled_legacy": 0,
        "closed_n": 0,
        "closed_short_trades": 0,
        "closed_phantom_flagged": 0,
        "closed_labeled_legacy": 0,
    }
    lots = data.get("lots") if isinstance(data.get("lots"), dict) else {}
    new_lots: dict[str, list[Any]] = {}
    for sym, rows in lots.items():
        if not isinstance(rows, list):
            continue
        keep: list[Any] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qty = float(row.get("qty") or 0.0)
            if abs(qty) <= 1e-12:
                continue
            counts["open_lots"] += 1
            if qty < 0:
                counts["open_short_lots"] += 1
                if _is_strategy_row(row) or not row.get("tag"):
                    counts["phantom_short_lots_dropped"] += 1
                    continue
                counts["other_short_lots_kept"] += 1
            if "market_source" not in row:
                row["market_source"] = LEGACY
                counts["lots_labeled_legacy"] += 1
            keep.append(row)
        if keep:
            new_lots[sym] = keep
    data["lots"] = new_lots
    closed = data.get("closed") if isinstance(data.get("closed"), list) else []
    for row in closed:
        if not isinstance(row, dict):
            continue
        counts["closed_n"] += 1
        if "market_source" not in row:
            row["market_source"] = LEGACY
            counts["closed_labeled_legacy"] += 1
        if str(row.get("side") or "") == "short":
            counts["closed_short_trades"] += 1
            if _is_strategy_row(row) and not row.get("phantom_short"):
                row["phantom_short"] = True
                counts["closed_phantom_flagged"] += 1
    return data, counts


def clean_shadow(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    data = json.loads(json.dumps(raw))
    counts = {"shadow_closed_n": 0, "shadow_closed_labeled_legacy": 0}
    closed = data.get("closed") if isinstance(data.get("closed"), list) else []
    for row in closed:
        if not isinstance(row, dict):
            continue
        counts["shadow_closed_n"] += 1
        if "market_source" not in row:
            row["market_source"] = LEGACY
            counts["shadow_closed_labeled_legacy"] += 1
    return data, counts


def _read(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: root must be an object")
    return raw


def _write_with_backup(path: Path, payload: dict[str, Any], stamp: str) -> Path:
    target = guarded_path(path)
    backup = guarded_path(target.with_name(f"{target.name}.bak-{stamp}"))
    shutil.copy2(target, backup)
    tmp = guarded_path(target.with_suffix(target.suffix + ".cleanup.tmp"))
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)
    return backup


def run(journal: Path, shadow: Path, *, apply: bool, out=None) -> dict[str, Any]:
    out = out if out is not None else sys.stdout
    report: dict[str, Any] = {"mode": "apply" if apply else "dry-run", "backups": []}
    stamp = time.strftime("%Y%m%d%H%M%S")
    j_raw = _read(journal)
    s_raw = _read(shadow)
    j_clean = s_clean = None
    if j_raw is not None:
        j_clean, counts = clean_journal(j_raw)
        report.update(counts)
    if s_raw is not None:
        s_clean, counts = clean_shadow(s_raw)
        report.update(counts)
    print(f"journal={journal}{'' if j_raw is not None else ' (missing)'}", file=out)
    print(f"shadow={shadow}{'' if s_raw is not None else ' (missing)'}", file=out)
    print(f"mode={report['mode']}", file=out)
    for key, value in report.items():
        if key not in {"mode", "backups"}:
            print(f"{key}={value}", file=out)
    if not apply:
        print("dry-run: nothing written. Re-run with --apply (API stopped) to rewrite.", file=out)
        return report
    if j_raw is not None and j_clean != j_raw:
        report["backups"].append(str(_write_with_backup(journal, j_clean, stamp)))
    if s_raw is not None and s_clean != s_raw:
        report["backups"].append(str(_write_with_backup(shadow, s_clean, stamp)))
    for b in report["backups"]:
        print(f"backup={b}", file=out)
    if not report["backups"]:
        print("nothing to change", file=out)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--journal", default="", help="paper_journal.json (default PAPER_JOURNAL_STORE or data dir)")
    parser.add_argument("--shadow", default="", help="shadow_compare.json (default SHADOW_COMPARE_STORE or data dir)")
    parser.add_argument("--apply", action="store_true", help="Rewrite the files (backup first). Default is dry-run.")
    args = parser.parse_args(argv)
    run(journal_path(args.journal or None), shadow_path(args.shadow or None), apply=bool(args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
