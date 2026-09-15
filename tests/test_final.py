# -*- coding: utf-8 -*-
"""终验回归：扫描池过滤 / 连板池 / 竞价裁决 / T+2 结局回填 / 推送通道纪律。"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import build as bld, mood, notifier, scoring  # noqa: E402
from pipeline.core import get_conn  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATES = [(date(2026, 9, 11) - timedelta(days=39 - i)).isoformat()
         for i in range(40)]          # 40 个自然日，最后一天 = 2026-09-11


def put_kline(con, d, code, o, h, l, c):
    con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, d, o, h, l, c, 1e6, None, None, None))


def mk_uptrend(con, code, tag):
    """40 日多头排列上涨（测试引擎已验证可检出）。末日按 tag 收阳/涨停。"""
    c = 10.0
    for i in range(39):
        pct = 2.5 if i % 5 != 4 else -0.5
        c *= 1 + pct / 100
        put_kline(con, DATES[i], code, c * 0.995, c * 1.008, c * 0.992, c)
    last = c * (1.10 if tag == "limit" else 1.025)
    put_kline(con, DATES[-1], code, c * 0.995, last * 1.008, c * 0.992, last)


def mk_con():
    con = get_conn(":memory:")
    for d in DATES:
        put_kline(con, d, "sh000001", 3000, 3010, 2995, 3005)
    mk_uptrend(con, "sh600100", "uptrend")     # 正常趋势票
    mk_uptrend(con, "sh600200", "uptrend")     # ST 票（应被过滤）
    mk_uptrend(con, "sh600300", "uptrend")     # 低成交额（应被过滤）
    mk_uptrend(con, "sh600400", "limit")       # 当日涨停（归连板池）
    # 快照：名称/成交额/市值
    rows = [("sh600100", "正常票", 5e8, 2.0, 30e8),
            ("sh600200", "*ST劣票", 5e8, 2.0, 30e8),
            ("sh600300", "穷票", 5e7, 2.0, 30e8),
            ("sh600400", "涨停票", 5e8, 5.0, 30e8)]
    con.executemany("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
                    [("2026-09-11", c, n, 10.0, 1.0, a, t, f)
                     for c, n, a, t, f in rows])
    # 昨日 600400 涨停（今日再板 → mood 递推 streak=2）
    con.execute("INSERT OR REPLACE INTO zt_pool VALUES('2026-09-10','sh600400',1,'涨停票')")
    con.commit()
    return con


class TestScan(unittest.TestCase):
    def setUp(self):
        self.con = mk_con()
        mood.compute_mood(self.con, "2026-09-11")   # 落库今日涨停池

    def test_filters_and_pools(self):
        cands, skipped = bld.scan_all(self.con, "2026-09-11")
        codes = {c["code"]: c for c in cands}
        self.assertIn("sh600100", codes, "正常票应入池")
        self.assertEqual(codes["sh600100"]["name"], "正常票")
        self.assertEqual(codes["sh600100"]["pool"], "趋势")
        self.assertIn("sh600400", codes, "涨停票应入连板池")
        self.assertEqual(codes["sh600400"]["pool"], "连板")
        self.assertEqual(codes["sh600400"]["streak"], 2, "昨日涨停+今日涨停=2板")
        self.assertNotIn("sh600200", codes, "*ST 票必须被名称过滤")
        self.assertNotIn("sh600300", codes, "成交额<1.2亿必须被过滤")
        reasons = " ".join(s["reason"] for s in skipped)
        self.assertIn("ST", reasons)
        self.assertIn("成交额", reasons)
        self.con.close()

    def test_ladder_decide_and_adjudicate(self):
        cands, _ = bld.scan_all(self.con, "2026-09-11")
        ladder = next(c for c in cands if c["pool"] == "连板")
        # 收盘时点 gap 未知 → 次日竞价达标买
        self.assertEqual(scoring._decide(ladder), "次日竞价达标买")
        # 竞价时点：st=2 高开 6% → 达标买；高开 3% → 观望；低开 → 禁买
        ladder2 = dict(ladder)
        ladder2["gap_pct"] = 6.0
        self.assertEqual(scoring._decide(ladder2), "现在买")
        ladder3 = dict(ladder)
        ladder3["gap_pct"] = 3.0
        self.assertEqual(scoring._decide(ladder3), "观望")
        ladder4 = dict(ladder)
        ladder4["gap_pct"] = -2.5
        self.assertEqual(scoring._decide(ladder4), "禁买")
        self.con.close()


class TestOutcomes(unittest.TestCase):
    def test_t2_fill(self):
        con = mk_con()
        days = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM klines WHERE code='sh000001' ORDER BY date")]
        t0, d2 = days[-3], days[-1]
        con.execute("INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t0, "sh600100", "票A", "趋势", "现在买",
                     1, 2, 1, 2, 2, 60, "", None))
        con.execute("INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t0, "sh600400", "票B", "连板", "现在买",
                     1, 2, 1, 2, 2, 60, "", None))
        # 600400 今日（T+2）收盘暴跌 → 相对 T0 收盘 lose
        con.execute("UPDATE klines SET c=5.0 WHERE code='sh600400' AND date=?", (d2,))
        con.commit()
        n = bld.fill_outcomes(con, d2)
        self.assertGreaterEqual(n, 2)
        rows = dict((r[0], r[1]) for r in con.execute(
            "SELECT code, outcome FROM rec_picks WHERE date=?", (t0,)))
        self.assertEqual(rows["sh600100"], "win")
        self.assertEqual(rows["sh600400"], "lose")
        con.close()


class TestChannels(unittest.TestCase):
    def test_wxpusher_only_discipline(self):
        """模拟盘/盘中异动只走 WxPusher 单通道（额度纪律的现形态）。"""
        import pipeline.wxpusher as wx
        with tempfile.TemporaryDirectory() as td:
            notifier.DIST_LEDGER = os.path.join(td, "ledger.json")
            con = get_conn(os.path.join(td, "t.db"))
            o_accounts = wx.load_accounts
            o_send = wx.send
            o_cfg = notifier.load_config
            wx.load_accounts = lambda: [{"name": "A", "app_token": "AT",
                                         "uids": ["U"]}]
            # 2026-09-15：必须 mock 实际发送。原用例未 mock → 走真实 HTTP
            # 且无凭据必失败；旧代码无条件报 sent=True 掩盖了这点，sent 语义
            # 收紧后暴露。本用例考察「通道路由」，须固定发送结果。
            wx.send = lambda acct, t, c, timeout=12: ("sent", "ok")
            # 同理固定配置：CI 无 config/notify.json 时通道列表为空。
            notifier.load_config = lambda *a, **k: {
                "push_dry_run": False, "primary_channel": "wxpusher",
                "push_tag": "Test"}
            try:
                r = notifier.push("intraday", "盘中异动", "候选 600000",
                                  date="2026-09-12", con=con,
                                  channels=("wxpusher",))
                self.assertTrue(r["sent"])
                self.assertEqual(set(r["results"]), {"wxpusher:A"},
                                 "盘中异动只走 WxPusher，不占备用通道")
                r2 = notifier.push("intraday", "盘中异动", "候选 600000",
                                   date="2026-09-12", con=con)
                self.assertTrue(r2.get("dedup"))
            finally:
                wx.load_accounts = o_accounts
                wx.send = o_send
                notifier.load_config = o_cfg
                # 2026-09-15：必须先关连接再退出 TemporaryDirectory。
                # 原实现 con.close() 在 with 块结束后才执行，导致 Windows 上
                # TemporaryDirectory 清理 t.db 时遇 WinError 32（文件被占用）。
                con.close()

    def test_candidate_line_ladder(self):
        c = {"code": "600400", "name": "涨停票", "pool": "连板", "streak": 2,
             "close": 12.1, "buy_low": 12.04, "buy_high": 12.46,
             "sell_low": 12.5, "sell_high": 12.9, "stop": 11.13,
             "score": 70, "action": "次日竞价达标买", "hot_pick": True,
             "position": "1~2成"}
        line = notifier._cand_line(c)
        self.assertIn("2板", line)
        self.assertIn("🔥优选", line)
        self.assertLessEqual(len(line), notifier.CAND_LINE_CAP)


if __name__ == "__main__":
    unittest.main(verbosity=1)
