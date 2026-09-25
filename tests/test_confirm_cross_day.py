# -*- coding: utf-8 -*-
"""跨天确认（用户模型）：收盘首推=1次；次日盘前仍在=双确认；竞价后仍在=三确认。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pipeline import build as bld, notifier  # noqa: E402
from pipeline.core import get_conn  # noqa: E402


class TestCrossDayConfirm(unittest.TestCase):
    def test_count_accumulates_cross_day(self):
        con = get_conn(":memory:")
        # Day 1 close 推荐 600001
        con.execute("INSERT OR REPLACE INTO confirm_log VALUES(?,?,?)",
                    ("2026-09-22", "close", "600001"))
        # Day 2 pre 推荐 600001（仍在列）
        con.execute("INSERT OR REPLACE INTO confirm_log VALUES(?,?,?)",
                    ("2026-09-23", "pre", "600001"))
        # Day 2 auction 推荐 600001（仍在列）
        con.execute("INSERT OR REPLACE INTO confirm_log VALUES(?,?,?)",
                    ("2026-09-23", "auction", "600001"))
        # Day 2 close 推荐 600002（新票）
        con.execute("INSERT OR REPLACE INTO confirm_log VALUES(?,?,?)",
                    ("2026-09-23", "close", "600002"))
        con.commit()
        cc = bld._confirm_counts(con, "2026-09-23", window=10)
        self.assertEqual(cc.get("600001"), 3, "600001 跨 2 天 3 时点 = 3 次")
        self.assertEqual(cc.get("600002"), 1, "600002 首推 = 1 次")

    def test_label_semantics(self):
        # count 1 → 无标注（首推）
        # count 2 → 双确认
        # count 3+ → 三确认（最强）
        c1 = {"code": "600001", "confirms": 1}
        c2 = {"code": "600002", "confirms": 2}
        c3 = {"code": "600003", "confirms": 3}
        # render_card 里的状态文本应反映确认次数
        for c, expect in ((c1, None), (c2, "双确认"), (c3, "三确认")):
            d = {"code": c["code"], "name": "票", "close": 10,
                 "confirms": c["confirms"], "status": "条件满足",
                 "zone": [9, 11], "stop": 8}
            html = notifier.render_card(d)
            if expect:
                self.assertIn(expect, html,
                              f"confirms={c['confirms']} 应显示 {expect}")
            else:
                self.assertNotIn("确认次数", html,
                                 "首推无标注")


if __name__ == "__main__":
    unittest.main(verbosity=1)
