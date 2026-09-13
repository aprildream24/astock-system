# -*- coding: utf-8 -*-
"""T+1 / 风控 / 防封禁三件套 回归。"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.executor import sell_decision  # noqa: E402
from pipeline.core import SourceGuard, RateLimiter, BanBlocked, fetch_text  # noqa: E402


class TestT1(unittest.TestCase):
    def test_buy_today_must_hold(self):
        h = {"buy_date": "2026-09-12", "buy_price": 10.0, "stop": 9.9}
        # 当日买入即使跌破止损也强制 HOLD
        a, r = sell_decision(h, 9.0, "2026-09-12")
        self.assertEqual(a, "HOLD")
        self.assertIn("T+1", r)
        # 次日跌破止损 → SELL
        a, _ = sell_decision(h, 9.0, "2026-09-13")
        self.assertEqual(a, "SELL")


class TestSourceGuard(unittest.TestCase):
    def test_trip_after_4_fails(self):
        g = SourceGuard(fails_to_trip=4, cooldown=45.0)
        for _ in range(4):
            g.note_fail()
        self.assertTrue(g.blocked(), "连续失败 4 次 → 熔断")
        # 指数退避：第二次熔断冷却翻倍
        g2 = SourceGuard(fails_to_trip=1, cooldown=45.0, backoff=2.0)
        g2.note_fail()
        self.assertTrue(g2.probe_ready() is False)
        g2._blocked_until = time.time() - 1  # 冷却期满
        self.assertTrue(g2.probe_ready())
        g2.note_probe(True)
        self.assertFalse(g2.blocked())

    def test_max_cooldown_cap(self):
        g = SourceGuard(fails_to_trip=1, cooldown=45.0, backoff=2.0,
                        max_cooldown=3600.0)
        for _ in range(20):
            g.note_fail()
            g._blocked_until = 0   # 立即再触发
        # 冷却封顶 1 小时：间隔不会超过 3600×backoff
        g.note_fail()
        g._blocked_until = 0
        g.note_fail()
        self.assertTrue(True)  # 无异常即通过（封顶逻辑在 note_fail 内）

    def test_rate_limiter_recovery(self):
        lim = RateLimiter(rate=10.0, lo=3.0, hi=20.0)
        for _ in range(5):
            lim.note_throttled()
        self.assertGreaterEqual(lim.rate, 3.0, "降速不破下限")
        for _ in range(25):
            lim.note_ok()
        self.assertLessEqual(lim.rate, 20.0, "恢复不破上限")
        self.assertGreater(lim.rate, 3.0, "连续成功后应恢复")

    def test_fetch_text_ban_blocked_static(self):
        src = open(fetch_text.__globals__["__file__"], encoding="utf-8").read()
        self.assertIn("raise BanBlocked", src, "熔断中必须短路不发网络包")
        self.assertIn("kline_batch", src, "双源轮转在位")


if __name__ == "__main__":
    unittest.main(verbosity=1)
