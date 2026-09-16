# -*- coding: utf-8 -*-
"""推送/加密/熔断/T+1 回归：去重账本、复核优先级、加密红线、胜率熔断全通道。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import notifier, publish, scoring  # noqa: E402
from pipeline.core import get_conn  # noqa: E402


def _mk_cand(**kw):
    c = {"code": "600000", "name": "测试票", "pool": "区间", "close": 10.0,
         "buy_low": 9.9, "buy_high": 10.05, "sell_low": 11.2,
         "sell_high": 11.5, "stop": 9.4, "score": 60, "action": "现在买",
         "cycle_hint": "慢节奏箱体", "hold_days": 20}
    c.update(kw)
    return c


class TestPush(unittest.TestCase):
    def test_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            notifier.DIST_LEDGER = os.path.join(td, "ledger.json")
            con = get_conn(os.path.join(td, "t.db"))
            # 2026-09-15：用例考察「去重账本」，不得依赖本机 config/notify.json。
            # CI 无该文件 → 通道列表为空 → sent=False（真实 HTTP 也无凭据）。
            # 固定通道与发送结果，让断言确定。
            from pipeline import wxpusher as wx
            o_load, o_send = wx.load_accounts, wx.send
            o_cfg = notifier.load_config
            wx.load_accounts = lambda: [{"name": "A", "app_token": "AT",
                                         "uids": ["U"]}]
            wx.send = lambda acct, t, c, timeout=12: ("sent", "ok")
            notifier.load_config = lambda *a, **k: {
                "push_dry_run": False, "primary_channel": "wxpusher",
                "push_tag": "Test"}
            try:
                r1 = notifier.push("m1", "标题", "候选 600000 000001",
                                   date="2026-09-12", con=con)
                r2 = notifier.push("m1", "标题", "候选 600000 000001",
                                   date="2026-09-12", con=con)
                self.assertTrue(r1["sent"])
                self.assertTrue(r2.get("dedup"), "同 biz_key 二次推送必须拦截")
                # 规则版本升级 → biz_key 变化
                old = notifier.RULE_VERSION
                notifier.RULE_VERSION = "v2-TEST-UP"
                k_new = notifier.biz_key("m1", "2026-09-12",
                                         ["600000", "000001"])
                r3 = notifier.push("m1", "标题", "候选 600000 000001",
                                   date="2026-09-12", con=con)
                notifier.RULE_VERSION = old
                self.assertNotEqual(k_new, r1["key"], "规则升级必须产生新 biz_key")
                # ⚠️ 2026-09-16 语义归位（不是放宽，是更严）：
                # 日级保险丝（同 mode + 同**交易日**已 sent → 拦）**优先于**
                # biz_key 变化 —— 它的设计意图就是"一天一条"（防 cron 幽灵
                # 延迟重复推送），规则升级不构成例外。
                # 旧实现里账本 ts 记的是"写入时刻（当前日期）"，补发/跨日场景
                # 恰好绕过这道闸，所以这里曾经断言 r3 放行；ts 锚定交易日后
                # 语义归位（同时修掉"补发历史会吃掉当日额度"的血案），
                # 断言同步更新为"必须被日级保险丝拦"。
                self.assertTrue(r3.get("daily_gate"),
                                "同交易日已有 sent 记录 → 日级保险丝必须拦截")
                # 换一个交易日 → 放行（证明 biz_key 机制本身没被日闸锁死）
                r4 = notifier.push("m1", "标题", "候选 600000 000001",
                                   date="2026-09-14", con=con)
                self.assertTrue(r4["sent"], "换交易日后必须放行")
            finally:
                wx.load_accounts, wx.send = o_load, o_send
                notifier.load_config = o_cfg
                con.close()

    def test_cand_line_cap(self):
        c = _mk_cand(cycle_hint="急拉后高位横住" * 6, entry_hint="距买点 3% 回落至 9.9 再关注")
        line = notifier._cand_line(c)
        self.assertLessEqual(len(line), notifier.CAND_LINE_CAP)
        # 买/卖/停永不丢
        self.assertIn("买", line)
        self.assertIn("停", line)

    def test_clip_html(self):
        html = "".join(f"<li>候选{i}号 60000{i % 10}" + "x" * 80 + "</li>"
                       for i in range(400))
        self.assertLessEqual(len(notifier._clip_html(html)), notifier.PP_HTML_CAP)

    def test_prev_pick_priority(self):
        it = _mk_cand(stop=9.4, buy_high=10.05)
        # 现价 ≤ 止损 → 跌破止损（最高优先级）
        st = notifier._prev_pick_status(it, [], {"600000": {"price": 9.3, "prev": 10.0, "open": 9.5, "open_pct": -5.0}})
        self.assertIn("跌破止损", st)
        # 走坏优先于买区：暴跌 -5% 跌进买区 ≠ 好买点
        st = notifier._prev_pick_status(it, [], {"600000": {"price": 9.5, "prev": 10.2, "open": 9.9, "open_pct": -2.9}})
        self.assertIn("走坏", st)
        # 还在跟
        st = notifier._prev_pick_status(it, [], {"600000": {"price": 10.0, "prev": 10.0, "open": 10.0, "open_pct": 0.5}})
        self.assertIn("还在跟", st)
        # 涨过头
        st = notifier._prev_pick_status(it, [], {"600000": {"price": 10.6, "prev": 10.0, "open": 10.4, "open_pct": 4.0}})
        self.assertIn("涨过头", st)
        # 降级路径：今日剔除
        st = notifier._prev_pick_status(it, None)
        self.assertIn("剔除", st)

    def test_open_snapshot_filter_static(self):
        """#605-② 静态断言：未开盘快照过滤在位。"""
        src = open(notifier.__file__, encoding="utf-8").read()
        core_src = open(os.path.join(os.path.dirname(notifier.__file__),
                                     "core.py"), encoding="utf-8").read()
        self.assertIn("abs(price - prev) < 0.001", core_src)
        self.assertIn("未开盘过滤", core_src)


