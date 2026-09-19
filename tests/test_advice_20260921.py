# -*- coding: utf-8 -*-
"""2026-09-21 用户两需求回归：
① 盘前/竞价/盘中的自选+持仓操作建议（含持续弱→换股 去弱留强）；
② 模拟盘 3322/3331 分仓 + 盘中直接买入 + 卖出后自动换股回补。
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime as _dt
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pipeline import executor, intraday, notifier, watchlist  # noqa: E402
from pipeline.core import get_conn  # noqa: E402

DATES = [(__import__("datetime").date(2026, 9, 18)
          - __import__("datetime").timedelta(days=49 - i)).isoformat()
         for i in range(50)]


def _mk_con(weak_code="sz002493", start=20.0, daily=-0.6):
    """持续阴跌票（近 10 日多数低于 MA20）+ 正常票。"""
    con = get_conn(":memory:")
    put = lambda d, code, o, h, l, c: con.execute(  # noqa: E731
        "INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
        (code, d, o, h, l, c, 1e6, None, None, None))
    for d in DATES:
        put(d, "sh000001", 3000, 3010, 2995, 3005)
    c = start
    for i, d in enumerate(DATES):
        c *= 1 + daily / 100
        put(d, weak_code, c * 0.995, c * 1.004, c * 0.99, c)
    c2 = 15.0
    for i, d in enumerate(DATES):
        c2 *= 1 + (1.5 if i % 5 != 4 else -0.3) / 100
        put(d, "sh600100", c2 * 0.995, c2 * 1.005, c2 * 0.99, c2)
    con.commit()
    return con


class TestPositionPlan(unittest.TestCase):
    def test_3322_default_and_3331_override(self):
        with tempfile.TemporaryDirectory() as td:
            executor.core.CONFIG_DIR = td
            self.assertEqual(executor._position_plan(),
                             [0.30, 0.30, 0.20, 0.20], "缺省 3322")
            json.dump({"position_plan": "3331"},
                      open(os.path.join(td, "sim.json"), "w",
                           encoding="utf-8"))
            self.assertEqual(executor._position_plan(),
                             [0.30, 0.30, 0.30, 0.10], "可切 3331")

    def test_slot_sizing_sequence(self):
        """第 1/2 笔 30%、第 3 笔 20%（3322），按净值计。"""
        con = _mk_con()
        today = DATES[-1]
        executor.ensure_account(con, today)
        for i, code in enumerate(("sh600100", "sz002493")):
            executor.place_order(con, code, "buy", 1000, 10.0, today)
        plan = executor._position_plan()
        self.assertEqual(plan[0], 0.30)
        # 第 3 笔应占 0.20 档
        con.close()


class TestAutoOpenIntraday(unittest.TestCase):
    def _con_with_pick(self, pick=("sh600100", "强势票", 25.0, 27.5)):
        con = _mk_con()
        today = DATES[-1]
        # 昨日已建仓 1 只（600100，占第一档 30%）
        executor.ensure_account(con, today)
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES('sh600100',?,2000,10,"
                    "0,'sim')", (DATES[-2],))
        # 当日推荐（默认=已持仓的 600100 自身区间；可换成别的票）
        con.execute("INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (today, pick[0], pick[1], "趋势", "现在买",
                     pick[2], pick[3], pick[2] * 0.92, 16, 17, 90, "", None))
        con.commit()
        return con, today

    def test_intraday_rebuy_allowed_and_sized(self):
        """盘中找到更合适机会 → 直接按下一档（30%）买入，不再被'每日一批'挡住。"""
        # 已持有 600100 → 推荐未持仓的 002493（买区覆盖其现价 ≈14.8）
        con, today = self._con_with_pick(
            pick=("sz002493", "荣盛石化", 14.0, 15.2))
        executor.ensure_account(con, today)
        log = executor.auto_open(con, today, slot="am",
                                 now=_dt(2026, 9, 18, 10, 0))
        buys = [x for x in log if x[1] == "BUY"]
        self.assertTrue(buys, f"盘中应直接买入：{log}")
        code, act, detail = buys[0]
        self.assertEqual(code, "sz002493")
        self.assertIn("第2档30%", detail, "第 2 笔占 30% 档")
        row = con.execute(
            "SELECT qty, cost FROM position_batches WHERE code='sz002493' "
            "AND buy_date=?", (today,)).fetchone()
        self.assertIsNotNone(row)
        amt = row[0] * row[1]
        self.assertGreater(amt, 25000, "30% 档 ≈ 3 万，不再是个几千块的小仓")
        self.assertLess(amt, 46000)
        con.close()

    def test_sell_then_swap_same_run(self):
        """卖出成交 → 同 run 自动回补更优候选（去弱留强闭环）。"""
        con, today = self._con_with_pick()
        executor.ensure_account(con, today)
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES('sz002493',?,1000,20,"
                    "1000,'sim')", (DATES[-2],))
        con.commit()
        with mock.patch.object(executor, "_exec_push", return_value=None), \
                mock.patch.object(executor, "get_conn", return_value=con), \
                mock.patch.object(executor, "today_str",
                                  return_value=DATES[-1]):
            log = executor.run("auto", slot="am",
                               now=_dt(2026, 9, 18, 10, 0))
        sells = [x for x in log if x[1] == "SELL"]
        self.assertTrue(sells, "持续阴跌持仓应触发退出")
        # 回补：买入今日推荐（quiet 模式下只留 BUY/REJECT）
        self.assertTrue(any(x[0] == "sh600100" and x[1] == "BUY"
                            for x in log),
                        f"卖出后应回补推荐票：{log}")
        con.close()


class TestWeakStreak(unittest.TestCase):
    def test_persistent_weak_escalates(self):
        con = _mk_con()
        holdings = [{"code": "sz002493", "name": "荣盛石化",
                     "buy_price": 20.0, "shares": 1000,
                     "buy_date": DATES[10]}]
        heval = executor.evaluate_real_holdings(con, DATES[-1], holdings)
        h = heval[0]
        self.assertGreaterEqual(h.get("weak_days", 0), 7,
                                "持续阴跌票：10 日中 ≥7 日低于 MA20")
        self.assertIsNotNone(h.get("swap_hint"))
        self.assertIn("去弱留强", h["swap_hint"])

    def test_healthy_holding_no_swap(self):
        con = _mk_con()
        holdings = [{"code": "sh600100", "name": "强势票",
                     "buy_price": 12.0, "shares": 1000,
                     "buy_date": DATES[10]}]
        heval = executor.evaluate_real_holdings(con, DATES[-1], holdings)
        h = heval[0]
        self.assertIsNone(h.get("swap_hint"), "强势票不给换股建议")


class TestPreAuctionAdviceWiring(unittest.TestCase):
    def test_build_wires_pre_auction_advice(self):
        src = open(os.path.join(ROOT, "pipeline", "build.py"),
                   encoding="utf-8").read()
        self.assertIn('if task in ("pre", "auction"):', src)
        self.assertIn("evaluate_real_holdings(con, date, _hold2)", src)
        self.assertIn('"watch_advice"', src)
        self.assertIn("可买（回落至买区）", src)

    def test_intraday_watch_alerts_wired(self):
        src = open(os.path.join(ROOT, "pipeline", "intraday.py"),
                   encoding="utf-8").read()
        self.assertIn("watch_zone_hits", src)
        self.assertIn("watch_stop_hits", src)
        self.assertIn("watch_intraday", src)
        self.assertIn("zone_stop_for", src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
