# -*- coding: utf-8 -*-
"""2026-09-13 三项整改回归：
① 推送版面（表格化 / 无 float / 纯文本可降级）
② 推的票必须在购买区间（买区宽度闸门 + 四态优先 + 主推位在区内）
③ 扫描全部个股（宇宙=快照∪K线并集，剔除全留痕）
"""
import os
import re
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import build as bld, engines, notifier, scoring  # noqa: E402
from pipeline.core import get_conn  # noqa: E402

DATES = [(date(2026, 9, 11) - timedelta(days=59 - i)).isoformat()
         for i in range(60)]


def put_kline(con, d, code, o, h, l, c):
    con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, d, o, h, l, c, 1e6, None, None, None))


def mk_surge(con, code, jump=2.6):
    """前 55 日横盘 10 元，最后 5 日急拉——历史上会产出跨越式伪买区。"""
    c = 10.0
    for i in range(55):
        put_kline(con, DATES[i], code, c, c * 1.01, c * 0.99, c)
    for i in range(55, 60):
        c = 10.0 * (1 + (jump - 1) * (i - 54) / 5)
        put_kline(con, DATES[i], code, c * 0.98, c * 1.02, c * 0.97, c)


def _cand(lo, hi, close, sell_high=None, pool="趋势", hint=None):
    c = {"code": "sh600100", "name": "测试", "pool": pool, "close": close,
         "buy_low": lo, "buy_high": hi, "sell_high": sell_high}
    if hint:
        c["action_hint"] = hint
    return c


# ---------------------------------------------------------------------------
# ② 买区自洽性
# ---------------------------------------------------------------------------

class TestBuyZoneSanity(unittest.TestCase):
    def test_surge_no_cross_zone(self):
        """急拉票的买区必须是窄带，不得出现 28~476 这种跨越式伪区间。"""
        con = get_conn(":memory:")
        mk_surge(con, "sh600100")
        rows = bld.recent_rows(con, "sh600100", date="2026-09-11")
        plan = engines.entry_plan(rows)
        lo, hi = plan["now_zone"]
        self.assertLess((hi - lo) / lo, engines.MAX_NOW_ZONE_WIDTH + 1e-6,
                        f"买区宽度必须受控，实得 {(hi-lo)/lo:.1%}")
        self.assertLess(hi / lo, 1.06, "买区上下沿不得跨越式拉开")

    def test_overheat_zone_below_close(self):
        """四态=过热/等回踩时，买点应回踩到均线基准（低于现价），不是贴着现价。"""
        con = get_conn(":memory:")
        mk_surge(con, "sh600100")
        rows = bld.recent_rows(con, "sh600100", date="2026-09-11")
        plan = engines.entry_plan(rows)
        if plan["state"] in ("等回踩", "过热"):
            self.assertLessEqual(plan["now_zone"][1], rows[-1][2],
                                 "追高时买区上沿不得高于现价")

    def test_target_zone_above_buy_zone(self):
        """趋势池目标区必须高于买入区（历史 bug：误用 pull_zone 导致卖价低于买价）。"""
        con = get_conn(":memory:")
        mk_surge(con, "sh600100")
        rows = bld.recent_rows(con, "sh600100", date="2026-09-11")
        plan = engines.entry_plan(rows)
        self.assertGreater(plan["target_zone"][0], plan["now_zone"][1],
                           "目标区下沿必须高于买区上沿")
        self.assertNotEqual(plan["target_zone"], plan["pull_zone"])

    def test_four_state_wins_over_zone(self):
        """引擎四态是权威：过热/勿追 不得被退化买区判成『现在买』。"""
        for hint, want in (("勿追", "观望"), ("禁买", "禁买"),
                           ("等回踩", "等回踩"), ("小仓试", "小仓试")):
            c = _cand(9.0, 11.0, 10.0, sell_high=13.0, hint=hint)
            self.assertEqual(scoring._decide(c), want,
                             f"四态 {hint} 必须判 {want}")
        # 四态说能买，但报价已跳出买区 → 降级等回踩，不得硬判现在买
        c = _cand(9.0, 9.5, 10.0, sell_high=13.0, hint="现在买")
        self.assertEqual(scoring._decide(c), "等回踩")

    def test_buy_zone_ok_gate(self):
        self.assertTrue(scoring.buy_zone_ok(_cand(10.0, 10.3, 10.1, 11.5)),
                        "窄带且有盈利空间 → 放行")
        self.assertFalse(scoring.buy_zone_ok(_cand(10.0, 20.0, 15.0, 25.0)),
                         "买区过宽（伪区间）→ 拦截")
        self.assertFalse(scoring.buy_zone_ok(_cand(10.0, 10.2, 10.1, 10.1)),
                         "目标区不高于买区（无盈利空间）→ 拦截")
        self.assertFalse(scoring.buy_zone_ok(_cand(20.0, 20.3, 10.0, 25.0)),
                         "买区整体远高于现价 → 拦截")

    def test_dist_pct(self):
        self.assertEqual(scoring.dist_pct(_cand(10.0, 10.5, 10.2)), 0.0)
        self.assertGreater(scoring.dist_pct(_cand(10.0, 10.5, 11.0)), 0)
        self.assertLess(scoring.dist_pct(_cand(10.0, 10.5, 9.0)), 0)


