# -*- coding: utf-8 -*-
"""模拟盘亏损统一修正回归锁（2026-10-04）。

取证结论（CI 日志还原的 09-28~10-01 完整交易链，见交付报告）：
  · 09-28 情绪 36.4「离场为主」（日志明写）→ 执行器照样建仓 3 笔
    73,959 元（74% 仓位）；09-30 情绪 36.1 又建 1 笔 —— 冷市满仓；
  · sz002614（首推，30% 档 31,697 元）次日三线齐破止损；sz002935
    次日硬止损 —— 「竞价买入 → 次日止损」隔日止损循环两连发；
  · sh603978 早盘 +4.6% → 收盘 -3.0%，7.6 个点浮盈回吐，既有规则
    （-3% 硬止损只看亏损端、+15% 止盈太远）全部沉默。

三个失败模式，本套件各锁一条修正（全部内存库，绝不碰生产库，
notifier 不会被调用——本套件只测 auto_open / evaluate_exit 本体）：
  F1 情绪裁决闸门：day_meta「离场为主/观望为主」→ 不建仓；
     「谨慎/轻仓试探」→ 本轮限 1/2 笔且单笔 ≤20%；day_meta 缺行时
     从 emotion_log 降级推导（同口径）。
  F2 首推不拿大档：≥25% 大档位只给双确认以上（confirm_log 近 10 天
     被推送 ≥2 天，与卡片确认标签同一把尺）；首推票降到 20% 小仓档。
  F3 盈利回吐保护：高水位 ≥+3% 回吐到 +0.5% 以下 → SELL；
     新买入的高水位必须归零（否则买回即触发回吐误卖）。
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pipeline.core as core              # noqa: E402
import pipeline.executor as executor      # noqa: E402

# 2026-10-09：节后首个周五交易日（10-08 周四复盘，10-09 周五盘中）。
DATE = "2026-10-09"
PREV = "2026-10-08"
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone(timedelta(hours=8)))


def _mkcon():
    con = sqlite3.connect(":memory:")
    con.executescript(core._SCHEMA)
    return con


def _price(con, code, price, date=DATE, prev=None):
    con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, date, price, price, price, price, 1e6, 3e7, 0.0, 1.0))
    if prev:
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (code, PREV, prev, prev, prev, prev, 1e6, 3e7, 0.0, 1.0))
    con.commit()


def _plan(con, code, lo, hi, score=80.0, action="现在买", date=DATE):
    con.execute("INSERT OR REPLACE INTO rec_picks VALUES("
                "?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (date, code, "票" + code[-2:], "趋势", action, lo, hi,
                 round(lo * 0.95, 2), None, None, score, "", None))
    con.commit()


def _hist(con, code, closes, start="2026-09-01"):
    """写一段日K历史（低点 = 收盘*0.99），构造 ATR/MA20 场景用。"""
    import datetime as _dt
    d = _dt.date.fromisoformat(start)
    for c in closes:
        while d.weekday() >= 5:
            d += _dt.timedelta(days=1)
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (code, d.isoformat(), c, c, c * 0.99, c, 1e6, 3e7, 0.0, 1.0))
        d += _dt.timedelta(days=1)
    con.commit()


def _verdict(con, level, mood=52.0, date=DATE):
    con.execute("INSERT OR REPLACE INTO day_meta VALUES(?,?,?,?,?,?)",
                (date, level, f"测试裁决 {level}", mood, "均衡",
                 "2026-10-09T09:00:00"))
    con.commit()


def _confirms(con, code, days):
    """confirm_log 打确认次数（与 build._confirm_counts 同构）。"""
    for i, (d, task) in enumerate(days):
        con.execute("INSERT OR REPLACE INTO confirm_log VALUES(?,?,?)",
                    (d, task, code))
    con.commit()


class TestF1VerdictGate(unittest.TestCase):
    """F1：当日仓位裁决是建仓的硬输入，不再是只进推送的文案。"""

    def test_离场为主_不建仓(self):
        con = _mkcon()
        _price(con, "sz002614", 9.13, prev=9.10)
        _plan(con, "sz002614", 8.90, 9.30, score=90)
        _verdict(con, "离场为主", mood=36.4)
        log = executor.auto_open(con, DATE, now=NOW)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0][0], "-")
        self.assertEqual(log[0][1], "HOLD")
        self.assertIn("离场为主", log[0][2])
        self.assertIn("36", log[0][2])
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_观望为主_不建仓(self):
        con = _mkcon()
        _price(con, "sh600356", 11.06, prev=11.00)
        _plan(con, "sh600356", 10.80, 11.20, score=88)
        _verdict(con, "观望为主", mood=50.0)
        log = executor.auto_open(con, DATE, now=NOW)
        self.assertEqual(log[0][1], "HOLD")
        self.assertIn("观望为主", log[0][2])

    def test_谨慎_限一笔且降小仓(self):
        con = _mkcon()
        _price(con, "sz002614", 9.13, prev=9.10)
        _price(con, "sh600356", 11.06, prev=11.00)
        _plan(con, "sz002614", 8.90, 9.30, score=90)
        _plan(con, "sh600356", 10.80, 11.20, score=85)
        _confirms(con, "sz002614", [(DATE, "close")])
        _confirms(con, "sh600356", [(PREV, "close"), (DATE, "pre")])
        _verdict(con, "谨慎", mood=48.0)
        log = executor.auto_open(con, DATE, now=NOW)
        buys = [e for e in log if e[1] == "BUY"]
        self.assertEqual(len(buys), 1, "谨慎裁决本轮最多 1 笔")
        amt = con.execute(
            "SELECT qty*price FROM fills WHERE side='buy'").fetchone()[0]
        self.assertLessEqual(amt, 100000 * 0.20 * 1.05,
                             "谨慎裁决单笔不得超过小仓档 20%")

    def test_轻仓试探_限两笔(self):
        con = _mkcon()
        for code, price, lo, hi, sc in (
                ("sz002614", 9.13, 8.90, 9.30, 90),
                ("sh600356", 11.06, 10.80, 11.20, 85),
                ("sh603978", 26.10, 25.50, 26.50, 80)):
            _price(con, code, price, prev=price * 0.995)
            _plan(con, code, lo, hi, score=sc)
            _confirms(con, code, [(PREV, "close"), (DATE, "pre")])
        _verdict(con, "轻仓试探", mood=50.0)
        log = executor.auto_open(con, DATE, now=NOW)
        buys = [e for e in log if e[1] == "BUY"]
        self.assertEqual(len(buys), 2, "轻仓试探本轮最多 2 笔")

    def test_day_meta缺失_从emotion_log降级推导(self):
        con = _mkcon()
        _price(con, "sz002614", 9.13, prev=9.10)
        _plan(con, "sz002614", 8.90, 9.30, score=90)
        con.execute("INSERT OR REPLACE INTO emotion_log VALUES(?,?,?,?,?,?,?)",
                    (DATE, 36.4, 7, 0.67, 1, "退潮", "{}"))
        con.commit()
        log = executor.auto_open(con, DATE, now=NOW)
        self.assertEqual(log[0][1], "HOLD")
        self.assertIn("离场为主", log[0][2])

    def test_day_meta缺失_覆盖不足推导谨慎(self):
        con = _mkcon()
        _price(con, "sz002614", 9.13, prev=9.10)
        _plan(con, "sz002614", 8.90, 9.30, score=90)
        con.execute("INSERT OR REPLACE INTO emotion_log VALUES(?,?,?,?,?,?,?)",
                    (DATE, 50.0, 0, 0.0, 0, "震荡", "{}"))
        con.commit()
        log = executor.auto_open(con, DATE, now=NOW)
        buys = [e for e in log if e[1] == "BUY"]
        self.assertEqual(len(buys), 1, "谨慎推导 → 限 1 笔")
        amt = con.execute(
            "SELECT qty*price FROM fills WHERE side='buy'").fetchone()[0]
        self.assertLessEqual(amt, 100000 * 0.20 * 1.05)

    def test_可开仓_正常按分仓计划(self):
        con = _mkcon()
        _price(con, "sz002614", 9.13, prev=9.10)
        _plan(con, "sz002614", 8.90, 9.30, score=90)
        _confirms(con, "sz002614", [(PREV, "close"), (DATE, "pre")])
        _verdict(con, "可开仓", mood=62.0)
        log = executor.auto_open(con, DATE, now=NOW)
        buys = [e for e in log if e[1] == "BUY"]
        self.assertEqual(len(buys), 1)
        amt = con.execute(
            "SELECT qty*price FROM fills WHERE side='buy'").fetchone()[0]
        self.assertGreater(amt, 100000 * 0.28, "可开仓按计划拿满 30% 档")

    def test_无裁决无情绪_不设闸照常(self):
        con = _mkcon()
        _price(con, "sz002614", 9.13, prev=9.10)
        _plan(con, "sz002614", 8.90, 9.30, score=90)
        _confirms(con, "sz002614", [(PREV, "close"), (DATE, "pre")])
        log = executor.auto_open(con, DATE, now=NOW)
        self.assertTrue(any(e[1] == "BUY" for e in log),
                        "两个数据源都缺失时保持原行为（不误伤）")


class TestF2FirstPickDemotion(unittest.TestCase):
    """F2：30% 大档只给双确认以上；首推票最高小仓档。"""

    def test_首推降档_双确认拿大档(self):
        con = _mkcon()
        # 首推 sz002614（确认 1 次）评分最高排前面；sh600356 双确认排后。
        _price(con, "sz002614", 9.13, prev=9.10)
        _price(con, "sh600356", 11.06, prev=11.00)
        _plan(con, "sz002614", 8.90, 9.30, score=90)
        _plan(con, "sh600356", 10.80, 11.20, score=85)
        _confirms(con, "sz002614", [(DATE, "close")])
        _confirms(con, "sh600356", [(PREV, "close"), (DATE, "pre")])
        _verdict(con, "可开仓", mood=62.0)
        log = executor.auto_open(con, DATE, now=NOW)
        by_code = {e[0]: e[2] for e in log if e[1] == "BUY"}
        self.assertIn("首推降档", by_code["sz002614"])
        self.assertIn("20%", by_code["sz002614"])
        self.assertNotIn("首推降档", by_code["sh600356"])
        self.assertIn("30%", by_code["sh600356"])
        amt = dict(con.execute(
            "SELECT code, qty*price FROM fills WHERE side='buy'").fetchall())
        self.assertLessEqual(amt["sz002614"], 100000 * 0.20 * 1.05)
        self.assertGreater(amt["sh600356"], 100000 * 0.28)

    def test_三确认照常大档(self):
        con = _mkcon()
        _price(con, "sh600356", 11.06, prev=11.00)
        _plan(con, "sh600356", 10.80, 11.20, score=90)
        _confirms(con, "sh600356", [
            ("2026-10-07", "close"), (PREV, "pre"), (DATE, "auction")])
        _verdict(con, "可开仓", mood=62.0)
        log = executor.auto_open(con, DATE, now=NOW)
        detail = next(e[2] for e in log if e[1] == "BUY")
        self.assertNotIn("首推降档", detail)
        self.assertIn("30%", detail)


class TestF3GivebackProtection(unittest.TestCase):
    """F3：盈利回吐保护 + 新仓高水位归零。"""

    def _hold(self, con, code="sh603978", cost=26.10, close=None):
        # 10 根平价历史（low=0.99c，ATR 保护线不触发），最后一根定 P&L。
        _hist(con, code, [26.0] * 9, start="2026-09-20")
        _price(con, code, close if close else cost * 1.003, date=DATE)
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES(?,?,?,?,1,'sim')",
                    (code, PREV, 800, cost))
        con.commit()

    def test_高水位回吐触发卖出(self):
        con = _mkcon()
        self._hold(con, close=26.10 * 1.003)          # 现价 +0.3%
        con.execute("INSERT OR REPLACE INTO pos_hwm VALUES(?,?,?)",
                    ("sh603978", 4.6, "2026-09-30T14:40:00"))
        con.commit()
        action, reasons, detail = executor.evaluate_exit(con, "sh603978", DATE)
        self.assertEqual(action, "SELL")
        self.assertTrue(any("盈利回吐保护" in r for r in reasons),
                        f"reasons={reasons}")
        self.assertIn("4.6", "".join(reasons))
        self.assertIn("+0.3", "".join(reasons))

    def test_回吐未到阈值_持有(self):
        con = _mkcon()
        self._hold(con, close=26.10 * 1.02)           # 现价 +2.0%
        con.execute("INSERT OR REPLACE INTO pos_hwm VALUES(?,?,?)",
                    ("sh603978", 4.6, "2026-09-30T14:40:00"))
        con.commit()
        action, reasons, _ = executor.evaluate_exit(con, "sh603978", DATE)
        self.assertEqual(action, "HOLD", "现 +2.0% 仍在保护带内")

    def test_新高水位续创_不误卖(self):
        con = _mkcon()
        self._hold(con, close=26.10 * 1.05)           # 现价 +5.0% 新高
        action, reasons, _ = executor.evaluate_exit(con, "sh603978", DATE)
        self.assertEqual(action, "HOLD")
        hwm = con.execute(
            "SELECT hwm FROM pos_hwm WHERE code='sh603978'").fetchone()[0]
        self.assertGreaterEqual(hwm, 5.0, "高水位应被创新高抬升")

    def test_新买入高水位归零(self):
        con = _mkcon()
        _price(con, "sz002614", 9.13, prev=9.10)
        con.execute("INSERT OR REPLACE INTO pos_hwm VALUES(?,?,?)",
                    ("sz002614", 8.0, "2026-09-28T09:26:00"))
        con.commit()
        executor.place_order(con, "sz002614", "buy", 2000, 9.13, DATE,
                             prev_close=9.10, reason="测试买回")
        self.assertIsNone(con.execute(
            "SELECT 1 FROM pos_hwm WHERE code='sz002614'").fetchone(),
            "旧高水位不清零会让回吐保护拿新仓成本对旧高点误判")


if __name__ == "__main__":
    unittest.main()
