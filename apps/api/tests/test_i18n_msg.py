"""Server text as key + params (app.i18n_msg): zh-CN renders exactly the old text; every key exists in all 11 locales."""
from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import i18n_msg as im
from app.backtest import stats
from app.backtest.costs import CostModel
from app.paper import events as ev
from app.paper import shadow_h2 as h2
from tests.test_shadow_h2 import INC, _Base

LANGS = ["zh-CN", "en", "de", "fr", "es", "pt", "tr", "ru", "ja", "ko", "ar"]


def keys_of(node, out=None):
    out = set() if out is None else out
    if isinstance(node, dict):
        if "k" in node:
            out.add(node["k"])
        for v in (node.get("p") or {}).values():
            keys_of(v, out)
        for v in node.get("j") or []:
            keys_of(v, out)
        if isinstance(node.get("sep"), dict):
            keys_of(node["sep"], out)
    return out


class _Check(unittest.TestCase):
    seen: set = set()

    def same(self, node, text):
        """zh-CN renders the server text exactly; every other language renders without a missing key."""
        self.assertEqual(im.render(node), text)
        for lang in LANGS[1:]:
            out = im.render(node, im.catalog(lang))
            self.assertNotRegex(out, r"\{\{\w+\}\}", lang)
        _Check.seen |= keys_of(node)


class Trade(SimpleNamespace):
    def as_dict(self):
        return {"tags": self.tags, "exit_reason": self.exit_reason}


class ConsoleEventTests(_Check):
    def setUp(self):
        ev.reset_events()

    def tearDown(self):
        ev.reset_events()

    def rows(self):
        return list(ev._events)

    def check_all(self, min_rows=1):
        rows = self.rows()
        self.assertGreaterEqual(len(rows), min_rows)
        for r in rows:
            self.same(r["msg"], r["message"])
            self.same(r["pill_msg"], r["pill"])
        return rows

    def test_system_and_discovery_rows(self):
        ev.note_api_start()
        ev.note_autopaper(True)
        ev.note_autopaper(False)
        for active, reason in [("pumpportal", "ok"), ("pumpportal", "connecting"), ("", "connecting"), ("off", "off"),
                               ("pumpportal", "portal_auth_rejected"), ("x", "live"), ("idle", "")]:
            ev.note_discovery_status(active, reason)
        ev.note_discovery(mint="So11111111111111111111111111111111111111112", symbol="PEPE/SOL", ts=1, creator="CreatorAbcdef")
        ev.note_discovery(mint="Mint2", symbol="", ts=2)
        rows = self.check_all(min_rows=12)
        self.assertEqual(rows[0]["message"], "API 已重启")
        self.assertEqual(im.render(rows[0]["msg"], im.catalog("en")), "API restarted")

    def test_entries_and_exits(self):
        for tag in ("auto", "source=manual"):
            opened = SimpleNamespace(qty=1234.5678, ts=10 if tag == "auto" else 11, price=0.00001234, tag=tag, mint="M1")
            ev.note_journal_fill("WIF/SOL", SimpleNamespace(ts=10, tag=""), opened=opened)
        trades = []
        for i, (tags, why, pct) in enumerate([(["TAKE_PROFIT"], "", 0.0123), ([], "stop_loss", -0.02), ([], "max_hold", 0.0),
                                               (["SELL_PRESSURE"], "", 0.001), (["CURVE_NEAR_GRADUATION"], "", 0.5),
                                               (["ORPHAN_EXIT"], "", -0.1), ([], "weird", 0.0004), (["source=manual"], "", 0.03)]):
            trades.append(Trade(id=f"t{i}", tags=tags, exit_reason=why, pnl_pct=pct, pnl=pct * 2, exit_ts=100 + i, symbol="BONK/SOL", mint="M2"))
        ev.note_journal_fill("BONK/SOL", SimpleNamespace(ts=100, tag=""), closed=trades)
        rows = self.check_all(min_rows=10)
        self.assertEqual({r["pill"] for r in rows}, {"开仓", "手动", "TP", "SL", "timeout", "weak-tape", "graduation", "orphan", "平仓"})

    def test_rejects_and_shadow(self):
        base = dict(ts=5, symbol="DOG/SOL", outcome="reject", mint="M3", risk_tags=[], signal_reason="")
        for extra in [dict(reject_bucket="impact", impact_gross_bps=212.4, impact_bps_cap=150.0),
                      dict(reject_bucket="impact", impact_bps_est=99.0, signal_reason="thin_book"),
                      dict(reject_bucket="impact", estimated_impact_bps=40.0),
                      dict(reject_bucket="impact", signal_reason="no_quote"),
                      dict(reject_bucket="impact", symbol=""),
                      dict(reject_bucket="risk", signal_reason="max_positions"),
                      dict(reject_bucket="risk", risk_tags=["A", "B", "C", "D"]),
                      dict(reject_bucket="risk")]:
            row = SimpleNamespace(**{"impact_gross_bps": None, "impact_bps_est": None, "estimated_impact_bps": None,
                                     "impact_bps_cap": None, **base, **extra})
            row.ts = len(self.rows()) + 1
            ev.note_decision(row)
        ev.note_shadow_decision({"action": "enter", "ts": 7, "set_id": "S1", "symbol": "CAT/SOL", "reason": "score>0.7"})
        ev.note_shadow_decision({"action": "skip", "ts": 8, "set_id": "", "symbol": "CAT/SOL", "reason": ""})
        ev.note_shadow_close({"exit_ts": 9, "set_id": "S1", "symbol": "CAT/SOL", "exit_reason": "take_profit", "pnl": 0.1, "net_bps": 12})
        ev.note_shadow_close({"exit_ts": 10, "set_id": "", "symbol": "", "exit_reason": "zzz"})
        self.check_all(min_rows=12)