# ---------------------------------------------------------------------------
# ③ 全市场扫描覆盖面
# ---------------------------------------------------------------------------

class TestScanCoverage(unittest.TestCase):
    def _con(self):
        con = get_conn(":memory:")
        for d in DATES:
            put_kline(con, d, "sh000001", 3000, 3010, 2995, 3005)
        # 票A：数据齐全（K线同步到 09-11）
        mk_surge(con, "sh600100")
        # 票B：只有快照、完全没有K线——历史口径下它从未进入过宇宙
        # 票C：有历史但K线停在 09-10（陈旧一天）——历史口径下静默消失
        for i, d in enumerate(DATES[:-1]):
            c = 10.0 * (1 + i * 0.01)
            put_kline(con, d, "sh600300", c * 0.98, c * 1.02, c * 0.97, c)
        con.executemany("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
                        [("2026-09-11", "sh600100", "齐全票", 10.0, 1.0,
                          5e8, 2.0, 30e8),
                         ("2026-09-11", "sh600200", "未同步票", 10.0, 1.0,
                          5e8, 2.0, 30e8),
                         ("2026-09-11", "sh600300", "陈旧票", 10.0, 1.0,
                          5e8, 2.0, 30e8)])
        con.commit()
        return con

    def test_universe_is_union_of_snapshot_and_klines(self):
        con = self._con()
        uni = bld.scan_universe(con, "2026-09-11")
        self.assertIn("sh600100", uni, "K线历史里的票必须在宇宙内")
        self.assertIn("sh600200", uni,
                      "只有快照、当日无K线的票也必须在宇宙内（否则永远扫不到）")
        self.assertIn("sh600300", uni, "K线陈旧一天的票也必须在宇宙内")

    def test_missing_bar_is_traced_not_silent(self):
        con = self._con()
        _cands, skipped = bld.scan_all(con, "2026-09-11")
        reasons = {s["code"]: s["reason"] for s in skipped}
        self.assertIn("未更新", reasons.get("sh600300", ""),
                      "K线陈旧一天的票必须显式留痕「未更新」，不得静默丢弃")
        self.assertIn("sh600200", reasons,
                      "完全无K线的票也必须留痕，不得静默丢弃")
        cov = bld.LAST_SCAN_COVERAGE
        self.assertEqual(cov["universe"], 3)
        self.assertEqual(cov["with_bar"], 1, "只有票A拿到当日K线")
        self.assertEqual(cov["missing_bar"], 2)
        self.assertAlmostEqual(cov["coverage"], 33.3, places=1)

    def test_every_skip_has_reason(self):
        con = self._con()
        _cands, skipped = bld.scan_all(con, "2026-09-11")
        self.assertTrue(skipped)
        for s in skipped:
            self.assertTrue(s.get("reason"), f"{s['code']} 剔除却无原因")

    def test_no_future_bars(self):
        """recent_rows 传 date 时不得返回未来数据。"""
        con = self._con()
        put_kline(con, "2026-09-12", "sh600300", 99, 99, 99, 99)
        rows = bld.recent_rows(con, "sh600300", date="2026-09-11")
        self.assertTrue(all(r[0] <= "2026-09-11" for r in rows),
                        "扫描不得使用目标日之后的K线（未来函数）")


