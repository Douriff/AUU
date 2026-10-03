"""Manual paper take-profit / stop-loss and OCO pairs (the automated strategy never uses these)."""
from __future__ import annotations

import unittest

from app.paper.mainstream_account import OrderError
from tests.test_mainstream_paper import _Base

M = 60_000


def bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


class TpSlTests(_Base):
    def setUp(self):
        super().setUp()
        self.buy = self.order(qty=0.01)  # 0.01 BTC @ ~100k
        self.m0 = (self.clock() // M) * M  # bar the TP/SL will be placed in

    def tpsl(self, **kw):
        self.n += 1
        self.clock.t += 5_000
        body = {"client_order_id": f"tp-{self.n:06d}", "symbol": "BTC", "side": "sell"}
        body.update(kw)
        return self.acc.place("u1", body)

    def err(self, **kw):
        with self.assertRaises(OrderError) as ctx:
            self.tpsl(**kw)
        return ctx.exception.code

    def test_validation_and_limits(self):
        self.assertEqual(self.err(type="take_profit", qty=0.01, trigger_price=99_000), "BAD_PRICE")  # TP must be above
        self.assertEqual(self.err(type="stop_loss", qty=0.01, trigger_price=101_000), "BAD_PRICE")  # SL must be below
        self.assertEqual(self.err(type="stop_loss", qty=0.01, trigger_price=40_000), "BAD_PRICE")  # +/-50% band
        self.assertEqual(self.err(type="take_profit", qty=0.015, trigger_price=110_000), "INSUFFICIENT_POSITION")
        self.assertEqual(self.err(type="take_profit", qty=0.01, trigger_price=110_000, side="buy"), "BAD_ORDER")
        self.assertEqual(self.err(type="stop_loss", qty=0.00005, trigger_price=95_000), "ORDER_TOO_SMALL")
        self.assertEqual(self.err(type="oco", qty=0.01, take_profit_price=110_000), "BAD_PRICE")  # needs both legs
        self.assertEqual(self.err(type="take_profit", symbol="ETH", qty=0.1, trigger_price=4_400), "INSUFFICIENT_POSITION")
        self.assertEqual(self.err(type="take_profit", qty=0.01), "BAD_PRICE")

    def test_per_order_cap_checked_at_trigger_price(self):
        self.order(qty=0.01)  # 0.02 BTC held
        self.mkt.px["BTC"] = 100_000.0
        # 0.0195 * 110k = 2145 > 2000 per-order cap
        self.assertEqual(self.err(type="take_profit", qty=0.0195, trigger_price=110_000), "ORDER_CAP")

    def test_oco_reserves_quantity_once_and_blocks_oversell(self):
        r = self.tpsl(type="oco", qty=0.01, take_profit_price=110_000, stop_loss_price=95_000)
        cid = f"tp-{self.n:06d}"
        self.assertEqual(len(r["orders"]), 2)
        self.assertTrue(r["ocoGroup"])
        self.assertEqual({o["type"] for o in r["orders"]}, {"take_profit", "stop_loss"})
        snap = self.acc.snapshot("u1")
        self.assertAlmostEqual(snap["positions"][0]["available"], 0.0)  # 0.01 reserved once, not twice
        self.assertEqual(len(snap["openOrders"]), 2)
        self.assertEqual(self.code(side="sell", qty=0.005), "INSUFFICIENT_POSITION")
        # idempotent resubmit
        again = self.acc.place("u1", {"client_order_id": cid, "symbol": "BTC", "side": "sell", "type": "oco",
                                      "qty": 0.01, "take_profit_price": 110_000, "stop_loss_price": 95_000})
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.acc.snapshot("u1")["openOrders"]), 2)

    def test_take_profit_fills_as_market_and_cancels_the_other_leg(self):
        r = self.tpsl(type="oco", qty=0.01, take_profit_price=110_000, stop_loss_price=95_000)
        # same-minute spike is ignored (placement bar), then a later bar trades through the TP
        self.mkt.bars["BTC"] = [bar(self.m0, 100_000, 120_000, 99_000, 100_000),
                                bar(self.m0 + M, 100_000, 100_500, 99_500, 100_000),
                                bar(self.m0 + 2 * M, 100_000, 111_000, 99_800, 110_500)]
        snap = self.acc.snapshot("u1")
        by = {o["type"]: o for o in snap["orders"] if o["type"] in ("take_profit", "stop_loss")}
        tp, sl = by["take_profit"], by["stop_loss"]
        self.assertEqual(tp["status"], "filled")
        self.assertEqual(tp["reason"], "TP_TRIGGERED")
        self.assertAlmostEqual(tp["fillPrice"], 110_000 * (1 - 0.0001))  # market at the trigger, minus slippage
        self.assertAlmostEqual(tp["fee"], tp["notional"] * 0.0005)  # taker fee
        self.assertEqual(tp["fillTs"], self.m0 + 2 * M)
        self.assertEqual(sl["status"], "cancelled")
        self.assertEqual(sl["reason"], "OCO_CANCEL")
        self.assertEqual(snap["positions"][0]["qty"], 0.0)
        self.assertEqual(snap["openOrders"], [])
        self.assertAlmostEqual(tp["realized"], (tp["fillPrice"] - self.buy["fillPrice"]) * 0.01 - tp["fee"])
        self.assertEqual(r["ocoGroup"], tp["ocoGroup"])

    def test_stop_loss_gap_fills_at_open_and_same_bar_prefers_stop(self):
        self.tpsl(type="oco", qty=0.01, take_profit_price=105_000, stop_loss_price=95_000)
        # one bar touches both legs and opens below the stop: conservative -> stop at the (worse) open
        self.mkt.bars["BTC"] = [bar(self.m0 + M, 94_000, 106_000, 93_000, 100_000)]
        snap = self.acc.snapshot("u1")
        by = {o["type"]: o for o in snap["orders"] if o["type"] in ("take_profit", "stop_loss")}
        self.assertEqual(by["stop_loss"]["status"], "filled")
        self.assertEqual(by["stop_loss"]["reason"], "SL_TRIGGERED")
        self.assertAlmostEqual(by["stop_loss"]["fillPrice"], 94_000 * (1 - 0.0001))
        self.assertEqual(by["take_profit"]["status"], "cancelled")

    def test_single_leg_and_cancel(self):
        sl = self.tpsl(type="stop_loss", qty=0.005, trigger_price=95_000)
        self.assertEqual(sl["status"], "open")
        self.assertIsNone(sl["ocoGroup"])
        self.assertAlmostEqual(self.acc.snapshot("u1")["positions"][0]["available"], 0.005)
        self.acc.cancel("u1", sl["id"])
        self.assertAlmostEqual(self.acc.snapshot("u1")["positions"][0]["available"], 0.01)

    def test_cancelling_one_oco_leg_cancels_the_pair(self):
        r = self.tpsl(type="oco", qty=0.01, take_profit_price=110_000, stop_loss_price=95_000)
        self.acc.cancel("u1", r["orders"][0]["id"])
        snap = self.acc.snapshot("u1")
        self.assertEqual(snap["openOrders"], [])
        self.assertEqual(sorted(o["reason"] for o in snap["orders"] if o.get("ocoGroup")), ["OCO_CANCEL", "USER_CANCEL"])

    def test_position_sold_elsewhere_cancels_on_trigger(self):
        self.tpsl(type="take_profit", qty=0.005, trigger_price=110_000)
        self.order(side="sell", qty=0.005)  # sells the unreserved half
        # simulate the reserved half disappearing (e.g. a reset): direct DB edit
        self.acc._db.execute("UPDATE positions SET qty=0.001 WHERE user_id='u1'")
        self.acc._db.commit()
        self.mkt.bars["BTC"] = [bar(self.m0 + M, 100_000, 111_000, 99_000, 110_000)]
        tp = [o for o in self.acc.snapshot("u1")["orders"] if o["type"] == "take_profit"][0]
        self.assertEqual(tp["status"], "cancelled")
        self.assertEqual(tp["reason"], "INSUFFICIENT_POSITION")

    def test_long_resting_order_advances_checkpoint_and_reads_candles_once_per_pass(self):
        self.tpsl(type="oco", qty=0.01, take_profit_price=110_000, stop_loss_price=95_000)
        calls = []
        orig = self.mkt.candles

        def counting(sym, since):
            calls.append((sym, since))
            return orig(sym, since)[:3]  # a store that only returns a few bars per read

        self.acc.candles_fn = counting
        self.mkt.bars["BTC"] = [bar(self.m0 + i * M, 100_000, 100_100, 99_900, 100_000) for i in range(1, 10)]
        self.mkt.bars["BTC"].append(bar(self.m0 + 10 * M, 100_000, 100_100, 94_000, 95_500))
        for _ in range(6):
            self.acc.snapshot("u1")
        passes = 7
        sl = [o for o in self.acc.snapshot("u1")["orders"] if o["type"] == "stop_loss"][0]
        self.assertEqual(sl["status"], "filled")
        self.assertEqual(sl["fillTs"], self.m0 + 10 * M)
        # both OCO legs share one read per pass (the fill happens on pass 4, later passes have no open orders)
        self.assertLessEqual(len(calls), passes)
        self.assertEqual([s for _, s in calls], sorted(s for _, s in calls))  # the checkpoint only moves forward
        self.assertTrue(all(sym == "BTC" for sym, _ in calls))

    def test_strategy_untouched_market_and_limit_still_work(self):
        self.tpsl(type="stop_loss", qty=0.005, trigger_price=95_000)
        s = self.order(side="sell", qty=0.005)
        self.assertEqual(s["status"], "filled")
        lim = self.order(side="buy", type="limit", qty=0.001, limit_price=90_000)
        self.assertEqual(lim["status"], "open")


class RouteTests(unittest.TestCase):
    def test_body_accepts_trigger_fields_and_forbids_others(self):
        from pydantic import ValidationError

        from app.routes.mainstream_paper import OrderBody

        b = OrderBody(client_order_id="x" * 10, symbol="BTC", side="sell", type="oco", qty=0.01,
                      take_profit_price=1.0, stop_loss_price=0.5)
        self.assertEqual(b.take_profit_price, 1.0)
        with self.assertRaises(ValidationError):
            OrderBody(client_order_id="x" * 10, symbol="BTC", side="sell", trailing=1)


if __name__ == "__main__":
    unittest.main()
