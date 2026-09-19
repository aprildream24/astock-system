# -*- coding: utf-8 -*-
"""持仓全生命周期 + 板块退潮 回归（2026-09-21 用户需求）：
「买入后持有几天、弱了及时提醒卖出、考虑板块更换周期（不要才进去就暴跌）」。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pipeline import executor, notifier, sector  # noqa: E402
from pipeline.core import get_conn  # noqa: E402

DATES = [(__import__("datetime").date(2026, 9, 18)
          - __import__("datetime").timedelta(days=59 - i)).isoformat()
         for i in range(60)]


def _mk_sector_con(pcts_by_day):
    """pcts_by_day: {date: pct} —— 指定板块的每日涨幅序列。"""
    con = get_conn(":memory:")
    for d, p in pcts_by_day.items():
        con.execute("INSERT OR REPLACE INTO sector_heat VALUES(?,?,?,?,?,?)",
                    (d, "炼化及贸易", p, 5.0, 10, 20))
        con.execute("INSERT OR REPLACE INTO sector_heat VALUES(?,?,?,?,?,?)",
                    (d, "半导体", 1.0, 12.0, 20, 10))
    con.commit()
    return con


class TestSectorRetreat(unittest.TestCase):
    def test_cum3_decline(self):
        con = _mk_sector_con({"2026-09-16": 1.0, "2026-09-17": -2.0,
                              "2026-09-18": -2.5})
        r = sector.retreat_signal(con, "2026-09-18", "炼化及贸易")
        self.assertTrue(r["retreat"], "3 日累计 -3.5% → 退潮")
        self.assertIn("资金撤离", r["detail"])

    def test_two_day_breakdown(self):
        con = _mk_sector_con({"2026-09-16": 2.0, "2026-09-17": -1.2,
                              "2026-09-18": -1.5})
        r = sector.retreat_signal(con, "2026-09-18", "炼化及贸易")
        self.assertTrue(r["retreat"], "连跌 2 日且最新 -1.5% → 下台阶")

    def test_healthy_not_retreat(self):
        con = _mk_sector_con({"2026-09-16": 1.0, "2026-09-17": 0.5,
                              "2026-09-18": -0.5})
        r = sector.retreat_signal(con, "2026-09-18", "炼化及贸易")
        self.assertFalse(r["retreat"])

    def test_insufficient_data_none(self):
        con = _mk_sector_con({"2026-09-18": -5.0})
        self.assertIsNone(sector.retreat_signal(con, "2026-09-18", "炼化及贸易"),
                          "历史不足 2 日 → 不判（防误杀）")

    def test_missing_sector_none(self):
        con = _mk_sector_con({"2026-09-18": -5.0})
        self.assertIsNone(sector.retreat_signal(con, "2026-09-18", "不存在的板块"))


class TestLifecycle(unittest.TestCase):
    def _mk(self):
        """仅生成**工作日** K 线（周末本就没有行情），60 个交易日。"""
        import datetime as dt
        con = get_conn(":memory:")
        put = lambda d, code, o, h, l, c: con.execute(  # noqa: E731
            "INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
            (code, d, o, h, l, c, 1e6, None, None, None))
        days = []
        d0 = dt.date(2026, 6, 22)
        d = d0
        while len(days) < 60:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d += dt.timedelta(days=1)
        for x, d in enumerate(days):
            put(d, "sh000001", 3000, 3010, 2995, 3005)
        c = 20.0
        for x, d in enumerate(days):
            c *= 1 + (-0.55 if x % 5 == 4 else -0.1) / 100
            put(d, "sz002493", c * 0.995, c * 1.004, c * 0.99, c)
        # 买入日附近（days[40]±4）的推荐携带 hold_days=8（快箱体）
        extra = json.dumps({"hold_days": 8})
        for d in days[39:43]:
            con.execute(
                "INSERT OR REPLACE INTO rec_picks "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d, "sz002493", "荣盛石化", "波段", "现在买",
                 1, 2, 1, 2, 2, 80, "", None))
            con.execute(
                "INSERT OR REPLACE INTO candidate_snapshots "
                "VALUES(?,?,?,?,?,?,?,?)",
                (d, "sz002493", "荣盛石化", "波段", 80, "现在买", "{}", extra))
        con.commit()
        return con, days

    def test_hold_limit_from_rec(self):
        con, days = self._mk()
        limit = executor.hold_limit_for(con, "sz002493", days[40])
        self.assertEqual(limit, 8, "推荐携带的快箱体周期应被采用")
        limit2 = executor.hold_limit_for(con, "sz002493", days[0])
        self.assertEqual(limit2, 20, "找不到推荐 → 默认 20 日")

    def test_lifecycle_phases(self):
        con, days = self._mk()
        buy = days[40]
        holdings = [{"code": "sz002493", "name": "荣盛石化",
                     "buy_price": 20.0, "shares": 1000, "buy_date": buy}]
        heval = executor.evaluate_real_holdings(con, days[44], holdings)
        h = heval[0]
        self.assertEqual(h.get("hold_days"), 4)
        self.assertEqual(h.get("hold_limit"), 8)
        self.assertEqual(h.get("phase"), "持有中")
        heval2 = executor.evaluate_real_holdings(con, days[48], holdings)
        h2 = heval2[0]
        self.assertEqual(h2.get("hold_days"), 8)
        self.assertEqual(h2.get("phase"), "已到期")
        self.assertTrue(any("持有周期已到" in str(r)
                            for r in h2.get("exit_reasons", [])),
                        "到期必须明确提示按纪律了结")

    def test_near_expiry_warning(self):
        con, days = self._mk()
        buy = days[40]
        holdings = [{"code": "sz002493", "name": "荣盛石化",
                     "buy_price": 20.0, "shares": 1000, "buy_date": buy}]
        # 第 7 个交易日：8 × 0.8 = 6.4 → 7 触发「接近到期」
        heval = executor.evaluate_real_holdings(con, days[47], holdings)
        h = heval[0]
        self.assertEqual(h.get("phase"), "接近到期")
        self.assertTrue(any("接近持有周期上限" in str(r)
                            for r in h.get("exit_reasons", [])))


class TestRenderLifecycle(unittest.TestCase):
    def test_rows_render_lifecycle_and_retreat(self):
        h = {"code": "sz002493", "name": "荣盛石化", "close": 12.85,
             "pnl_pct": -1.61, "verdict": "持有观察", "phase": "已到期",
             "hold_days": 8, "hold_limit": 8,
             "sector_retreat": "近3日累计 -3.5%，资金撤离",
             "exit_reasons": []}
        html = notifier.render_holding_advice([h], [], "2026-09-18")
        self.assertIn("⏰ 持有 8/8 个交易日（已到期）", html)
        self.assertIn("板块退潮：近3日累计", html)
        # 无字段的老数据不渲染这些行
        html2 = notifier.render_holding_advice(
            [{"code": "x", "name": "x", "close": 1, "verdict": "持有观察"}],
            [], "d")
        self.assertNotIn("⏰ 持有", html2)


class TestBuildVetoWiring(unittest.TestCase):
    def test_build_has_retreat_veto(self):
        src = open(os.path.join(ROOT, "pipeline", "build.py"),
                   encoding="utf-8").read()
        self.assertIn("retreat_signal(con, date, sec)", src)
        self.assertIn("板块退潮不接刀", src)
        # 否决必须发生在推荐配额之前（防"先选出再否决"浪费配额）
        self.assertLess(src.find("板块退潮否决"), src.find("行情档位 → 推荐配额"))

    def test_pre_auction_actionable_includes_lifecycle(self):
        src = open(os.path.join(ROOT, "pipeline", "build.py"),
                   encoding="utf-8").read()
        seg = src[src.find('if task in ("pre", "auction"):'):]
        self.assertIn('x.get("phase") in ("已到期", "接近到期")', seg)
        self.assertIn('x.get("sector_retreat")', seg)


if __name__ == "__main__":
    unittest.main(verbosity=1)