class TestCrypto(unittest.TestCase):
    def test_roundtrip_and_wrong_password(self):
        blob = publish.encrypt_bytes('{"hello":"世界"}'.encode(), "口令A")
        self.assertEqual(publish.decrypt_bytes(blob, "口令A"), '{"hello":"世界"}'.encode())
        with self.assertRaises(Exception):
            json.loads(publish.decrypt_bytes(blob, "错误口令").decode())

    def test_owner_strip(self):
        data = {"holdings_detail": [{"cost": 9.5}], "candidates": [{"code": "600000", "cost": 9.5, "float_pnl": 100}]}
        out = publish.strip_owner_fields(data, is_owner=False)
        self.assertNotIn("cost", out["candidates"][0])
        self.assertNotIn("holdings_detail", out)
        out2 = publish.strip_owner_fields(data, is_owner=True)
        self.assertIn("cost", out2["candidates"][0])

    def test_verify_site_redline(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "data"))
            open(os.path.join(td, "data.js"), "w").write("var x=1")   # 明文红线！
            issues = publish.verify_site(td)
            self.assertTrue(any("data.js" in i for i in issues), "明文 data.js 必须红线终止")


class TestFuseAndPicks(unittest.TestCase):
    def test_winrate_fuse(self):
        with tempfile.TemporaryDirectory() as td:
            con = get_conn(os.path.join(td, "t.db"))
            for i in range(12):
                con.execute("INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (f"2026-08-{i+1:02d}", f"60000{i%10}", "票", "波段",
                             "现在买", 1, 2, 1, 2, 2, 60, "lose", None))
            con.commit()
            wr = scoring.tag_winrate(con, days=90, min_n=10, today="2026-09-12")
            self.assertTrue(wr["波段"]["observe"], "胜率<45% 且样本≥10 → observe")
            con.close()
    def test_top_picks_rules(self):
        cands = []
        for i in range(5):
            cands.append(_mk_cand(code=f"60{i:04d}", pool="连板", score=90 + i,
                                  tag="连板", eff_probe=i))
        env_w = {"连板": 1.0, "趋势": 1.0, "波段": 1.0, "区间": 1.0}
        picks = scoring.compute_top_picks(cands, env_w, {})
        self.assertLessEqual(len(picks), 3, "TOP3 上限")
        self.assertLessEqual(sum(1 for p in picks if p["pool"] == "连板"), 2,
                             "连板席位 ≤2")

    def test_env_bias(self):
        w = scoring.env_bias(0.60, 0.25, 65)
        self.assertEqual(w["连板"], 1.25)
        self.assertEqual(w["趋势"], 1.15)
        w2 = scoring.env_bias(0.30, 0.45, 30)
        self.assertEqual(w2["连板"], 0.70 * 0.85)
        # 退潮不抬波段/区间
        self.assertEqual(w2["波段"], 1.0)
        self.assertEqual(w2["区间"], 1.0)

    def test_tech_lock_static(self):
        """技巧只增不减：TECHNIQUES 基线守门的静态等价断言。"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        eng = open(os.path.join(base, "pipeline", "engines.py"), encoding="utf-8").read()
        for fn in ("screen_uptrend", "detect_stage_bottom", "classify_box_speed",
                   "ladderplan_plan", "screen_pullback_relay", "kronos_lite",
                   "entry_plan", "auction_discipline"):
            self.assertIn(f"def {fn}", eng, f"引擎 {fn} 缺失——技巧只增不减红线")


if __name__ == "__main__":
    unittest.main(verbosity=1)