class GoAndNoteTests(_Check):
    def test_go_messages_and_console_nogo(self):
        cases = [
            {"verdict": "pending", "days": 12, "minDays": 250, "message": "数据积累中，未证明优势（日收益 12/250 天）"},
            {"verdict": "go", "message": "通过：日收益 CI 下限 > 0 且跑赢国债"},
            {"verdict": "no-go", "reasons": ["CI_LO", "TBILL"], "message": "未通过：日收益年化 bootstrap CI 下限 ≤ 0；未跑赢国债（超额 CI 下限 ≤ 0）"},
            {"verdict": "no-go", "reasons": ["TBILL"], "message": "未通过：未跑赢国债（超额 CI 下限 ≤ 0）"},
        ]
        for g in cases:
            self.same(im.go_msg(g), g["message"])
            self.same(im.m("srv.nogo.ledger", msg=im.go_msg(g)), "趋势策略纸面账本：" + g["message"])
        self.same(im.m("srv.nogo.readFailed", err="KeyError"), "策略账本读取失败：KeyError")

    def test_console_stats_carries_nogo_msg(self):
        out = ev.console_stats()
        self.same(out["nogo_msg"], out["nogo_reason"])

    def test_leaderboard_note(self):
        from app.auth import accounts

        for on in (False, True):
            with patch.object(accounts, "auth_enabled", lambda on=on: on), patch.object(accounts, "_load", lambda: []):
                board = accounts.leaderboard()
            self.same(board["noteMsg"], board["note"])


class RiskReasonTests(_Check):
    def test_lock_reasons_use_runner_format(self):
        for dd in (-0.2, -0.15, 0.1):
            text = "drawdown <= %.0f%%" % (dd * 100)
            self.same(im.lock_reason_msg(text), text)
            self.assertEqual(im.lock_reason_msg(text)["k"], "srv.risk.drawdown")
        text = "day loss <= %.0f%%" % (-0.08 * 100)
        self.same(im.lock_reason_msg(text), text)
        self.same(im.lock_reason_msg("something new"), "something new")
        self.same(im.lock_reason_msg(None), "")

    def test_data_breaker_reasons(self):
        refresh = "no successful market refresh (%s): %s"
        samples = [
            "stale 1h: BTC,ETH",
            "stale 1h: BTC, no exchange",
            "stale 1h: SOL, " + (refresh % ("17 min", "ReadTimeout: x, y"))[:200],
            "no exchange",
            "exchange health failed",
            refresh % ("12 min", ""),
            refresh % ("31 min since start", "ConnectError: refused"),
            refresh % ("never in this process", "boom"),
            "RuntimeError: odd, text",
            "",
        ]
        for text in samples:
            self.same(im.data_bad_msg(text), text)
        self.assertEqual(im.data_bad_msg("no exchange")["k"], "srv.risk.noExchange")

    def test_runner_risk_summary_adds_reason_msg(self):
        from app.paper.strategy_runner import _with_msg

        st = {"kind": "review", "since": 1, "reason": "drawdown <= -20%"}
        out = _with_msg(st, im.lock_reason_msg)
        self.assertEqual(out["reason"], st["reason"])
        self.assertNotIn("reasonMsg", st)  # stored state untouched
        self.same(out["reasonMsg"], st["reason"])
        self.assertIsNone(_with_msg(None, im.data_bad_msg))