# ---------------------------------------------------------------------------
# ① 推送版面
# ---------------------------------------------------------------------------

def _decision(close, lo, hi, dist=0.0, status="条件满足"):
    return {"code": "sh600100", "name": "示例票", "status": status,
            "zone": [lo, hi], "stop": lo * 0.94, "close": close,
            "dist_pct": dist, "sell_low": hi * 1.08, "sell_high": hi * 1.16,
            "invalid_if": "收盘跌破止损", "valid_until": "2026-09-18",
            "reason": "回踩MA5企稳", "research_grade": "B", "score": 72}


class TestPushLayout(unittest.TestCase):
    def _brief(self, **kw):
        return notifier.render_brief(
            "2026-09-11", _decision(10.2, 10.0, 10.3),
            [_decision(20.2, 20.0, 20.3, status="等待确认")],
            [{"code": "sh600000", "new": "超价取消", "reason": "超上限"}],
            {"reviewed": 120, "data_date": "2026-09-11",
             "valid_until": "2026-09-18", "universe": 4936, "coverage": 93.0,
             "note": "情绪58；评分不是上涨概率。"}, **kw)

    def test_table_layout_no_float(self):
        html = self._brief()
        self.assertIn("<table", html, "指标必须用表格对齐")
        self.assertNotIn("float:", html,
                         "float 在微信/PushPlus webview 会错位，禁用")

    def test_required_fields(self):
        html = self._brief()
        for need in ("首选观察", "备选观察", "计划变化", "复核 120 只",
                     "不追价上限", "失效条件", "超价取消",
                     "现价", "买入区间", "目标区间", "止损"):
            self.assertIn(need, html)

    def test_pending_group_separated(self):
        """现价不在买区的票必须进独立分组，且标注距买区。"""
        html = self._brief(pending=[_decision(11.0, 10.0, 10.3, dist=6.8,
                                              status="等待确认")])
        self.assertIn("等待更好买点", html)
        self.assertIn("距买区", html)
        self.assertIn("待回踩", html)

    def test_coverage_shown(self):
        self.assertIn("覆盖", self._brief())

    def test_still_concise(self):
        html = self._brief()
        self.assertLess(len(re.sub(r"<[^>]+>", "", html)), 2500,
                        "M35：主报告应保持简洁")

    def test_text_degrade_is_readable(self):
        """ServerChan 等纯文本通道：必须分行可读，不是黏成一坨。"""
        txt = notifier.html_to_text(self._brief())
        lines = [l for l in txt.splitlines() if l.strip()]
        self.assertGreater(len(lines), 8, "纯文本降级必须保留分行结构")
        self.assertIn("买入区间", txt)
        self.assertIn("现价", txt)
        self.assertNotIn("<", txt, "纯文本里不得残留标签")

    def test_clip_keeps_content(self):
        """超限裁剪按整卡回退，不得返回空串（旧实现对 div 版式失效）。"""
        big = self._brief() * 40
        clipped = notifier._clip_html(big)
        self.assertLessEqual(len(clipped), notifier.PP_HTML_CAP)
        self.assertIn("买入区间", clipped, "裁剪后仍要保留可读内容")
        self.assertTrue(clipped.count("<div") == clipped.count("</div>"),
                        "裁剪后标签必须闭合")

    def test_empty_day_message(self):
        html = notifier.render_brief(
            "2026-09-11", None, [], [], {"reviewed": 0})
        self.assertIn("无当下可买入", html)


if __name__ == "__main__":
    unittest.main(verbosity=1)
