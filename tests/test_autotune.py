# -*- coding: utf-8 -*-
"""周度自修正 + 每次推送自带持仓动态 回归锁（2026-10-05 用户需求）。

① autotune：模拟盘平仓证据 → 选股加权系数自动修正（有界/有据/留痕）。
   锁死：样本 <3 笔不动参数；k_hot 追高搅肉→压热度；k_first 首推连亏→
   加深折价；周五/距上次≥7天触发；系数只落在选股加权，永不碰止损/裁决。
② holdings_status_lines：每次主推送自动带持仓动态行（用户「不要让我
   随时问，每次更新即可」）。
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
import pipeline.autotune as autotune      # noqa: E402
import pipeline.scoring as scoring        # noqa: E402
import pipeline.build as build_mod        # noqa: E402


def _mkcon():
    con = sqlite3.connect(":memory:")
    con.executescript(core._SCHEMA)
    return con


def _fill(con, ts, code, side, price, reason=""):
    oid = f"o{ts.replace('-', '')}{side}{code[-3:]}"
    con.execute("INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?,?,?,?)",
                (oid, ts, code, side, 1000, price, "filled", reason))
    con.execute("INSERT INTO fills VALUES(NULL,?,?,?,?,?,?,?)",
                (oid, ts, code, side, 1000, price, 0.0))
    con.commit()
    return oid


def _seed_trade(con, code, buy_d, sell_d, bp, sp, reason="普通硬止损",
                verdict="轻仓试探"):
    con.execute("INSERT OR REPLACE INTO day_meta VALUES(?,?,?,?,?,?)",
                (buy_d, verdict, "t", 50.0, "均衡", buy_d + "T09:00:00"))
    _fill(con, f"{buy_d}T09:26:00", code, "buy", bp)
    _fill(con, f"{sell_d}T14:40:00", code, "sell", sp, reason)


class TestAutotune(unittest.TestCase):
    def setUp(self):
        scoring.TUNE.update({"k_first": 1.0, "k_hot": 1.0})

    def tearDown(self):
        scoring.TUNE.update({"k_first": 1.0, "k_hot": 1.0})

    def test_追高搅肉_压热度因子(self):
        con = _mkcon()
        _seed_trade(con, "sz000001", "2026-09-28", "2026-09-29", 10.0, 9.4)
        _seed_trade(con, "sz000002", "2026-09-29", "2026-09-30", 20.0, 19.4)
        _seed_trade(con, "sh600000", "2026-09-28", "2026-09-30", 10.0, 10.5,
                    reason="持仓浮盈止盈")
        rep = autotune.tune(con, "2026-10-02", dry=True)
        self.assertGreaterEqual(rep["n"], 3)
        self.assertGreaterEqual(rep["churn_n"], 2)
        self.assertTrue(rep["changes"],
                        "2/3 隔日止损必须触发 k_hot 下调")
        k_change = next(c for c in rep["changes"] if c[0] == "k_hot")
        self.assertAlmostEqual(k_change[1], 0.80)
        self.assertIn("k_hot", rep["summary"])

    def test_首推连亏_加深折价(self):
        con = _mkcon()
        _seed_trade(con, "sz000001", "2026-09-28", "2026-09-29", 10.0, 9.5)
        _seed_trade(con, "sz000002", "2026-09-29", "2026-09-30", 20.0, 18.8)
        _seed_trade(con, "sh600000", "2026-09-28", "2026-09-29", 10.0, 9.6)
        rep = autotune.tune(con, "2026-10-02", dry=True)
        k_change = next((c for c in rep["changes"] if c[0] == "k_first"), None)
        self.assertIsNotNone(k_change, "首推3笔均亏超2%必须触发 k_first 下调")
        self.assertAlmostEqual(k_change[1], 0.85)

    def test_样本不足_不动参数(self):
        con = _mkcon()
        _seed_trade(con, "sz000001", "2026-09-28", "2026-09-29", 10.0, 9.0)
        rep = autotune.tune(con, "2026-10-02", dry=True)
        self.assertEqual(rep["changes"], [])
        self.assertIn("样本不足", rep["summary"])

    def test_系数有界(self):
        con = _mkcon()
        con.execute("INSERT OR REPLACE INTO tune_state VALUES(?,?,?,?)",
                    ("k_hot", 1.35, "t", "2026-09-20T09:00:00"))
        _seed_trade(con, "sz000001", "2026-09-28", "2026-09-29", 10.0, 9.4)
        _seed_trade(con, "sz000002", "2026-09-29", "2026-09-30", 20.0, 19.4)
        _seed_trade(con, "sh600000", "2026-09-28", "2026-09-30", 10.0, 10.5,
                    reason="持仓浮盈止盈")
        rep = autotune.tune(con, "2026-10-02", dry=True)
        for k, v, _why in rep["changes"]:
            lo, hi = autotune.KNOBS[k]["lo"], autotune.KNOBS[k]["hi"]
            self.assertGreaterEqual(v, lo)
            self.assertLessEqual(v, hi)

    def test_写库留痕(self):
        con = _mkcon()
        _seed_trade(con, "sz000001", "2026-09-28", "2026-09-29", 10.0, 9.4)
        _seed_trade(con, "sz000002", "2026-09-29", "2026-09-30", 20.0, 19.4)
        _seed_trade(con, "sh600000", "2026-09-28", "2026-09-30", 10.0, 10.5,
                    reason="持仓浮盈止盈")
        rep = autotune.tune(con, "2026-10-02")
        self.assertTrue(rep["changes"])
        row = con.execute(
            "SELECT value, reason FROM tune_state WHERE key='k_hot'"
        ).fetchone()
        self.assertIsNotNone(row, "调整必须落 tune_state 留痕")
        self.assertAlmostEqual(row[0], 0.80)

    def test_due_周五或超7天(self):
        con = _mkcon()
        self.assertTrue(autotune.due(con, "2026-10-09"))     # 周五
        self.assertFalse(autotune.due(con, "2026-10-05"))    # 周一且从未调
        con.execute("INSERT OR REPLACE INTO tune_state VALUES(?,?,?,?)",
                    ("k_hot", 0.8, "t", "2026-09-29T20:00:00"))
        self.assertFalse(autotune.due(con, "2026-10-05"))    # 距上次6天
        self.assertTrue(autotune.due(con, "2026-10-08"))     # 距上次9天

    def test_评分器吃到系数(self):
        base = {"code": "sz000001", "name": "甲", "pool": "波段",
                "close": 20.0, "worth": 70.0, "action": "现在买",
                "buy_low": 19.5, "buy_high": 20.5, "score": 70.0,
                "confirms": 2, "rs_mom": 10.0}
        scoring.TUNE.update({"k_first": 1.0, "k_hot": 1.0})
        out_hot = scoring.compute_top_picks(
            [dict(base)], {"波段": 1.0}, {}, limit=None, per_sector=0,
            ladder_cap=99)[0]["eff_score"]
        scoring.TUNE.update({"k_first": 1.0, "k_hot": 0.6})
        out_cold = scoring.compute_top_picks(
            [dict(base)], {"波段": 1.0}, {}, limit=None, per_sector=0,
            ladder_cap=99)[0]["eff_score"]
        self.assertGreater(out_hot, out_cold,
                           "k_hot<1 必须压低热度/RS 加成后的有效分")
        self.assertAlmostEqual(out_hot / out_cold, 1.05 / 1.03, places=2)
        scoring.TUNE.update({"k_first": 0.85, "k_hot": 1.0})
        _c0 = dict(base)
        _c0["confirms"] = 0                      # 首推票：确认<2
        first = scoring.compute_top_picks(
            [_c0], {"波段": 1.0}, {}, limit=None, per_sector=0,
            ladder_cap=99)[0]["eff_score"]
        # 波段池 WINRATE_ANCHOR 口径：70 × anchor × RS1.05 × k_first0.85
        _anchor = scoring.WINRATE_ANCHOR.get("波段", 0.72)
        self.assertAlmostEqual(
            first, round(70 * _anchor * 1.05 * 0.85, 2),
            msg="k_first 只作用于确认<2 的票")

    def test_load_into_scoring(self):
        con = _mkcon()
        con.execute("INSERT OR REPLACE INTO tune_state VALUES(?,?,?,?)",
                    ("k_first", 0.85, "t", "2026-09-28T20:00:00"))
        autotune.load_into_scoring(con)
        self.assertAlmostEqual(scoring.TUNE["k_first"], 0.85)
        scoring.TUNE["k_first"] = 1.0

    def test_全部历史入样_超7天的旧交易也算证据(self):
        # 用户原话：「运行了一个多月，那么多历史都可以参考借鉴，
        # 样本不足是伪命题」——学习窗口必须是全部历史。
        con = _mkcon()
        _seed_trade(con, "sz000001", "2026-09-07", "2026-09-08", 10.0, 9.4)
        _seed_trade(con, "sz000002", "2026-09-09", "2026-09-10", 20.0, 19.4)
        _seed_trade(con, "sh600000", "2026-09-10", "2026-09-15", 10.0, 10.5,
                    reason="持仓浮盈止盈")
        rep = autotune.tune(con, "2026-10-05", dry=True)
        self.assertEqual(rep["n"], 3, "一个月前的平仓必须入样")

    def test_假期幻影成交_剔除不计(self):
        # 复刻 10-01 真实事故形态：09-30 买入、假期用旧价幻影卖出（已冲正）。
        con = _mkcon()
        _seed_trade(con, "sz000001", "2026-09-28", "2026-09-29", 10.0, 9.4)
        _seed_trade(con, "sh600000", "2026-09-28", "2026-09-30", 10.0, 10.5,
                    reason="持仓浮盈止盈")
        _fill(con, "2026-09-30T09:26:00", "sz000002", "buy", 20.0)
        _fill(con, "2026-10-01T09:25:30", "sz000002", "sell", 19.0,
              "普通硬止损")
        fid = con.execute("SELECT MAX(fill_id) FROM fills").fetchone()[0]
        con.execute("INSERT INTO offday_reverted VALUES(?)", (fid,))
        con.commit()
        rep = autotune.tune(con, "2026-10-09", dry=True)
        self.assertEqual(rep["n"], 2, "冲正标记的幻影卖出不得算成交易")


class TestHoldingsStatusLines(unittest.TestCase):
    def test_有持仓_每次推送自带动态行(self):
        con = _mkcon()
        import datetime as _dt
        d = _dt.date(2026, 9, 1)
        closes = []
        c = 24.0
        for i in range(24):
            while d.weekday() >= 5:
                d += _dt.timedelta(days=1)
            c *= 1 + (0.4 if i % 4 else -0.15) / 100
            con.execute(
                "INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("sz300192", d.isoformat(), c, c * 1.01, c * 0.99, c,
                 1e6, 3e7, 0.0, 1.0))
            closes.append(c)
            d += _dt.timedelta(days=1)
        con.commit()
        hold = [{"code": "sz300192", "name": "科德教育",
                 "buy_price": round(closes[0], 2),
                 "buy_date": "2026-09-01", "shares": 800}]
        with mock.patch.object(build_mod, "load_holdings", return_value=hold):
            lines = build_mod.holdings_status_lines(con, "2026-09-30")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("📦科德教育"))
        self.assertIn("→", lines[0])

    def test_无持仓_空列表不占位(self):
        con = _mkcon()
        with mock.patch.object(build_mod, "load_holdings", return_value=[]):
            self.assertEqual(
                build_mod.holdings_status_lines(con, "2026-09-30"), [])


if __name__ == "__main__":
    unittest.main()
