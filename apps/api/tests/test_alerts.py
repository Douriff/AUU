"""Email alerts + daily digest: dedup, rate limits, retries, sources, 08:30 Beijing digest."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app import alerts as al
from app.backtest.panel import DAY_MS
from app.paper import strategy_runner as sr
from app.paper.strategy_risk import HOUR_MS
from tests.test_strategy_risk import Clock, _Base as _RiskBase


def bj_ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=al.BJ).timestamp() * 1000)


class Outbox:
    def __init__(self):
        self.sent = []
        self.fail = 0

    def __call__(self, to, subject, body):
        if self.fail:
            self.fail -= 1
            raise ConnectionError("smtp down for someone@example.com")
        self.sent.append((to, subject, body))


class CenterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(bj_ms("2026-10-04 09:00"))
        self.box = Outbox()
        self.c = al.AlertCenter(Path(self.tmp.name) / "alerts.sqlite", send=self.box, configured=lambda: True, now_ms=self.clock)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_default_recipient_and_env_override(self):
        self.assertEqual(al.recipient(), "olesaruga00@gmail.com")
        with unittest.mock.patch.dict("os.environ", {"AUU_ALERT_TO": "ops@example.com"}):
            self.assertEqual(al.recipient(), "ops@example.com")

    def test_every_recipient_is_masked_in_logs_rows_and_status(self):
        to = "douriff3@gmail.com,olesaruga00@gmail.com"
        c = al.AlertCenter(Path(self.tmp.name) / "m.sqlite", send=self.box, configured=lambda: True, now_ms=self.clock, to=to)
        try:
            with self.assertLogs("auu.alerts", level="WARNING") as cm:
                self.assertEqual(c.raise_alert("k", "test", "s", "b"), "sent")
            text = "\n".join(cm.output) + str(c.rows(1)[0]["to_masked"]) + json.dumps(c.status())
            self.assertNotIn("douriff3@", text)
            self.assertNotIn("olesaruga00@", text)
            self.assertIn("d***@gmail.com, o***@gmail.com", text)
            # rows stored before the fix are re-masked on open
            c._db.execute("UPDATE alerts SET to_masked=?", ("d***@gmail.com, olesaruga00@gmail.com",))
        finally:
            c.close()
        c2 = al.AlertCenter(Path(self.tmp.name) / "m.sqlite", send=self.box, configured=lambda: True, now_ms=self.clock, to=to)
        try:
            self.assertEqual(c2.rows(1)[0]["to_masked"], "d***@gmail.com, o***@gmail.com")
        finally:
            c2.close()

    def test_dedup_within_cooldown_then_resend(self):
        self.assertEqual(self.c.raise_alert("stall:x", "stall", "s", "b"), "sent")
        self.assertEqual(self.c.raise_alert("stall:x", "stall", "s", "b"), "duplicate")
        self.clock.t += 6 * HOUR_MS + 1
        self.assertEqual(self.c.raise_alert("stall:x", "stall", "s", "b"), "sent")
        self.assertEqual(len(self.box.sent), 2)
        self.assertEqual(self.box.sent[0][0], "olesaruga00@gmail.com")

    def test_rate_limit_suppresses_and_digest_bypasses(self):
        for i in range(8):
            self.c.raise_alert(f"k{i}", "risk", f"s{i}", "b")
        self.assertEqual(len(self.box.sent), 6)  # 6 per hour
        self.assertEqual(self.c.status()["last24h"]["suppressed"], 2)
        self.assertEqual(self.c.raise_alert("digest:d", "digest", "d", "b", bypass_limits=True), "sent")

    def test_failed_send_is_retried_with_backoff_and_hides_error_text(self):
        self.box.fail = 1
        self.assertEqual(self.c.raise_alert("k", "risk", "s", "b"), "failed")
        row = self.c.rows(1)[0]
        self.assertEqual(row["error"], "ConnectionError")  # no address from the exception text
        self.assertEqual(self.c.retry_failed(), 0)  # backoff not elapsed
        self.clock.t += 3 * 60_000
        self.assertEqual(self.c.retry_failed(), 1)
        self.assertEqual(self.c.rows(1)[0]["status"], "sent")

    def test_unconfigured_smtp_records_without_sending(self):
        c = al.AlertCenter(Path(self.tmp.name) / "b.sqlite", send=self.box, configured=lambda: False, now_ms=self.clock)
        self.assertEqual(c.raise_alert("k", "risk", "s", "b"), "unconfigured")
        self.assertEqual(self.box.sent, [])
        self.assertFalse(c.status()["configured"])
        c.close()

    def test_record_hash_is_canonical_and_tamper_evident(self):
        run = {"day": 1, "nav_close": 10000.5, "weights": "{}"}
        fills = [{"coin": "ETH", "fee": 1.0}, {"coin": "BTC", "fee": 2.0}]
        h = al.record_hash(run, fills)
        self.assertEqual(h, al.record_hash(dict(reversed(list(run.items()))), list(reversed(fills))))
        self.assertNotEqual(h, al.record_hash({**run, "nav_close": 10000.51}, fills))
        self.assertEqual(len(h), 64)


class MonitorTests(_RiskBase):
    def setUp(self):
        super().setUp()
        self.box = Outbox()
        self.fresh = {"enabled": True, "exchange": "okx", "stale": False, "staleSeries": [], "lastRefreshMs": 0}
        self.r, self.clock, self.e, self.w = self.ready_book()
        self.center = al.AlertCenter(Path(self.tmp.name) / "alerts.sqlite", send=self.box, configured=lambda: True, now_ms=self.clock)
        self.mon = al.AlertMonitor(self.center, runner_fn=lambda: self.r, freshness_fn=lambda: self.fresh)

    def tearDown(self):
        self.center.close()
        super().tearDown()

    def subjects(self):
        return [s for _, s, _ in self.box.sent]

    def test_risk_events_alert_once_and_old_events_do_not_spam(self):
        self.at(self.clock, self.e, 0, 20)  # 08:20 Beijing: before the digest
        self.mon.check()  # cursor starts at the current max id
        self.assertEqual(self.box.sent, [])
        g = self.gross(self.w)
        self.shock = {c: 1 - 0.06 / g for c in self.w}
        self.at(self.clock, self.e, 1, 5)  # 09:05 Beijing -> digest also due; risk first
        self.r.tick()
        self.mon.check()
        risk = [b for _, s, b in self.box.sent if "风控触发" in s]
        self.assertEqual(len(risk), 1)
        self.assertIn("当日 −5% 全部减半", risk[0])
        self.mon.check()
        self.assertEqual(len([s for s in self.subjects() if "风控触发" in s]), 1)

    def test_stall_alert(self):
        self.at(self.clock, self.e + 2, 2)
        self.mon.check()
        self.assertTrue(any("策略停滞" in s for s in self.subjects()))

    def test_stale_data_needs_two_checks_and_no_exchange(self):
        self.at(self.clock, self.e, 0, 15)
        self.fresh = {"enabled": True, "exchange": "okx", "stale": True, "staleSeries": ["BTC:1h"], "lastRefreshMs": self.clock.t}
        self.mon.check()
        self.assertEqual(self.box.sent, [])
        self.mon.check()
        self.assertEqual([s for s in self.subjects() if "数据异常" in s], ["AUUTRADE 告警：数据异常"])
        self.mon.check()
        self.assertEqual(len([s for s in self.subjects() if "数据异常" in s]), 1)  # cooldown
        self.fresh = {"enabled": True, "exchange": None, "stale": False, "lastError": "okx: timeout"}
        self.mon.check()
        self.mon.check()
        self.assertIn("没有可用交易所", self.box.sent[-1][2])

    def test_first_start_after_the_slot_waits_for_the_next_0830(self):
        self.at(self.clock, self.e, 12)  # 20:00 Beijing on first start
        self.mon.check()
        self.assertFalse(any("每日摘要" in s for s in self.subjects()))
        self.at(self.clock, self.e + 1, 0, 31)  # next day 08:31 (rebalance written first)
        self.r.tick()
        self.mon.check()
        self.assertEqual(len([s for s in self.subjects() if "每日摘要" in s]), 1)

    def test_missed_slot_is_caught_up_later_that_day(self):
        self.at(self.clock, self.e, 0, 20)
        self.mon.check()  # armed before the slot
        self.at(self.clock, self.e, 3)  # process was down at 08:30; 11:00 Beijing
        self.mon.check()
        self.assertEqual(len([s for s in self.subjects() if "每日摘要" in s]), 1)

    def test_daily_digest_at_0830_beijing_once(self):
        self.at(self.clock, self.e, 0, 25)  # 08:25 Beijing
        self.mon.check()
        self.assertFalse(any("每日摘要" in s for s in self.subjects()))
        self.at(self.clock, self.e, 0, 31)  # 08:31 Beijing
        self.mon.check()
        dig = [(s, b) for _, s, b in self.box.sent if "每日摘要" in s]
        self.assertEqual(len(dig), 1)
        subj, body = dig[0]
        last = self.r.ledger.last_run()
        self.assertIn(f"NAV {last['nav_close']:,.2f}", subj)
        fills = [dict(f) for f in self.r.ledger.fills(200) if f["day"] == last["day"]]
        self.assertIn(al.record_hash(dict(last), fills), body)
        from app.version import git_commit

        self.assertIn(f"代码版本：{git_commit() or 'unknown'}", body)
        for p in self.r.summary()["positions"]:
            self.assertIn(p["coin"], body)
        self.assertIn("当日调仓", body)
        self.assertIn("风控事件（近 24h）", body)
        self.assertNotIn("password", body.lower())
        self.at(self.clock, self.e, 5)
        self.mon.check()
        self.assertEqual(len([s for s in self.subjects() if "每日摘要" in s]), 1)

    def test_digest_waits_for_todays_rebalance(self):
        self.center.set("digest_armed", 1)
        self.at(self.clock, self.e + 1, 0, 31)  # 08:31 Beijing, but day e+1 not rebalanced yet
        self.mon.check()
        self.assertFalse(any("每日摘要" in s for s in self.subjects()))
        self.r.tick()  # rebalance written
        self.mon.check()
        self.assertTrue(any("每日摘要" in s for s in self.subjects()))


class HealthAndCliTests(unittest.TestCase):
    def test_health_field_is_coarse(self):
        from app.routes.health import _alerts_status

        h = _alerts_status()
        self.assertNotIn("to", h)
        self.assertIn("configured", h)

    def test_cli_test_mail_uses_the_channel(self):
        tmp = tempfile.TemporaryDirectory()
        box = Outbox()
        c = al.AlertCenter(Path(tmp.name) / "a.sqlite", send=box, configured=lambda: True)
        al.reset_center(c)
        try:
            self.assertEqual(al.main(["test"]), 0)
            self.assertEqual(len(box.sent), 1)
            self.assertIn("测试邮件", box.sent[0][1])
            self.assertEqual(al.main(["test"]), 0)  # each test mail has its own key
        finally:
            al.reset_center(None)
            c.close()
            tmp.cleanup()


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()


class DeliveryEvidenceTests(unittest.TestCase):
    def test_self_send_detection(self):
        self.assertTrue(al.self_send("olesaruga00@gmail.com", "olesaruga00@gmail.com"))
        self.assertTrue(al.self_send("Ole.Saruga00+auu@gmail.com", "olesaruga00@gmail.com"))  # same Gmail mailbox
        self.assertFalse(al.self_send("olesaruga00@gmail.com, me@qq.com", "olesaruga00@gmail.com"))
        self.assertFalse(al.self_send("me@qq.com", "olesaruga00@gmail.com"))
        self.assertFalse(al.self_send("a@x.com", ""))
        self.assertEqual(al.recipients("a@x.com; b@y.com ,"), ["a@x.com", "b@y.com"])

    def test_message_id_and_smtp_response_are_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []

            def send(to, s, b):
                sent.append(to)
                return {"message_id": "<abc@gmail.com>", "response": "250 2.0.0 OK 1791 - gsmtp"}

            c = al.AlertCenter(Path(tmp) / "a.sqlite", send=send, configured=lambda: True, to="a@x.com, b@y.com")
            c.smtp_user = lambda: "a@x.com"
            self.assertEqual(c.raise_alert("k", "test", "AUUTRADE 告警测试 #2", "body", bypass_limits=True), "sent")
            row = c.rows(1)[0]
            self.assertEqual(row["message_id"], "<abc@gmail.com>")
            self.assertTrue(row["smtp_response"].startswith("250"))
            self.assertEqual(sent, ["a@x.com, b@y.com"])
            self.assertFalse(c.status()["selfSend"])
            c.close()
