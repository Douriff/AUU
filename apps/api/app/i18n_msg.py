"""Language-neutral server messages: a key plus params that the web UI renders per language.

A message node is one of
  {"k": "srv.ev.restart", "p": {...}}   catalog key (apps/web/src/i18n/locales/*.json) + params
  {"s": "raw text"}                       untranslated text (tickers, ids, exception names, numbers)
  {"j": [node, ...], "sep": " "|node, "trim": bool}   nodes joined by a separator
Param values are plain strings / numbers or nested nodes. Every place that returns a node keeps
returning the old text field too (older clients, emails, logs); the zh-CN catalog renders each
node back to exactly that text (tests/test_i18n_msg.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

Node = dict[str, Any]


def m(key: str, **params: Any) -> Node:
    return {"k": key, "p": params} if params else {"k": key}


def raw(text: Any) -> Node:
    return {"s": "" if text is None else str(text)}


def join(parts: list[Node], sep: Any = " ", trim: bool = False) -> Node:
    out: Node = {"j": parts, "sep": sep}
    if trim:
        out["trim"] = True
    return out


def go_msg(g: dict[str, Any]) -> Node:
    """Same text as StrategyRunner.go_no_go()["message"], as a node (web: i18n/strategy.ts goMessage)."""
    verdict = g.get("verdict")
    if verdict == "pending":
        return m("go.pending", n=g.get("days", 0), min=g.get("minDays", 0))
    if verdict == "go":
        return m("go.pass")
    reasons = g.get("reasons")
    if reasons is None:
        return raw(g.get("message") or "")
    return m("go.fail", why=join([m(f"go.reason.{r}") for r in reasons], sep=m("go.sep")))


# ---- risk lock / data breaker reasons (parsed from the stored text: storage stays unchanged) -------------
_DD = re.compile(r"^drawdown <= (-?\d+)%$")
_DAY = re.compile(r"^day loss <= (-?\d+)%$")
_STALE = re.compile(r"^stale 1h: ([^ ,]+(?:,[^ ,]+)*)(?:, (.*))?$", re.S)
_REFRESH = re.compile(r"^no successful market refresh \((\d+) min( since start)?\): (.*)$", re.S)
_REFRESH_NEVER = re.compile(r"^no successful market refresh \(never in this process\): (.*)$", re.S)


def lock_reason_msg(reason: Any) -> Node:
    text = "" if reason is None else str(reason)
    hit = _DD.match(text)
    if hit:
        return m("srv.risk.drawdown", pct=hit.group(1))
    hit = _DAY.match(text)
    if hit:
        return m("srv.risk.dayLoss", pct=hit.group(1))
    return raw(text)


def _exchange_reason(text: str) -> Node:
    if text == "no exchange":
        return m("srv.risk.noExchange")
    if text == "exchange health failed":
        return m("srv.risk.healthFailed")
    hit = _REFRESH.match(text)
    if hit:
        key = "srv.risk.noRefreshStart" if hit.group(2) else "srv.risk.noRefresh"
        return m(key, min=hit.group(1), err=hit.group(3))
    hit = _REFRESH_NEVER.match(text)
    if hit:
        return m("srv.risk.noRefreshNever", err=hit.group(1))
    return raw(text)


def data_bad_msg(reason: Any) -> Node:
    """'stale 1h: BTC,ETH' / exchange reason / both joined by ', ' (strategy_runner._mark)."""
    text = "" if reason is None else str(reason)
    hit = _STALE.match(text)
    if hit:
        parts = [m("srv.risk.stale", coins=hit.group(1))]
        if hit.group(2) is not None:
            parts.append(_exchange_reason(hit.group(2)))
        return join(parts, sep=", ")
    return _exchange_reason(text)


# ---- rendering (tests, and anything server-side that wants another language) -------------------------------
_CATALOGS: dict[str, dict[str, Any]] = {}
_LOCALES = Path(__file__).resolve().parents[2] / "web" / "src" / "i18n" / "locales"


def catalog(lang: str = "zh-CN", path: Optional[Path] = None) -> dict[str, Any]:
    if lang not in _CATALOGS:
        _CATALOGS[lang] = json.loads(((path or _LOCALES) / f"{lang}.json").read_text(encoding="utf-8"))
    return _CATALOGS[lang]


def _lookup(cat: dict[str, Any], key: str) -> Optional[str]:
    cur: Any = cat
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur if isinstance(cur, str) else None


def render(node: Any, cat: Optional[dict[str, Any]] = None) -> str:
    """i18next-compatible {{name}} interpolation of a node; raises KeyError on an unknown key."""
    cat = catalog() if cat is None else cat
    if isinstance(node, (str, int, float)):
        return str(node)
    if not isinstance(node, dict):
        return ""
    if "s" in node:
        return str(node["s"])
    if "j" in node:
        sep = node.get("sep", " ")
        out = (sep if isinstance(sep, str) else render(sep, cat)).join(render(x, cat) for x in node["j"])
        return out.strip() if node.get("trim") else out
    tpl = _lookup(cat, node["k"])
    if tpl is None:
        raise KeyError(node["k"])
    params = {k: render(v, cat) for k, v in (node.get("p") or {}).items()}
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda x: params.get(x.group(1), x.group(0)), tpl)
