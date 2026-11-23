"""Deterministic matcher from a text note to already-registered AUU catalog ids.

This is not an LLM, not a strategy compiler, and not a port of any third-party
source module. It only suggests strategy ids that are already registered in the
AUU catalog. It never invents a strategy, never generates or compiles executable
code, never calls a model, never persists state, and never places or arms an
order.
"""
from __future__ import annotations

from typing import Any

from app.ai_section.body import MAX_BODY

MAX_PROMPT = 2000
# Same raw JSON body cap as app.ai_review.routes._json_body.

# Registered AUU catalog ids only. Order longer / more specific ids first
# so "cand_A" does not swallow "cand_A_ls".
KNOWN_IDS: tuple[str, ...] = (
    "cand_A_ls",
    "cand_B_ls",
    "cand_A",
    "cand_B",
    "trend_tsmom_v1",
    "shadow_s3",
    "shadow_h2",
    "auu_signal_v0",
)

# Independent keyword tables written against those ids (Latin + Chinese).
# Generic words such as "trend" or "funding" alone are intentionally omitted.
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cand_A_ls", ("cand_a_ls", "candidate a long short")),
    ("cand_B_ls", ("cand_b_ls", "candidate b long short")),
    ("cand_A", ("cand_a", "candidate a")),
    ("cand_B", ("cand_b", "candidate b")),
    (
        "trend_tsmom_v1",
        (
            "trend_tsmom_v1",
            "trend_tsmom",
            "tsmom",
            "time series momentum",
            "timeseries momentum",
            "时序动量",
            "时间序列动量",
        ),
    ),
    (
        "shadow_s3",
        (
            "shadow_s3",
            "funding extreme",
            "extreme funding",
            "极端资金费率",
            "资金费率极端",
        ),
    ),
    (
        "shadow_h2",
        (
            "shadow_h2",
            "trend carry",
            "carry idle",
            "趋势套息",
        ),
    ),
    (
        "auu_signal_v0",
        (
            "auu_signal_v0",
            "signal v0",
            "auu signal v0",
            "momentum volume funding",
            "动量成交量资金费",
            "信号v0",
        ),
    ),
)


def _latin_fold(text: str) -> str:
    return " ".join(text.lower().split())


def _flags() -> dict[str, Any]:
    return {
        "kind": "catalog_match",
        "engine": "deterministic_catalog_matcher",
        "used_model": False,
        "generates_code": False,
        "creates_strategy": False,
        "live": False,
        "orders": False,
    }


def match_catalog(prompt: str, known_ids: set[str] | None = None) -> dict[str, Any]:
    """Return catalog id matches for a prompt. Never calls a model or emits code."""
    if not isinstance(prompt, str):
        return {"ok": False, "code": "BAD_REQUEST", "message": "bad_body"}
    text = prompt.strip()
    if not text:
        return {"ok": False, "code": "BAD_REQUEST", "message": "bad_body"}
    if len(text) > MAX_PROMPT:
        return {"ok": False, "code": "BAD_REQUEST", "message": "bad_body"}

    allowed = set(known_ids) if known_ids is not None else set(KNOWN_IDS)
    folded = _latin_fold(text)
    lower = text.lower()
    hits: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(sid: str, keyword: str) -> None:
        if sid not in allowed or sid in seen:
            return
        seen.add(sid)
        hits.append({"id": sid, "keyword": keyword})

    for sid in KNOWN_IDS:
        if _latin_fold(sid) == folded:
            add(sid, sid)
            break

    for sid, keywords in RULES:
        for keyword in keywords:
            key = keyword.lower()
            if key.isascii() and all(ch.isalnum() or ch in "._-" for ch in key):
                if folded == key or f" {key} " in f" {folded} ":
                    add(sid, keyword)
                    break
            elif key in lower or key in folded:
                add(sid, keyword)
                break

    return {
        "ok": True,
        **_flags(),
        "prompt_len": len(text),
        "matches": hits,
        "unmatched": len(hits) == 0,
    }
