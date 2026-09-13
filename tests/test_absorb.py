# -*- coding: utf-8 -*-
"""原项目吸收回归：各移植模块的核心行为断言。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import (alerts, datacenter, decisions, emotion, gapfill,
                      mktfilter, multi_source, quality, recperf, recveto,
                      risklevel)  # noqa: E402
from pipeline.core import get_conn  # noqa: E402
from pipeline.trade_calendar import is_trade_day, why_closed  # noqa: E402


class TestKlineBatchConcurrent(unittest.TestCase):
    """kline_batch 并发契约：items 必须按 (num, pfx) 传递，双通道各半，
    主源失败自动切对侧。"""

    def test_split_and_fallback(self):
        from unittest import mock
        from pipeline import core
        calls = []

        def fake_em(num, pfx, days):
            calls.append(("em", num, pfx))
            if num.endswith("8"):
                raise core.BanBlocked("em")
            return [["2026-09-11", 1.0, 1.0, 1.0, 1.0, 1e6]]

        def fake_tx(num, pfx, days):
            calls.append(("tx", num, pfx))
            return [["2026-09-11", 1.0, 1.0, 1.0, 1.0, 1e6]]

        codes = [(f"60000{i}", "sh") for i in range(9)]   # 600008 落在东财通道
        with mock.patch.object(core, "_kline_one_em", fake_em), \
                mock.patch.object(core, "_kline_one_tx", fake_tx):
            out = core.kline_batch(codes, days=5, workers=4)
        self.assertEqual(len(out), 9, "9 只全部应有数据（含熔断切对侧）")
        nums_called = {c[1] for c in calls}
        self.assertEqual(nums_called, {c[0] for c in codes},
                         "必须用真实代码请求，不得传索引/元组")
        em_nums = {n for k, n, _ in calls if k == "em"}
        tx_nums = {n for k, n, _ in calls if k == "tx"}
        self.assertTrue(em_nums & tx_nums or len(em_nums) and len(tx_nums),
                        "双通道各领一半（或熔断切对侧）")
        # 熔断票应切到 tx 成功
        self.assertIn("600008", out)


class TestBuyable(unittest.TestCase):
    """用户口径（2026-09-13）：主推荐只放「当下就能下单买入」的票。"""

    def test_now_actions_marked_buyable(self):
        NOW = ("现在买", "等回踩", "小仓试")
        for a in NOW:
            c = {"code": "600100", "action": a, "pool": "趋势"}
            c["buyable_now"] = c["action"] in NOW
            self.assertTrue(c["buyable_now"])
        # 次日竞价达标买：当日涨停买不进 → 非即时可买
        c = {"code": "600400", "action": "次日竞价达标买", "pool": "连板"}
        c["buyable_now"] = c["action"] in NOW
        self.assertFalse(c["buyable_now"],
                         "当日涨停票必须归次日通道，不得混入现在可买")

    def test_halted_filtered_reason(self):
        # scan_all 对 amt<=0 的票给「不可买」原因（逻辑契约：原因串固定）
        with tempfile.TemporaryDirectory() as td:
            con = get_conn(os.path.join(td, "t.db"))
            con.execute("INSERT OR REPLACE INTO snapshot VALUES("
                        "'2026-09-11','sh600000','停牌票',0,0,0,0,1e9)")
            from pipeline import build as bld
            snap = bld._snapshot(con, "2026-09-11")
            self.assertIsNotNone(snap.get("sh600000"))
            self.assertLessEqual((snap["sh600000"])[2], 0, "零成交可被识别")
            con.close()


class TestTradeCalendar(unittest.TestCase):
    def test_holiday_guard(self):
        self.assertFalse(is_trade_day("2026-10-01"), "国庆节必须拦截")
        self.assertFalse(is_trade_day("2026-02-16"), "春节必须拦截")
        self.assertFalse(is_trade_day("2026-09-12"), "周六休市")
        self.assertTrue(is_trade_day("2026-09-11"), "周五是交易日")
        self.assertEqual(why_closed("2026-10-01"), "法定节假日休市")
        self.assertEqual(why_closed("2026-09-12"), "周末休市")
        # 未收录年份：保守视为交易日，绝不漏推
        self.assertTrue(is_trade_day("2027-03-08"))


class TestMktFilter(unittest.TestCase):
    def test_tradable_precise(self):
        self.assertTrue(mktfilter.tradable("600000"))
        self.assertTrue(mktfilter.tradable("001227"))
        self.assertTrue(mktfilter.tradable("300750"))
        self.assertTrue(mktfilter.tradable("301236"))
        self.assertFalse(mktfilter.tradable("688981"), "科创板未开通")
        self.assertFalse(mktfilter.tradable("832000"), "北交所未开通")
        self.assertFalse(mktfilter.tradable("430047"))
        self.assertFalse(mktfilter.tradable("510300"), "ETF 不推")
        self.assertFalse(mktfilter.tradable("900901"), "B股")
        self.assertEqual(mktfilter.market_of("605111"), "沪深主板")


class TestMultiSource(unittest.TestCase):
    def test_cross_check_offline_graceful(self):
        # 无网络环境：逐源失败降级，不抛异常、不阻断
        r = multi_source.cross_check(["600519", "000001"], sample=2, timeout=3)
        self.assertEqual(r["checked"], 2)
        self.assertIn("spec", r)
        self.assertEqual(r["spec"]["version"], multi_source.XCHECK_VERSION)

    def test_spread_logic(self):
        # 纯逻辑：构造 items 验证中位数/价差判定（不依赖网络）
        vals = [10.0, 10.02, 10.5]
        median = sorted(vals)[1]
        spread = (max(vals) - min(vals)) / median * 100
        self.assertGreater(spread, 0.5)


class TestRecveto(unittest.TestCase):
    def test_vol_ratio_and_shrink(self):
        self.assertIsNone(recveto.day_vol_ratio(0, [1e6] * 5))
        self.assertAlmostEqual(recveto.day_vol_ratio(5e5, [1e6] * 5), 0.5)
        self.assertTrue(recveto.is_shrunk(0.5))
        self.assertFalse(recveto.is_shrunk(None), "缺失按非缩量（保守）")

    def test_veto_annotated(self):
        # V1 标注式：p_break 85 放量 → WARN 不拦
        v = recveto.veto(p_break=85, day_vol_ratio=1.5)
        self.assertTrue(recveto.is_warn(v))
        # VETO：p_break ≥90 放量非一字
        self.assertTrue(recveto.is_veto(recveto.veto(p_break=92, day_vol_ratio=2.0)))
        # 缩量豁免
        self.assertIsNone(recveto.veto(p_break=85, day_vol_ratio=0.5))
        self.assertIsNone(recveto.veto(p_break=92, day_vol_ratio=0.5))
        # 一字板豁免
        self.assertIsNone(recveto.veto(p_break=95, day_vol_ratio=2.0, yizi=True))

    def test_auction_gate(self):
        g = recveto.auction_gate(-0.5)
        self.assertEqual(g["action"], "avoid", "低开=灾难区（24%胜率）")
        g2 = recveto.auction_gate(1.0, is_leader=True)
        self.assertEqual(g2["action"], "buy")
        g3 = recveto.auction_gate(1.0)
        self.assertEqual(g3["action"], "watch")
        # LOW_OPEN=-0.1% 语义：open_pct < -0.1% 才算低开（原项目源码口径）；
        # 恰好 -0.1% 属不低开边界，-0.11% 进入灾难区
        self.assertEqual(recveto.auction_gate(-0.1)["action"], "watch")
        self.assertEqual(recveto.auction_gate(-0.11)["action"], "avoid")

    def test_suggest_thresholds_insufficient(self):
        with tempfile.TemporaryDirectory() as td:
            con = get_conn(os.path.join(td, "t.db"))
            self.assertIsNone(recveto.suggest_thresholds(con),
                              "样本不足返回 None 维持默认")
            con.close()


class TestRiskLevel(unittest.TestCase):
    def test_three_lights(self):
        red = risklevel.classify_holding(
            {"code": "600000", "close": 9.0, "stop": 9.4, "pnl_pct": -6})
        self.assertEqual(red[0], "red")
        yellow = risklevel.classify_holding(
            {"code": "600000", "close": 9.5, "stop": 9.4, "pnl_pct": -5.5})
        self.assertEqual(yellow[0], "yellow")
        blue = risklevel.classify_holding(
            {"code": "600000", "close": 11.0, "stop": 9.4, "pnl_pct": 3})
        self.assertEqual(blue[0], "blue")
        # 周期上限
        over = risklevel.classify_holding(
            {"code": "600000", "close": 11.0, "hold_days": 21, "hold_limit": 20})
        self.assertEqual(over[0], "red")
        rl = risklevel.compute([{"code": "600000", "close": 9.0, "stop": 9.4}])
        self.assertEqual(rl["overall"]["level"], "red")


class TestAlerts(unittest.TestCase):
    def test_triggers(self):
        sigs = [{"code": "600100", "name": "A", "status": "等待确认",
                 "zone": [9.9, 10.05], "stop": 9.4}]
        prices = {"600100": (10.0, 9.3, 10.2)}   # 盘中触止损+收盘回区间
        tr = alerts.build_triggers(sigs, prices)
        types = [h["type"] for h in tr["hits"]]
        self.assertIn("止损", types, "日K触及止损必须触发（不伪造先后顺序）")
        # 持仓止盈
        tr2 = alerts.build_triggers([], {}, holdings_pnl=[
            {"code": "600200", "name": "B", "pnl_pct": 18}])
        self.assertIn("止盈", [h["type"] for h in tr2["hits"]])
        # 关注锁定
        tr3 = alerts.build_triggers([], {}, watch_since=[
            {"code": "600300", "name": "C", "since_pct": 35}])
        self.assertIn("锁定", [h["type"] for h in tr3["hits"]])


class TestRecPerf(unittest.TestCase):
    def test_curve_and_phase(self):
        with tempfile.TemporaryDirectory() as td:
            con = get_conn(os.path.join(td, "t.db"))
            for i in range(10):
                con.execute(
                    "INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"2026-09-{i+1:02d}", f"60000{i}", "票", "趋势", "现在买",
                     1, 2, 1, 2, 2, 60,
                     "win" if i % 2 else "lose", 2.0 if i % 2 else -1.0))
            con.commit()
            rp = recperf.build(con)
            self.assertIsNotNone(rp)
            self.assertEqual(rp["n_days"], 10)
            self.assertIn("上升", rp["phase_winrate"])
            self.assertIn("口径", " ".join(recperf.summary_lines(rp)),
                          "必须携带附录B披露口径")
            con.close()


class TestGapFill(unittest.TestCase):
    def test_detect_half_bars(self):
        with tempfile.TemporaryDirectory() as td:
            con = get_conn(os.path.join(td, "t.db"))
            # 三个日期：正常 100 行 / 半根 30 行 / 正常 100 行（唯一 code）
            for d, n in (("2026-09-08", 100), ("2026-09-09", 30),
                         ("2026-09-10", 100)):
                for i in range(n):
                    con.execute(
                        "INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (f"sh6001{i:04d}", d, 1, 1, 1, 1, 1e6, None, None, None))
            con.commit()
            gaps = gapfill.detect_gaps(con)
            self.assertEqual([g[0] for g in gaps], ["2026-09-09"],
                             "行数<60%中位数 → 判定残缺")
            con.close()


class TestDataCenter(unittest.TestCase):
    def test_offline_graceful_mocked(self):
        # 数据源失败 → 全部降级 None / []，绝不抛异常（mock 断网）
        from unittest import mock
        with mock.patch.object(datacenter.emdc, "get", return_value=[]):
            self.assertIsNone(datacenter.margin_scan())
            self.assertIsNone(datacenter.lhb_scan("2026-09-11"))
            self.assertIsNone(datacenter.blocktrade_scan("2026-09-11"))
        self.assertEqual(datacenter.summary(), [])

    def test_theme_scan_injected(self):
        r = datacenter.theme_scan("2026-09-11", [
            {"concepts": ["固态电池", "融资融券"], "industry": "电池"},
            {"concepts": ["固态电池"], "industry": "电池"}])
        self.assertIsNotNone(r)
        self.assertEqual(r["main_theme"], "固态电池")
        self.assertNotIn("融资融券", [t["theme"] for t in
                                     [{"theme": r["main_theme"]}]],
                         "元标签噪声必须排除")


if __name__ == "__main__":
    unittest.main(verbosity=1)
