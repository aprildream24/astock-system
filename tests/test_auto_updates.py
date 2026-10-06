# -*- coding: utf-8 -*-
"""三项全量自动化功能回归锁（2026-10-05 用户「全部同意，全部做」）。

① 月度/半月周期复盘自动触发：每日复盘 run 顺带检查（1-3 日补跑 30 天档、
   14-17 日补跑 15 天档，遇假期自动顺延），job_state 幂等防重。
② 站点持仓收益曲线：近 30 交易日组合累计盈亏%（有股数=市值加权，
   缺股数=等权均盈亏）；发布端仅 buy/all 角色可见，observe 密文里直接剥除。
③ 盘中买点提醒相关度排序：自选股最先 → 与持仓同板块次之 → 其余按分；
   只排序不筛选，到价照推。
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
import pipeline.build as build_mod        # noqa: E402
import pipeline.intraday as intraday      # noqa: E402
import pipeline.publish as publish        # noqa: E402


def _mkcon():
    con = sqlite3.connect(":memory:")
    con.executescript(core._SCHEMA)
    return con


def _bar(con, code, date, close):
    con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, date, close, close, close * 0.99, close,
                 1e6, 3e7, 0.0, 1.0))


def _seed_days(con, days, base="2026-10-"):
    import datetime as _dt
    d = _dt.date(2026, 10, 1)
    out = []
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    for i, dt_ in enumerate(out):
        _bar(con, "sh000001", dt_, 3000 + i)
        _bar(con, "sz300192", dt_, 10 + i * 0.1)
    con.commit()
    return out


class TestAutoPeriod(unittest.TestCase):
    def test_月初自动跑30天档且幂等(self):
        con = _mkcon()
        _seed_days(con, 6)
        with mock.patch("pipeline.notifier.push",
                        return_value={"sent": False}) as mp:
            rep = build_mod._maybe_period(con, "2026-11-02")
            self.assertIsNotNone(rep, "11 月首个复盘日必须自动跑 30 天档")
            self.assertEqual(rep["days"], 30)
            self.assertEqual(mp.call_count, 1)
            # 同月再来一次 → 幂等不重跑
            self.assertIsNone(build_mod._maybe_period(con, "2026-11-03"))
            self.assertEqual(mp.call_count, 1)

    def test_月中15天档与窗口外(self):
        con = _mkcon()
        _seed_days(con, 6)
        with mock.patch("pipeline.notifier.push",
                        return_value={"sent": False}):
            rep = build_mod._maybe_period(con, "2026-11-16")
            self.assertIsNotNone(rep)
            self.assertEqual(rep["days"], 15)
            self.assertIsNone(build_mod._maybe_period(con, "2026-11-20"),
                              "窗口外不得触发")

    def test_窗口内跨日只跑一次(self):
        # 11-01 与 11-02 都落在「1-3 日」补跑窗口：第一次跑、第二次不再跑
        con = _mkcon()
        _seed_days(con, 6)
        with mock.patch("pipeline.notifier.push",
                        return_value={"sent": False}):
            self.assertIsNotNone(build_mod._maybe_period(con, "2026-11-01"))
            self.assertIsNone(build_mod._maybe_period(con, "2026-11-02"),
                              "同月同档只允许跑一次")


class TestHoldingsCurve(unittest.TestCase):
    def test_缺股数_等权均盈亏(self):
        con = _mkcon()
        days = _seed_days(con, 6)
        hold = [{"code": "sz300192", "buy_price": 10.0, "shares": None}]
        with mock.patch.object(build_mod, "load_holdings", return_value=hold):
            curve = build_mod._holdings_curve(con, days[-1])
        self.assertEqual(len(curve), 6)
        self.assertIsNotNone(curve[-1][1])
        self.assertAlmostEqual(curve[-1][1], (10.5 / 10.0 - 1) * 100, places=2,
                               msg="末日 = 现价相对成本的等权盈亏%")

    def test_有股数_市值加权(self):
        con = _mkcon()
        days = _seed_days(con, 6)
        hold = [{"code": "sz300192", "buy_price": 10.0, "shares": 100}]
        with mock.patch.object(build_mod, "load_holdings", return_value=hold):
            curve = build_mod._holdings_curve(con, days[-1])
        self.assertAlmostEqual(curve[-1][1], (100 * 10.5 / (100 * 10) - 1)
                               * 100, places=2)

    def test_无持仓_空表(self):
        con = _mkcon()
        days = _seed_days(con, 3)
        with mock.patch.object(build_mod, "load_holdings", return_value=[]):
            self.assertEqual(build_mod._holdings_curve(con, days[-1]), [])

    def test_曲线从最早买入日起_买入前不画(self):
        con = _mkcon()
        days = _seed_days(con, 6)
        hold = [{"code": "sz300192", "buy_price": 10.0, "shares": None,
                 "buy_date": days[3]}]           # 第 4 个交易日才买入
        with mock.patch.object(build_mod, "load_holdings", return_value=hold):
            curve = build_mod._holdings_curve(con, days[-1])
        self.assertEqual([p[0] for p in curve], days[3:],
                         "买入日之前的点不得出现在收益曲线里")

    def test_非buy角色密文剥除(self):
        class _U:
            uid = "guest"
            name = "访客"
            roles = ["observe"]
            is_owner = False
            password = "x"
        data = {"holdings_curve": [["2026-10-30", 2.5]],
                "holdings_detail": [{"code": "sz300192"}]}
        out = publish.apply_roles(data, _U())
        self.assertNotIn("holdings_curve", out,
                         "observe 角色的密文里不得残留持仓曲线")
        self.assertNotIn("holdings_detail", out)


class TestBreakoutTrack(unittest.TestCase):
    """F4 双轨买区：回踩轨之外新增「突破确认价」轨（回应周检买漏了）。"""

    def test_breakout_ref计算(self):
        from pipeline import engines
        rows = [[f"2026-10-0{i}", 10.0, 10.2, 10.25, 9.9, 1e6]
                for i in range(1, 6)]
        bo = engines.breakout_ref(rows)
        self.assertAlmostEqual(bo, round(10.25 * 1.005, 2))
        self.assertIsNone(engines.breakout_ref(rows[:2]), "bar 不足返回 None")

    def _mk_exec_con(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.executescript(core._SCHEMA)
        return con

    def test_突破确认_半仓买入(self):
        con = self._mk_exec_con()
        for d, c in (("2026-10-07", 10.0), ("2026-10-08", 10.05)):
            con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("sz000001", d, c, c * 1.02, c * 0.99, c,
                         1e6, 3e7, 0.0, 1.0))
        # 当日价 10.4（对昨收 10.2 = +2%？→ 需 3-7%：昨收 10.0、今价 10.4 = +4%）
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("sz000001", "2026-10-09", 10.4, 10.4, 10.4, 10.4,
                     1e6, 3e7, 0.0, 1.0))
        con.execute("INSERT OR REPLACE INTO candidate_snapshots VALUES("
                    "?,?,?,?,?,?,?,?)",
                    ("2026-10-09", "sz000001", "甲", "趋势", 80, "现在买",
                     "", '{"breakout": 10.25, "buy_low": 9.5, "buy_high": 10.0}'))
        con.execute(
            "INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-09", "sz000001", "甲", "趋势", "现在买",
             9.5, 10.0, 9.0, None, None, 85.0, "", None))
        con.commit()
        from pipeline import executor
        now = datetime(2026, 10, 9, 9, 35, tzinfo=timezone(timedelta(hours=8)))
        log = executor.auto_open(con, "2026-10-09", slot="am", now=now)
        buys = [e for e in log if e[1] == "BUY"]
        self.assertEqual(len(buys), 1, f"突破确认价上方+4% 必须触发突破买：{log}")
        self.assertIn("突破半仓", buys[0][2])
        amt = con.execute(
            "SELECT qty*price FROM fills WHERE side='buy'").fetchone()[0]
        self.assertLessEqual(amt, 100000 * 0.15 * 1.05,
                             "突破轨 = 档位半仓（30%→15%）")
        # 同日第二只突破票 → 每日限 1 笔，不再买
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("sz000002", "2026-10-09", 5.4, 5.4, 5.4, 5.4,
                     1e6, 3e7, 0.0, 1.0))
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("sz000002", "2026-10-08", 5.0, 5.0, 5.0, 5.0,
                     1e6, 3e7, 0.0, 1.0))
        con.execute("INSERT OR REPLACE INTO candidate_snapshots VALUES("
                    "?,?,?,?,?,?,?,?)",
                    ("2026-10-09", "sz000002", "乙", "趋势", 80, "现在买",
                     "", '{"breakout": 5.05, "buy_low": 4.5, "buy_high": 4.9}'))
        con.execute(
            "INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-09", "sz000002", "乙", "趋势", "现在买",
             4.5, 4.9, 4.2, None, None, 84.0, "", None))
        con.commit()
        log2 = executor.auto_open(con, "2026-10-09", slot="pm", now=now)
        self.assertFalse([e for e in log2 if e[1] == "BUY"],
                         "突破买每日限 1 笔")

    def test_涨幅超带_不追(self):
        con = self._mk_exec_con()
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("sz000001", "2026-10-08", 10.0, 10.0, 10.0, 10.0,
                     1e6, 3e7, 0.0, 1.0))
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("sz000001", "2026-10-09", 11.4, 11.4, 11.4, 11.4,
                     1e6, 3e7, 0.0, 1.0))   # +14% 超出 3-7% 带
        con.execute("INSERT OR REPLACE INTO candidate_snapshots VALUES("
                    "?,?,?,?,?,?,?,?)",
                    ("2026-10-09", "sz000001", "甲", "趋势", 80, "现在买",
                     "", '{"breakout": 10.25, "buy_low": 9.5, "buy_high": 10.0}'))
        con.execute(
            "INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-09", "sz000001", "甲", "趋势", "现在买",
             9.5, 10.0, 9.0, None, None, 85.0, "", None))
        con.commit()
        from pipeline import executor
        now = datetime(2026, 10, 9, 9, 35, tzinfo=timezone(timedelta(hours=8)))
        log = executor.auto_open(con, "2026-10-09", slot="am", now=now)
        self.assertFalse([e for e in log if e[1] == "BUY"],
                         "涨幅超带（涨停附近/已板）不得突破追入")


class TestRelevanceSort(unittest.TestCase):
    def test_自选最先_同板块次之_其余按分(self):
        con = _mkcon()
        con.executemany("INSERT OR REPLACE INTO stock_industry VALUES(?,?,?)",
                        [("300192", "医药", "2026-10-01"),
                         ("600701", "医药", "2026-10-01"),
                         ("600000", "银行", "2026-10-01"),
                         ("000002", "地产", "2026-10-01")])
        con.commit()
        plans = [("sz000002", "地产票", "现在买", 1, 2, None, 90),
                 ("sh600701", "同板块", "现在买", 1, 2, None, 80),
                 ("sz300192", "同板块低分", "等回踩", 1, 2, None, 60),
                 ("sz000001", "无关高分", "现在买", 1, 2, None, 95)]
        out = intraday.relevance_sort(
            con, list(plans), watch_set={"sh600000"}, held_secs={"医药"})
        order = [p[0] for p in out]
        self.assertEqual(order[0], "sh600000" if "sh600000" in order
                         else order[0])
        # 自选不在 plans 里时：同板块两票排前，板块内按分；无关按分垫底
        self.assertEqual(order, ["sh600701", "sz300192", "sz000001",
                                 "sz000002"],
                         "相关度(自选>同板块>其余)必须压过原始分数")

    def test_自选票压过一切(self):
        con = _mkcon()
        con.execute("INSERT OR REPLACE INTO stock_industry VALUES(?,?,?)",
                    ("600000", "银行", "2026-10-01"))
        con.commit()
        plans = [("sz000001", "高分无关", "现在买", 1, 2, None, 99),
                 ("sh600000", "自选低分", "等回踩", 1, 2, None, 50)]
        out = intraday.relevance_sort(
            con, list(plans), watch_set={"sh600000"}, held_secs=set())
        self.assertEqual(out[0][0], "sh600000", "自选票必须排最前")

    def test_缺行业数据_不炸不前插(self):
        con = _mkcon()
        plans = [("sz000001", "甲", "现在买", 1, 2, None, 80),
                 ("sz000002", "乙", "现在买", 1, 2, None, 90)]
        out = intraday.relevance_sort(con, list(plans), watch_set=set(),
                                      held_secs=set())
        self.assertEqual([p[0] for p in out], ["sz000002", "sz000001"],
                         "无相关度信息时退化为 分数→代码 全序")


if __name__ == "__main__":
    unittest.main()
