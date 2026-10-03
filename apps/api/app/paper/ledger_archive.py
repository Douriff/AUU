"""Archive / restore the legacy pump.fun paper ledger (dry-run by default).

    python -m app.paper.ledger_archive archive [--apply]
    python -m app.paper.ledger_archive restore FILE [--apply] [--force]
    python -m app.paper.ledger_archive list

``archive`` packs the pump-era paper data under ``data_dir()`` into
``data_dir()/archive/pump-ledger-<UTC ts>.tar.gz`` (with ``MANIFEST.json``:
size + sha256 per file), re-reads the archive to verify every checksum, and
only then removes the originals, so the API starts on a fresh, empty ledger.
Stop the API first: a running process keeps the ledger in memory and would
write it back.

Archived: paper_journal*.json(.tmp), shadow_compare*.json(.tmp),
trader_watchlist.json, evidence/ (paper evidence + launch tapes).
Never touched: users.json, user journals, mainstream.sqlite*, archive/.

``restore`` extracts an archive back into ``data_dir()`` after verifying the
manifest; it refuses to overwrite existing files unless ``--force``.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import time
from pathlib import Path
from typing import Optional

from app.data_paths import data_dir

PATTERNS = ("paper_journal*.json", "paper_journal*.json.tmp", "shadow_compare*.json", "shadow_compare*.json.tmp", "trader_watchlist.json")
DIRS = ("evidence",)
MANIFEST = "MANIFEST.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_dir(root: Optional[Path] = None) -> Path:
    return (root or data_dir()) / "archive"


def collect(root: Optional[Path] = None) -> list[Path]:
    """Relative paths of legacy ledger files present under ``root``."""
    base = root or data_dir()
    found: set[Path] = set()
    for pat in PATTERNS:
        found.update(p for p in base.glob(pat) if p.is_file())
    for d in DIRS:
        sub = base / d
        if sub.is_dir():
            found.update(p for p in sub.rglob("*") if p.is_file())
    return sorted(p.relative_to(base) for p in found)


def archive(*, apply: bool = False, root: Optional[Path] = None, now: Optional[float] = None) -> dict:
    base = root or data_dir()
    files = collect(base)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    dest = archive_dir(base) / f"pump-ledger-{stamp}.tar.gz"
    entries = [{"path": str(rel), "size": (base / rel).stat().st_size, "sha256": _sha256(base / rel)} for rel in files]
    out = {"apply": apply, "archive": str(dest), "files": entries, "removed": []}
    if not apply or not files:
        return out
    dest.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.dumps(
        {"kind": "auu-pump-paper-ledger", "created": stamp, "dataDir": ".", "files": entries}, indent=1
    ).encode()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with tarfile.open(tmp, "w:gz") as tar:
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(manifest)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(manifest))
        for rel in files:
            tar.add(base / rel, arcname=str(rel), recursive=False)
    verify(tmp)  # raises on any mismatch; originals untouched
    tmp.rename(dest)
    for rel in files:
        (base / rel).unlink()
        out["removed"].append(str(rel))
    return out


def verify(path: Path) -> dict:
    with tarfile.open(path, "r:gz") as tar:
        manifest = json.loads(tar.extractfile(MANIFEST).read())  # type: ignore[union-attr]
        for entry in manifest["files"]:
            fh = tar.extractfile(entry["path"])
            if fh is None:
                raise ValueError(f"missing in archive: {entry['path']}")
            h = hashlib.sha256()
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
            if h.hexdigest() != entry["sha256"]:
                raise ValueError(f"checksum mismatch: {entry['path']}")
    return manifest


def restore(path: Path, *, apply: bool = False, force: bool = False, root: Optional[Path] = None) -> dict:
    base = root or data_dir()
    manifest = verify(path)
    targets = [base / e["path"] for e in manifest["files"]]
    clash = [str(t.relative_to(base)) for t in targets if t.exists()]
    out = {"apply": apply, "archive": str(path), "files": [e["path"] for e in manifest["files"]], "existing": clash}
    if clash and not force:
        out["error"] = "target files exist (stop the API; use --force to overwrite)"
        return out
    if not apply:
        return out
    with tarfile.open(path, "r:gz") as tar:
        for entry in manifest["files"]:
            rel = Path(entry["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe path in archive: {rel}")
            dst = base / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(entry["path"])
            assert src is not None
            tmp = dst.with_name(dst.name + ".restore")
            with tmp.open("wb") as fh:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    fh.write(chunk)
            if _sha256(tmp) != entry["sha256"]:
                tmp.unlink()
                raise ValueError(f"checksum mismatch after restore: {rel}")
            tmp.replace(dst)
    out["restored"] = True
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.paper.ledger_archive", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("archive")
    a.add_argument("--apply", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--apply", action="store_true")
    r.add_argument("--force", action="store_true")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    if args.cmd == "archive":
        res = archive(apply=args.apply)
        print(json.dumps({**res, "files": len(res["files"]), "bytes": sum(f["size"] for f in res["files"])}, indent=1))
        return 0
    if args.cmd == "restore":
        res = restore(Path(args.file), apply=args.apply, force=args.force)
        print(json.dumps(res, indent=1))
        return 1 if res.get("error") else 0
    for p in sorted(archive_dir().glob("pump-ledger-*.tar.gz")):
        m = verify(p)
        print(f"{p}  files={len(m['files'])}  created={m['created']}  verified=ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
