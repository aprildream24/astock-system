# -*- coding: utf-8 -*-
"""2026-09-22 用户反馈回归：
① 持仓体检一票一卡（手机不再错行）；② 换股候选排序即优先级 + 评分口径说明；
③ 模拟盘每笔成交即时 force 推送（不再攒到总结）；④ rec_picks 存综合可执行分。
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pipeline import executor, notifier  # noqa: E402


H = {"code": "sz002493", "name": "荣盛石化", "close": 12.85,
     "buy_price": 13.06, "pnl_pct": -1.61, "verdict": "建议减仓/离场",
     "exit_action": "SELL", "exit_reasons": ["ATR保护线", "MA20破位"],
     "stop": 12.08, "zone": [12.65, 13.08], "sector": "炼化及贸易",
     "sector_hot": False}


class TestHoldingLayout(unittest.TestCase):
    def test_one_card_per_stock(self):
        """每只持仓独立卡片（一票一卡），信息竖排不横挤。"""
        html = notifier.render_holding_advice([H, dict(H, code="sh600100",
                                                       name="强势票")], [],
                                              "2026-09-18")
        self.assertEqual(html.count("荣盛石化"), 1)
        self.assertEqual(html.count("强势票"), 1)
        # 每卡必含四要素且各自成行
        self.assertIn("成本", html)
        self.assertIn("现价", html)
        self.assertIn("止损", html)
        self.assertIn("12.85", html)  # 现价行

    def test_weak_reasons_prominent(self):
        html = notifier.render_holding_advice([dict(H, swap_hint="持续走弱",
                                                    phase="接近到期",
                                                    hold_days=7,
                                                    hold_limit=8)], [],
                                              "d")
        for frag in ("持续走弱", "持有周期", "7/8 日（接近到期）",
                     "接近到期"):
            self.assertIn(frag, html)


class TestSwapCandidateOrdering(unittest.TestCase):
    def test_buyable_first_over_high_score_wait(self):
        """「可买 80 分」必须排在「等回踩 96 分」前面——排序即答案。"""
        cands = [
            {"code": "sz003026", "name": "中晶科技", "action": "等回踩",
             "score": 96, "buy_low": 32.91, "buy_high": 34.24,
             "stop": 31.51, "sector": "电子"},
            {"code": "sh600343", "name": "航天动力", "action": "现在买",
             "score": 80, "eff_score": 58, "buy_low": 21.56,
             "buy_high": 22.32, "stop": 19.94, "sector": "通用设备"},
        ]
        html = notifier.render_holding_advice([], cands, "2026-09-18")
        i_buy = html.find("航天动力")
        i_wait = html.find("中晶科技")
        self.assertLess(0 < i_buy < i_wait, float("inf") if i_buy > 0 else -1,
                        "可下单的必须排在等回踩前面")
        self.assertIn("排序即优先级", html)
        self.assertIn("以排序为准", html, "必须解释评分口径")

    def test_rank_numbers_render(self):
        cands = [{"code": "600001", "name": "A", "action": "现在买",
                  "score": 80, "buy_low": 1, "buy_high": 2},
                 {"code": "600002", "name": "B", "action": "等回踩",
                  "score": 90, "buy_low": 1, "buy_high": 2}]
        html = notifier.render_holding_advice([], cands, "d")
        self.assertIn("<b>1</b>", html)
        self.assertIn("<b>2</b>", html)


class TestExecForceOnBuy(unittest.TestCase):
    def test_buy_batch_pushed_with_force(self):
        """每笔模拟买入必须即时 force 推送（不被日熔丝吞掉）。"""
        con = executor.core.get_conn(":memory:")
        executor.ensure_account(con, "2026-09-21")
        with mock.patch.object(notifier, "push",
                               return_value={"status": "sent"}) as push:
            log = [("sh600001", "BUY", "1000股@10.00（现在买·第1档30%）"),
                   ("sh600002", "REJECT", "资金不足 1 手")]
            executor._exec_push(con, "auto", "2026-09-21", log)
        self.assertTrue(push.called)
        self.assertTrue(push.call_args[1].get("force"),
                        "买入批次必须 force 即时送达")
        body = push.call_args[0][2]
        self.assertIn("资金不足", body, "到价未成交同批可见")


class TestEffScoreStored(unittest.TestCase):
    def test_rec_picks_stores_eff_score(self):
        src = open(os.path.join(ROOT, "pipeline", "build.py"),
                   encoding="utf-8").read()
        self.assertIn('c.get("eff_score") or c["score"]', src,
                      "rec_picks 评分列必须是综合可执行分")


if __name__ == "__main__":
    unittest.main(verbosity=1)
