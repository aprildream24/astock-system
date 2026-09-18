# -*- coding: utf-8 -*-
"""2026-09-19 用户四问回归：
① 「要么上要么下」——决断门控扩展到波段池 + 躺榜衰减/移出 + 决断力证据；
② 网页端持仓管理（holdings-sync 前端契约）；
③ push_modes 推送开关（不发送/不占额度/不写账本）。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pipeline import engines, notifier, scoring  # noqa: E402
from pipeline.core import get_conn  # noqa: E402


def _rows_trending(days=30, daily=1.2):
    """稳定爬升：决断力应达标。"""
    rows, c = [], 10.0
    for i in range(days):
        c *= 1 + daily / 100
        rows.append([f"d{i}", c * 0.995, c, c * 1.005, c * 0.99, 1e6])
    return rows


def _rows_choppy(days=30):
    """进二退一横盘：净位移≈0，决断力应不达标。"""
    rows, c = [], 10.0
    for i in range(days):
        c *= 1 + (2.0 if i % 2 == 0 else -2.0) / 100
        rows.append([f"d{i}", c * 0.995, c, c * 1.01, c * 0.99, 1e6])
    return rows


def _rows_flat(days=30):
    rows = []
    for i in range(days):
        rows.append([f"d{i}", 10.0, 10.0, 10.01, 9.99, 1e6])
    return rows


class TestDecisiveStats(unittest.TestCase):
    def test_trending_pass(self):
        st = engines.decisive_stats(_rows_trending())
        self.assertTrue(st["ok"])
        self.assertGreater(st["net"], engines.DECISIVE_NET_MIN)
        self.assertGreater(st["eff"], engines.DECISIVE_EFF_MIN)

    def test_choppy_fail(self):
        st = engines.decisive_stats(_rows_choppy())
        self.assertIsNotNone(st)
        self.assertFalse(st["ok"], "进二退一必须判磨叽")

    def test_flat_fail(self):
        self.assertFalse(engines.decisive_stats(_rows_flat())["ok"])

    def test_insufficient_data(self):
        self.assertIsNone(engines.decisive_stats(_rows_trending(days=10)))

    def test_screen_decisive_same_behavior(self):
        self.assertTrue(engines.screen_decisive(_rows_trending()))
        self.assertFalse(engines.screen_decisive(_rows_choppy()))


class TestWaitDaysDecay(unittest.TestCase):
    def _cand(self, code, score_base, wait_days=None, sector=None):
        return {"code": code, "name": code, "pool": "趋势", "close": 10.0,
                "buy_low": 9.9, "buy_high": 10.05, "sell_low": 11.0,
                "sell_high": 11.5, "stop": 9.4, "tag": "趋势",
                "worth_score": score_base, "action": "现在买",
                "sector": sector, "wait_days": wait_days}

    def test_stale_excluded(self):
        env = {"趋势": 1.0}
        # wd>=5：终审直接 continue（build 侧 <5 过滤是第一道，这里双保险）
        cands = [self._cand("600001", 80, wait_days=5),
                 self._cand("600002", 60, wait_days=None)]
        picks = scoring.compute_top_picks(cands, env, {})
        self.assertEqual([p["code"] for p in picks], ["600002"],
                         "连续挂榜>=5日未兑现 → 移出，哪怕分数最高")

    def test_decay_ordering(self):
        env = {"趋势": 1.0}
        # 同分票：躺榜 3 日的应排在新面孔之后
        cands = [self._cand("600001", 70, wait_days=3),
                 self._cand("600002", 70, wait_days=None)]
        picks = scoring.compute_top_picks(cands, env, {})
        self.assertEqual(picks[0]["code"], "600002",
                         "连续挂榜未兑现 → 每日8%折价，新面孔优先")
        # 折价有界：wd=3 → ×0.92²
        self.assertAlmostEqual(picks[1]["eff_score"], 70 * 0.92 * 0.92 * 0.72,
                               places=1)

    def test_no_wait_days_unchanged(self):
        """老调用方（无 wait_days 字段）行为不变——M43 默认值等价红线。"""
        env = {"趋势": 1.0}
        cands = [self._cand("600001", 80), self._cand("600002", 60)]
        picks = scoring.compute_top_picks(cands, env, {})
        self.assertEqual([p["code"] for p in picks], ["600001", "600002"])


class TestPushModesSwitch(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = notifier.load_config

    def tearDown(self):
        notifier.load_config = self._orig_cfg

    def _cfg(self, switches):
        return {"push_dry_run": True, "primary_channel": "pushplus",
                "push_tag": "Astra", "push_modes": switches}

    def test_exact_mode_off(self):
        notifier.load_config = lambda: self._cfg({"intraday_am": False})
        r = notifier.push("intraday_am", "t", "600000", date="2026-09-19",
                          con=get_conn(":memory:"))
        self.assertTrue(r.get("skipped"), "关闭的 mode 必须静默跳过")
        self.assertFalse(r.get("sent"))

    def test_prefix_match_off(self):
        notifier.load_config = lambda: self._cfg({"exec": False})
        r = notifier.push("exec_auto", "t", "600000", date="2026-09-19",
                          con=get_conn(":memory:"))
        self.assertTrue(r.get("skipped"), "exec_auto 应被前缀 exec 命中")

    def test_default_on(self):
        notifier.load_config = lambda: self._cfg({})
        r = notifier.push("m_test", "t", "600000", date="2026-09-19",
                          con=get_conn(":memory:"))
        self.assertFalse(r.get("skipped"), "未配置的 mode 默认开（行为不变）")

    def test_off_mode_no_ledger_write(self):
        notifier.load_config = lambda: self._cfg({"watch": False})
        con = get_conn(":memory:")
        notifier.push("watch_advice", "t", "600000", date="2026-09-19",
                      con=con)
        n = con.execute("SELECT COUNT(*) FROM push_ledger").fetchone()[0]
        self.assertEqual(n, 0, "关掉的推送不得写账本（像从未触发过）")


class TestHoldingsFrontendContract(unittest.TestCase):
    def test_parse_holdings_roundtrip(self):
        from pipeline import sync_holdings
        payload = json.dumps([
            {"code": "sz002493", "name": "荣盛石化", "buy_price": 13.06,
             "shares": 1000, "buy_date": "2026-09-18"}], ensure_ascii=False)
        rows = sync_holdings.parse_holdings(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "sz002493")
        self.assertEqual(rows[0]["buy_price"], 13.06)
        # 逗号串（纯加仓场景）
        rows2 = sync_holdings.parse_holdings("002493,600519")
        self.assertEqual([r["code"] for r in rows2],
                         ["sz002493", "sh600519"])

    def test_appjs_has_holdings_ui(self):
        """结构断言：前端必须有持仓管理入口（防回归删除）。"""
        src = open(os.path.join(os.path.dirname(HERE), "site_template",
                                "app.js"), encoding="utf-8").read()
        self.assertIn("holdingManageCard", src)
        self.assertIn("holdings-sync", src)
        self.assertIn("_pushHoldingsToCloud", src)


class TestCardEvidence(unittest.TestCase):
    def test_card_renders_decisive_and_wait(self):
        d = {"code": "600100", "name": "示例", "pool": "趋势", "close": 10.0,
             "zone": [9.9, 10.05], "stop": 9.4, "status": "条件满足",
             "decisive": {"net": 12.3, "eff": 0.62, "ok": True},
             "wait_days": 3, "valid_until": "2026-09-25",
             "invalid_if": "跌破止损"}
        html = notifier.render_card(d)
        self.assertIn("决断力(20日)", html)
        self.assertIn("净移+12.3%", html)
        self.assertIn("效率0.62", html)
        self.assertIn("已挂榜 3 日", html)
        # 无字段时不渲染（老数据兼容）
        html2 = notifier.render_card(
            {"code": "600100", "name": "示例", "pool": "趋势", "close": 10.0,
             "zone": [9.9, 10.05], "stop": 9.4, "status": "条件满足",
             "valid_until": "x"})
        self.assertNotIn("决断力", html2)
        self.assertNotIn("已挂榜", html2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
