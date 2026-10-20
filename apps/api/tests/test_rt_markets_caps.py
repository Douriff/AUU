"""/api/v1/mainstream/markets advertises the realtime push only while it runs (frontend step 3 gate)."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.marketdata import realtime as RT
from app.marketdata.realtime.manager import RealtimeManager
from app.routes import mainstream as R
from tests.test_realtime_ws import fast_cfg


class _Svc:
    def markets(self, holdings=None):
        return {"exchange": "binance", "quote": "USDT", "items": [], "asOf": 1, "tickers": {}}


def _body(resp):
    return json.loads(resp.body)["data"] if hasattr(resp, "body") else resp["data"]


class MarketsCapsTests(unittest.TestCase):
    def tearDown(self):
        RT.set_manager(None)

    def _call(self):
        with patch.object(R, "get_service", lambda: _Svc()), patch.object(R, "_strategy_holdings", lambda: {}):
            return _body(R.markets())

    def test_switch_off_response_unchanged(self):
        RT.set_manager(None)
        self.assertEqual(self._call(), _Svc().markets())

    def test_caps_only_while_running_with_push(self):
        m = RealtimeManager(fast_cfg(exchanges=("binance",)), bases=["BTC", "ETH"], feed_kw={"connect": object()})
        RT.set_manager(m)
        self.assertNotIn("realtime", self._call())  # not running yet
        m.running = True
        m.selected = "binance"
        caps = self._call()["realtime"]
        self.assertEqual(caps, {"push": True, "intervals": ["1h", "4h", "1d"], "symbols": ["BTC", "ETH"],
                                "staleMs": m.cfg.stale_ms, "live": True, "source": "binance"})
        m.cfg = replace(m.cfg, push=False)  # AUU_RT_WS_PUSH=off: overlay only, no push -> no caps
        self.assertNotIn("realtime", self._call())


if __name__ == "__main__":
    unittest.main()
