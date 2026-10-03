"""Code version of the running checkout (no subprocess: reads .git directly)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional


def _git_dir() -> Optional[Path]:
    for p in Path(__file__).resolve().parents:
        g = p / ".git"
        if g.is_dir():
            return g
        if g.is_file():  # worktree: "gitdir: <path>"
            txt = g.read_text().strip()
            if txt.startswith("gitdir:"):
                return Path(txt.split(":", 1)[1].strip())
    return None


@lru_cache(maxsize=1)
def git_commit() -> Optional[str]:
    """Full commit SHA of HEAD (``AUU_GIT_COMMIT`` overrides), or None."""
    env = (os.getenv("AUU_GIT_COMMIT") or "").strip()
    if env:
        return env
    g = _git_dir()
    if g is None:
        return None
    try:
        head = (g / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.split(":", 1)[1].strip()
        common = g
        if (g / "commondir").exists():
            common = (g / (g / "commondir").read_text().strip()).resolve()
        for base in (g, common):
            f = base / ref
            if f.exists():
                return f.read_text().strip() or None
        packed = common / "packed-refs"
        if packed.exists():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0]
    except OSError:
        return None
    return None


def short_commit() -> str:
    c = git_commit()
    return c[:7] if c else "unknown"
