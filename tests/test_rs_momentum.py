# -*- coding: utf-8 -*-
"""RS 超额动量因子回归（2026-09-19 融入经典横截面动量）：
数学口径 / 终审排序效果 / 老调用方行为不变。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pipeline import engines, scoring  # noqa: E402


def _stock_rows(closes):
    return [[f"d{i}", c * 0.995, c, c * 1.005, c * 0.99, 1e6]
            for i, c in enumerate(closes)]


class TestRsMomentum(unittest.TestCase):
    def test_math(self):
        # 个股 20 日 +10%，指数 20 日 +2% → RS ≈ +8%（跑赢）
        n = 21
        stock = _stock_rows([10.0 * (1.10) ** (i / (n - 1)) for i in range(n)])
        index = _stock_rows([3000.0 * (1.02) ** (i / (n - 1)) for i in range(n)])
        rs = engines.rs_momentum(stock, index)
        self.assertIsNotNone(rs)
        self.assertGreater(rs, engines.RS_STRONG,
                           "跑赢大盘 8 个点应判强相对动量")
        # 反向：个股 -5%，指数 +2% → RS ≈ -7%
        stock2 = _stock_rows([10.0 * (0.95) ** (i / (n - 1)) for i in range(n)])
        rs2 = engines.rs_momentum(stock2, index)
        self.assertLess(rs2, engines.RS_WEAK)

    def test_insufficient_none(self):
        stock = _stock_rows([10.0] * 10)
        index = _stock_rows([3000.0] * 30)
        self.assertIsNone(engines.rs_momentum(stock, index))
        self.assertIsNone(engines.rs_momentum([], index))


class TestRsFactorInPicks(unittest.TestCase):
    def _cand(self, code, base, rs_mom=None):
        return {"code": code, "name": code, "pool": "趋势", "close": 10.0,
                "buy_low": 9.9, "buy_high": 10.05, "sell_low": 11.0,
                "sell_high": 11.5, "stop": 9.4, "tag": "趋势",
                "worth_score": base, "action": "现在买", "rs_mom": rs_mom}

    def test_rs_breaks_tie(self):
        env = {"趋势": 1.0}
        cands = [self._cand("600001", 70, rs_mom=8.0),   # 跑赢大盘 → ×1.05
                 self._cand("600002", 70, rs_mom=-8.0)]  # 跑输大盘 → ×0.95
        picks = scoring.compute_top_picks(cands, env, {})
        self.assertEqual(picks[0]["code"], "600001",
                         "同基础分，跑赢大盘者优先（横截面动量）")
        self.assertAlmostEqual(picks[0]["eff_score"] / picks[1]["eff_score"],
                               1.05 / 0.95, places=3)

    def test_neutral_zone_no_effect(self):
        env = {"趋势": 1.0}
        a = self._cand("600001", 70, rs_mom=2.0)     # ±5% 之内 → 不动
        b = self._cand("600002", 70, rs_mom=None)    # 缺数据 → 中性
        picks = scoring.compute_top_picks([a, b], env, {})
        self.assertAlmostEqual(picks[0]["eff_score"], picks[1]["eff_score"],
                               places=6)

    def test_legacy_no_field_unchanged(self):
        env = {"趋势": 1.0}
        cands = [self._cand("600001", 80), self._cand("600002", 60)]
        picks = scoring.compute_top_picks(cands, env, {})
        self.assertEqual([p["code"] for p in picks], ["600001", "600002"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
