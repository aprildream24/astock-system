# -*- coding: utf-8 -*-
"""引擎边界测试：合成 K 线 → 1 只放行 / 1 只拦截 + 源码静态断言。"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import engines  # noqa: E402


def gen_box_rows(box_low=10.0, box_high=11.5, days=40, end_at_low=True):
    """合成箱体：在 [box_low, box_high] 内震荡，touch/top 证据充分。"""
    import math
    rows = []
    base = 100.0
    for i in range(days):
        c = box_low + (box_high - box_low) * (0.5 + 0.4 * math.sin(i))
        o = c * 1.005
        h = max(o, c) * 1.008
        l = min(o, c) * 0.992
        if i >= days - 3 and end_at_low:
            c, o, h, l = box_low * 1.02, box_low * 1.025, box_low * 1.03, box_low * 0.995
        rows.append([f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", o, c, h, l, 1e6])
    # 前段抬高避免 MA20 斜率闸误伤：整体起点更低
    shift = [box_low * 0.97] * days
    for i, r in enumerate(rows):
        r[2] += shift[i] * 0.0
    return rows


def gen_uptrend_rows(days=40, daily=2.5, flat=False):
    rows = []
    c = 10.0
    for i in range(days):
        pct = daily if i % 5 != 4 else -0.5
        if flat:
            pct = 0.3
        c *= 1 + pct / 100
        rows.append([f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
                     c * 0.995, c, c * 1.008, c * 0.992, 1e6])
    return rows


def gen_fastbox_rows(pull_ratio=0.15):
    """快箱体合成：15 日窗显式构造「爬升→单日8%大阳→见顶→回踩→回升」。
    pull_ratio = 最深回踩/净涨幅：<0.2 → 快箱体；0.2~0.5 且回升 → N字二波。"""
    rows = []
    c = 10.0
    for i in range(60):
        rows.append([f"d{i}", c * 0.995, c, c * 1.01, c * 0.99, 1e6])
        c *= 1.001
    S = c                                   # 窗起点
    peak = S * 1.30                         # 窗内最高（净涨约 30%）
    pull_pct = pull_ratio * 15.0            # 回踩幅度（峰高的百分比）
    dip = peak * (1 - pull_pct / 100)       # 最深回踩位
    last = max(dip * (1 + 0.01), peak * 0.995 if pull_ratio <= 0.2
               else dip * (1 + 0.01))       # 现价：快箱体高位横住 / 二波回升
    n_pre, n_post = 7, 7                    # 峰前 7 日爬升、峰后 7 日（含谷）
    pre = [S + (peak - S) * i / n_pre for i in range(n_pre)]
    if n_pre > 0:
        pre[3] = pre[2] * 1.08              # 单日 8% 大阳（FAST_SURGE）
    post = [dip] + [dip + (last - dip) * i / 6 for i in range(1, 7)]
    for v in pre + [peak] + post:
        rows.append(["x", v * 0.995, v, max(peak, v) * 1.005, v * 0.99, 1e6])
    return rows


class TestUptrend(unittest.TestCase):
    def test_one_pass_one_block(self):
        good = engines.screen_uptrend(gen_uptrend_rows())
        self.assertIsNotNone(good, "标准多头排列应放行")
        self.assertGreater(good["worth_score"], 40)
        bad = engines.screen_uptrend(gen_uptrend_rows(daily=0.3))
        self.assertIsNone(bad, "日均 0.3% 应拦截")

    def test_flat_blocked(self):
        self.assertIsNone(engines.screen_uptrend(gen_uptrend_rows(flat=True)),
                          "横盘死水应拦截")


class TestBandTrade(unittest.TestCase):
    def test_box_detect_and_break(self):
        good = engines.detect_stage_bottom(gen_box_rows())
        self.assertIsNotNone(good, "标准箱体应检出")
        self.assertGreaterEqual(good["touches"], 3)
        self.assertGreaterEqual(good["tops"], 3)
        broken = engines.detect_stage_bottom(
            gen_box_rows(end_at_low=False) and _break_rows())
        self.assertIsNone(broken, "跌破箱底 1% 应剔除（不接飞刀）")

    def test_tight_box_blocked(self):
        r = gen_box_rows(box_low=10.0, box_high=10.3)  # 宽度 <5%
        self.assertIsNone(engines.detect_stage_bottom(r), "过窄箱体应拦截")


def _break_rows():
    rows = gen_box_rows()
    close = rows[-1][2]
    box_low = 10.0
    rows[-1][2] = box_low * 0.95
    rows[-1][4] = box_low * 0.94
    return rows


class TestFastBox(unittest.TestCase):
    def test_fast_vs_nwave(self):
        fast = engines.classify_box_speed(gen_fastbox_rows(pull_ratio=0.15))
        self.assertEqual(fast["speed"], "快箱体")
        self.assertEqual(fast["hold_days"], 8)
        n = engines.classify_box_speed(gen_fastbox_rows(pull_ratio=0.4))
        self.assertEqual(n["speed"], "N字二波")
        self.assertEqual(n["hold_days"], 12)
        slow = engines.classify_box_speed(
            [r for r in gen_fastbox_rows(pull_ratio=0.15)][:60] +
            [[f"f{i}", 10.0, 10.0, 10.01, 9.99, 1e6] for i in range(15)])
        self.assertEqual(slow["speed"], "常规")

    def test_deepest_pullback_not_current(self):
        # 源码静态断言：最深回踩必须用 min(c15[hi_idx:])，不是当前回撤
        src = open(engines.__file__, encoding="utf-8").read()
        self.assertIn("min(c15[hi_idx:])", src,
                      "回撤口径被改动：必须用主升后最深回踩")


class TestLadderPlan(unittest.TestCase):
    def test_gap_discipline(self):
        follow, watch = engines.auction_discipline(2, 3.0)
        self.assertFalse(follow and not watch, "st=2 弱高开 3% 必须观望（胜率14.3%）")
        follow, _ = engines.auction_discipline(2, 5.5)
        self.assertTrue(follow, "st=2 强高开 ≥5% 跟进")
        follow, _ = engines.auction_discipline(3, 2.5)
        self.assertTrue(follow, "st≥3 高开≥2% 跟进")
        follow, _ = engines.auction_discipline(1, -2.5)
        self.assertFalse(follow, "低开 ≤-2% 放弃")

    def test_spacing_guard(self):
        plan = engines.ladderplan_plan(1, 100.0)
        self.assertGreaterEqual(plan["t1"], plan["buy_high"] * 1.06,
                                "间距守卫：目标下沿 ≥ 买区上沿×1.06")
        self.assertAlmostEqual(plan["stop"], 92.0)


class TestZones(unittest.TestCase):
    def test_four_states(self):
        rows = gen_uptrend_rows()
        plan = engines.entry_plan(rows)
        self.assertIn(plan["state"], ("可买", "微超", "等回踩", "过热", "已破位"))
        close = rows[-1][2]
        if close <= plan["ref"] * 1.03:
            self.assertEqual(plan["action"], "现在买")


class TestKronos(unittest.TestCase):
    def test_score_range(self):
        s = engines.kronos_lite(gen_uptrend_rows())
        self.assertTrue(0 <= s <= 100)
        s2 = engines.kronos_lite(gen_box_rows())
        self.assertTrue(0 <= s2 <= 100)


if __name__ == "__main__":
    unittest.main(verbosity=1)
