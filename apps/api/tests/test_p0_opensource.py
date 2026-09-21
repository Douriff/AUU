"""P0 open-source alignment: self-computed stats, no Jesse/QuantStats/vectorbt, RiskGate checklist."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.models.contracts import Fill
from app.paper.ledger import PaperTradeJournal, summarize
from app.paper.pipeline import run_paper_order
from app.risk.gate import REASON

ROOT = Path(__file__).resolve().parents[3]
API_APP = ROOT / "apps" / "api" / "app"
REQ = ROOT / "apps" / "api" / "requirements.txt"
WEB_PKG = ROOT / "apps" / "web" / "package.json"
ROOT_PKG = ROOT / "package.json"

BANNED = ("jesse", "quantstats", "vectorbt", "vnpy", "freqtrade", "hftbacktest", "nautilus", "backtrader")


def _iter_py(root: Path):
    for p in root.rglob("*.py"):
        yield p


def _top_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0].lower())
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0].lower())
    return names


class ForbiddenRuntimeTests(unittest.TestCase):
    def test_requirements_have_no_banned_libs(self):
        text = REQ.read_text(encoding="utf-8").lower()
        for name in BANNED:
            self.assertNotIn(name, text, f"{name} must not be an API dependency")
        for pkg in (WEB_PKG, ROOT_PKG):
            raw = pkg.read_text(encoding="utf-8").lower()
            for name in BANNED:
                self.assertNotIn(name, raw, f"{name} must not be a web/root dependency")

    def test_app_does_not_import_banned_libs(self):
        seen: list[str] = []
        for path in _iter_py(API_APP):
            for name in _top_imports(path):
                if name in BANNED:
                    seen.append(f"{path.relative_to(ROOT)}:{name}")
        self.assertEqual(seen, [])


class JournalStatsSelfComputeTests(unittest.TestCase):
    def test_win_rate_is_wins_over_n_from_trades(self):
        journal = PaperTradeJournal()
        journal.record_fill("A/SOL", Fill(ts=1, price=1.0, qty=1.0, fee=0.0))
        journal.record_fill("A/SOL", Fill(ts=2, price=1.2, qty=-1.0, fee=0.0))
        journal.record_fill("B/SOL", Fill(ts=3, price=2.0, qty=1.0, fee=0.0))
        journal.record_fill("B/SOL", Fill(ts=4, price=1.5, qty=-1.0, fee=0.0))
        stats = summarize(journal.closed)
        self.assertEqual(stats["n_trades"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertAlmostEqual(stats["win_rate"], stats["wins"] / stats["n_trades"])
        pnls = [t.pnl for t in journal.closed]
        self.assertAlmostEqual(stats["expectancy"], sum(pnls) / len(pnls))
        self.assertIsNone(stats["monte_carlo"])
        self.assertFalse(stats["mc"])


class RiskGateChecklistTests(unittest.TestCase):
    def test_mapped_hard_gate_tags_exist(self):
        for tag in (
            "TRADING_HALTED",
            "REDUCE_ONLY",
            "POSITION_CAP",
            "DAY_LOSS_BREAKER",
            "COOLDOWN",
            "SPREAD_TOO_WIDE",
            "SLIPPAGE_CAP",
        ):
            self.assertIn(tag, REASON)

    def test_fills_go_through_paper_broker_only(self):
        src = Path(run_paper_order.__code__.co_filename).read_text(encoding="utf-8")
        self.assertIn("get_paper_broker", src)
        self.assertIn("broker.submit", src)
        lowered = src.lower()
        self.assertNotIn("sendtransaction", lowered)
        self.assertNotIn("sniper", lowered)


if __name__ == "__main__":
    unittest.main()