class H2TextTests(_Base, _Check):
    def test_refused_reasons(self):
        bad = copy.deepcopy(h2.PARAMS)
        bad["trend"]["coin_cap"] = 0.2
        errs = []
        for fn in (lambda: h2.verify_frozen(bad), lambda: h2.verify_frozen(stored="0" * 64),
                   lambda: h2.verify_frozen(registry=self.root / "nope.json")):
            with self.assertRaises(h2.FrozenParamsError) as cm:
                fn()
            errs.append(cm.exception)
        reg = json.loads(h2.registry_path().read_text(encoding="utf-8"))
        reg["params"]["carry"]["leverage"] = 5
        p = self.root / "reg.json"
        p.write_text(json.dumps(reg), encoding="utf-8")
        with self.assertRaises(h2.FrozenParamsError) as cm:
            h2.verify_frozen(registry=p)
        errs.append(cm.exception)
        with patch.object(h2, "CostModel", lambda: CostModel(taker=0.0004)):
            with self.assertRaises(h2.FrozenParamsError) as cm:
                h2.verify_frozen()
            errs.append(cm.exception)
        with patch.object(stats, "TBILL", 0.05):
            with self.assertRaises(h2.FrozenParamsError) as cm:
                h2.verify_frozen()
            errs.append(cm.exception)
        self.assertEqual({e.msg["k"].rsplit(".", 1)[1] for e in errs},
                         {"paramsHash", "ledgerHash", "registryUnreadable", "registryMismatch", "costModel", "tbill"})
        for e in errs:
            self.same(e.msg, str(e))
        sh = self.make(self.led, params=bad)
        self.clock.t = INC + 86_400_000 + 20 * 60_000
        sh.tick()
        s = sh.summary()
        self.same(s["refusedMsg"], s["refused"])

    def test_waiting_lines(self):
        from app.backtest.panel import DAY_MS

        seen = []

        def grab(sh):
            s = sh.summary()
            if s["waiting"]:
                self.same(s["waitingMsg"], s["waiting"])
                seen.append(s["waitingMsg"]["k"])
            else:
                self.assertIsNone(s["waitingMsg"])

        self.clock.t = INC + 20 * 60_000
        self.sh.tick(); grab(self.sh)  # next day not closed
        self.clock.t = INC + DAY_MS + 5 * 60_000
        self.sh.tick(); grab(self.sh)  # settling
        for kw in (dict(after_fn=lambda d: False), dict(ready_fn=lambda d: (False, "BTC:1d,ETH:1d"))):
            sh = self.make(self.led, **kw)
            self.clock.t = INC + DAY_MS + 20 * 60_000
            sh.tick(); grab(sh)
        sh = self.make(self.led, ready_fn=lambda d: (False, "x" * 400))
        sh.tick()
        self.assertEqual(sh.summary()["waitingMsg"], {"s": sh.waiting})  # truncated: raw text
        sh = self.make(self.led)
        sh.panel_fn = lambda d: None
        sh.tick(); grab(sh)
        sh = self.make(self.led)
        sh.perp_fn = lambda ex, c, lo, hi: []
        sh.tick(); grab(sh)
        self.run_to(INC + 35 * DAY_MS); grab(self.sh)  # behind
        self.assertEqual(sorted(set(k.rsplit(".", 1)[1] for k in seen)),
                         sorted(["nextDay", "settling", "paper", "data", "noPanel", "noPerp", "behind"]))

    def test_aborted_line(self):
        for text in ("2026-10-07 abort:drawdown: drawdown -0.0512", "2026-10-07 abort:carry_liquidation: carry leg liquidated", "odd"):
            self.same(h2.aborted_msg(text), text)
        self.assertIsNone(h2.aborted_msg(None))
        # real write path
        p = self.panel
        i = p.days.index(INC + 3 * 86_400_000)
        for c in ("BTC", "ETH"):
            p.perp_close[c][i] *= 1.4
        self.run_to(INC)
        self.run_to(INC + 3 * 86_400_000)
        s = self.sh.summary()
        self.assertTrue(s["aborted"])
        self.same(s["abortedMsg"], s["aborted"])
        self.assertTrue(s["abortedMsg"]["k"].startswith("srv.h2.abort"))


class CatalogTests(unittest.TestCase):
    def test_every_srv_key_is_reachable_and_present(self):
        zh = im.catalog()

        def flat(d, pre=""):
            for k, v in d.items():
                if isinstance(v, dict):
                    yield from flat(v, pre + k + ".")
                else:
                    yield pre + k

        srv = {"srv." + k for k in flat(zh["srv"])}
        for lang in LANGS:
            cat = im.catalog(lang)
            for k in srv:
                self.assertIsInstance(im._lookup(cat, k), str, (lang, k))
        # keys used by the server code exist in the catalog
        code = "".join(Path(p).read_text(encoding="utf-8") for p in
                       ("app/paper/events.py", "app/paper/shadow_h2.py", "app/i18n_msg.py", "app/auth/accounts.py"))
        used = set(re.findall(r'"(srv\.[A-Za-z0-9_.]+)"', code))
        used = {u for u in used if not u.endswith(".")}
        self.assertTrue(used)
        self.assertEqual(used - srv, set())

    def test_unknown_key_raises_and_raw_passes(self):
        with self.assertRaises(KeyError):
            im.render(im.m("srv.nope"))
        self.assertEqual(im.render(im.join([im.raw(" a "), im.raw("b ")], trim=True)), "a  b")


if __name__ == "__main__":
    unittest.main()
