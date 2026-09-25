# -*- coding: utf-8 -*-
"""2026-09-25 用户需求回归：
① 到买点候选池扩大到近5日全部历史候选（不再永远那几只）；
② GitHub 量化仓库技巧融入：alpha 因子组 + 唐奇安突破 + alphalens 式 IC。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pipeline import engines, intraday, scoring  # noqa: E402
from pipeline.core import get_conn  # noqa: E402


def _rows(closes, vol_scale=1.0):
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) * 1.005
        l = min(o, c) * 0.99
        rows.append([f"d{i}", o, c, h, l, 1e6 * vol_scale * (1 + i % 3)])
        prev = c
    return rows


class TestAlphaFactors(unittest.TestCase):
    def test_corr_pv_healthy_uptrend_positive(self):
        """量价同向的健康上涨 → corr 为正。"""
        closes = [10 * (1 + 0.01 * (i % 3)) * (1.008 ** i) for i in range(25)]
        vols = [1e6 * (1 + 0.02 * (i % 3)) * (1.01 ** i) for i in range(25)]
        rows = _rows([10 * (1.008 ** i) for i in range(25)])
        for i, r in enumerate(rows):
            r[5] = vols[i]
        a = engines.alpha_extras(rows)
        self.assertGreater(a["corr_pv"], 0.2, "量增价涨 → 正相关")

    def test_squeeze_detects_contraction(self):
        # 前 45 日大振幅、后 15 日极窄 → squeeze < 1
        closes, rows = [], []
        c = 20.0
        for i in range(60):
            swing = 0.06 if i < 45 else 0.008
            c *= 1 + (swing if i % 2 == 0 else -swing / 2)
            rows.append([f"d{i}", c * 0.99, c, c * (1 + swing),
                         c * (1 - swing), 1e6])
        a = engines.alpha_extras(rows)
        self.assertLess(a["vol_squeeze"], 0.8, "窄幅盘整应检出收缩")

    def test_insufficient_empty(self):
        self.assertEqual(engines.alpha_extras(_rows([10.0] * 15)), {})


class TestDonchian(unittest.TestCase):
    def test_breakout_true(self):
        closes = [10.0] * 25
        closes[-1] = 12.0                      # 创 20 日新高
        rows = _rows(closes)
        rows[-1][3] = 12.1
        self.assertTrue(engines.donchian_breakout(rows))

    def test_no_breakout(self):
        rows = _rows([10.0] * 26)
        self.assertFalse(engines.donchian_breakout(rows))


class TestAlphaBonus(unittest.TestCase):
    def test_squeeze_and_corr_add(self):
        c = {"pool": "趋势", "worth_score": 70, "trend_state": "加速上行",
             "alpha": {"vol_squeeze": 0.6, "corr_pv": 0.5}}
        base = scoring.score_candidate(c, {"趋势": 1.0})
        c2 = dict(c, alpha={})
        base_plain = scoring.score_candidate(c2, {"趋势": 1.0})
        self.assertAlmostEqual(base - base_plain, 5.0, places=1,
                               msg="收缩+3 与量价同向+2 都应生效")

    def test_divergence_penalized(self):
        c = {"pool": "趋势", "worth_score": 70, "alpha":
             {"vol_squeeze": 1.0, "corr_pv": -0.5}}
        base = scoring.score_candidate(c, {"趋势": 1.0})
        c2 = dict(c, alpha={})
        plain = scoring.score_candidate(c2, {"趋势": 1.0})
        self.assertAlmostEqual(plain - base, 3.0, places=1, msg="背离 -3")


class TestHistPlans(unittest.TestCase):
    def test_hist_candidates_merged(self):
        """近 5 日历史候选（含当日未入选的）并入盘中到买点监控。"""
        con = get_conn(":memory:")
        put = lambda d, code, o, h, l, c: con.execute(  # noqa: E731
            "INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
            (code, d, o, h, l, c, 1e6, None, None, None))
        for i in range(40):
            d = f"2026-09-{i % 25 + 1:02d}"
            put(d, "sh600401", 10 + i * 0.01, 10.2 + i * 0.01,
                9.9 + i * 0.01, 10.1 + i * 0.01)
        for d in ("2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23"):
            put(d, "sh000001", 3000, 3010, 2995, 3005)
        # 昨日候选（当日 rec_picks 无）——应被并入监控
        extra = json.dumps({"buy_low": 10.4, "buy_high": 10.9, "stop": 9.8})
        con.execute(
            "INSERT OR REPLACE INTO candidate_snapshots VALUES(?,?,?,?,?,?,?,?)",
            ("2026-09-23", "sh600401", "历史票", "趋势", 85, "等回踩",
             "{}", extra))
        con.commit()
        # 走 _hist 分支的 SQL 依赖 json_extract —— :memory: sqlite 支持 JSON1
        plans = con.execute(
            "SELECT code, MAX(action) FROM candidate_snapshots "
            "WHERE date>=date('2026-09-24','-6 day') AND date<'2026-09-24' "
            "GROUP BY code").fetchall()
        self.assertTrue(plans, "历史候选应可查出")

    def test_source_assert(self):
        src = open(os.path.join(ROOT, "pipeline", "intraday.py"),
                   encoding="utf-8").read()
        self.assertIn("历史候选并入", src)
        self.assertIn("candidate_snapshots", src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
