# -*- coding: utf-8 -*-
"""审计升级回归（222.docx M/N 项 + 333.docx 九场景 + 升级-4 口径纪律）。"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import decisions, emotion, executor, notifier, quality  # noqa: E402
from pipeline import build as bld  # noqa: E402
from pipeline.core import get_conn  # noqa: E402


def put_kline(con, d, code, o, h, l, c, pct=None):
    con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, d, o, h, l, c, 1e6, None, pct, None))


class TestQuality(unittest.TestCase):
    def test_m02_amount_graded(self):
        # 单位错误 → 阻断
        lv, act, _ = quality.grade_total_amount(2.5e11 / 1e4)
        self.assertEqual((lv, act), ("block", "block"))
        # 历史分布内 → 放行
        lv, act, _ = quality.grade_total_amount(1.5e12)
        self.assertEqual((lv, act), ("ok", "allow"))
        # 超分布 → 告警复核（不直接放行也不阻断）
        lv, act, _ = quality.grade_total_amount(9e12)
        self.assertEqual((lv, act), ("warn", "review"))
        # 多源确认的极端真实行情 → 放行但留痕
        lv, act, _ = quality.grade_total_amount(9e12, multi_source_confirmed=True)
        self.assertEqual(act, "allow")
        # 盘中累计：不套用全日下限
        lv, act, _ = quality.grade_total_amount(2e11, intraday=True)
        self.assertEqual(act, "allow")

    def test_m03_repair_units(self):
        self.assertIsNone(quality.est_float_shares(1e6, 0),
                          "换手率为零不计算（M03）")
        self.assertIsNone(quality.est_float_shares(1e6, None))
        self.assertAlmostEqual(quality.est_float_shares(1e6, 0.01), 1e8)
        # 真实流通股本 = 流通市值÷股价；量纲锚 q=量/股本
        rows = [(f"60000{i}", 5e8) for i in range(10)]   # 源异常返「股」
        fs = {f"60000{i}": 1e9 for i in range(10)}       # 流通股本 10 亿股
        factor, records = quality.repair_volume_units(rows, fs)
        self.assertEqual(factor, 100.0, "全市场量纲失灵 → ÷100 修复建议")
        self.assertGreater(len(records), 0)
        for r in records:      # 修复记录必须含原值/新值/依据/版本
            self.assertIn("basis", r)
            self.assertIn("rule_version", r)
            self.assertEqual(r["old"], 5e8)
            self.assertAlmostEqual(r["new"], 5e6)
        # 正常量纲（手）：q ≈ 换手率/100 < 0.01 → 不触发
        ok_rows = [(f"60000{i}", 3e5) for i in range(10)]
        factor2, _ = quality.repair_volume_units(ok_rows, fs)
        self.assertEqual(factor2, 1.0)

    def test_n02_batch_record(self):
        with tempfile.TemporaryDirectory() as td:
            con = get_conn(os.path.join(td, "t.db"))
            rec = quality.batch_record("test", "2026-09-12", caliber="qfq")
            quality.write_batch_meta(con, rec)
            row = con.execute("SELECT source, quality FROM batch_meta "
                              "WHERE batch_id=?", (rec["batch_id"],)).fetchone()
            self.assertEqual(row, ("test", "ok"))
            con.close()

    def test_m04_cross_confirm_and_quarantine(self):
        with tempfile.TemporaryDirectory() as td:
            con = get_conn(os.path.join(td, "t.db"))
            put_kline(con, "2026-09-11", "sh000001", 1, 2, 1, 2)
            # 快照 pct 全零 → 休市嫌疑
            con.execute("INSERT OR REPLACE INTO snapshot VALUES("
                        "'2026-09-11','sh600000','x',10,0,1e8,1,1e9)")
            ok, why = __import__("pipeline.core", fromlist=["is_trading_day_cross"]) \
                .is_trading_day_cross(con, "2026-09-11")
            self.assertFalse(ok, "pct 全零 → 交叉确认不通过")
            # 隔离备份先于删除
            path = quality.quarantine_records(con, ["2026-09-11"],
                                              out_dir=os.path.join(td, "q"))
            self.assertTrue(os.path.exists(path))
            con.close()


class TestEmotion(unittest.TestCase):
    def test_m06_anchor_interpolation(self):
        # 反向维度：跌停 30→0 分、8→50、0→100（M06）
        a = [p for p in emotion.TEN_DIMS if p["key"] == "dt_count"][0]["anchors"]
        self.assertEqual(emotion.anchor_score(30, a), 0)
        self.assertEqual(emotion.anchor_score(8, a), 50)
        self.assertEqual(emotion.anchor_score(0, a), 100)
        self.assertAlmostEqual(emotion.anchor_score(19, a), 25.0)
        # 区间外截断
        self.assertEqual(emotion.anchor_score(50, a), 0)

    def test_m07_missing_dims(self):
        with tempfile.TemporaryDirectory() as td:
            con = get_conn(os.path.join(td, "t.db"))
            out = emotion.emotion_ten(con, "2026-09-12", extra_dims={
                "zt_count": 80, "promote": 25, "money_effect": 1.0,
                "max_streak": 5, "up_ratio": 60, "dt_count": 2,
                "zha_count": 3, "amt_chg": 5, "seal_rate": 80, "heat": 1.1},
                write_log=False)
            self.assertEqual(out["effective"], 10)
            self.assertTrue(out["qualified"])
            # 缺一半维度：分数变化但缺失的不补中性
            out2 = emotion.emotion_ten(con, "2026-09-12", extra_dims={
                "zt_count": 80, "promote": 25}, write_log=False)
            self.assertEqual(out2["effective"], 2)
            self.assertFalse(out2["qualified"], "覆盖率不足 → 不用于策略加权")
            self.assertIsNone(out2["parts"]["money_effect"]["score"],
                              "缺失维度不得补中性值")
            con.close()

    def test_m05_normalization(self):
        # 单维度满分时：score = 100×w/Σ有效w = 100（加权平均 M05）
        a = emotion.TEN_DIMS[0]["anchors"]
        self.assertEqual(emotion.anchor_score(6, a), 100)

    def test_m08_phase(self):
        self.assertEqual(emotion.market_phase(
            [("d1", 50), ("d2", 55), ("d3", 62)]), "发酵")
        self.assertEqual(emotion.market_phase(
            [("d1", 70), ("d2", 60), ("d3", 50)]), "退潮")
        self.assertEqual(emotion.market_phase([("d1", 30)]), "不可判")


class TestDecisions(unittest.TestCase):
    def _cand(self, **kw):
        c = {"code": "600100", "name": "示例", "pool": "趋势", "close": 10.0,
             "buy_low": 9.9, "buy_high": 10.05, "stop": 9.4, "score": 70,
             "action": "现在买", "is_st": False, "consecutive_limit_ups": 0,
             "fmv": 80e8}
        c.update(kw)
        return c

    def test_m11_grade_vs_status(self):
        # 评级 B ≠ 现在买：exec_status 独立
        g, s = decisions.research_grade(
            self._cand(action="等回踩", consecutive_limit_ups=3, gap_pct=None))
        self.assertEqual(g, "B")
        d = decisions.make_decision(self._cand(action="等回踩",
                                               consecutive_limit_ups=3),
                                    "2026-09-12")
        self.assertEqual(d["exec_status"], "等待确认",
                         "评级高不等于'现在买'（M11）")

    def test_m12_missing_fields(self):
        g, s = decisions.research_grade(self._cand(fmv=None),
                                        missing_fields=("fmv",))
        self.assertEqual((g, s), ("X", "数据不足"),
                         "必要字段缺失不得按 B 兜底")

    def test_m13_boundary_5pct(self):
        c = self._cand(pool="连板", consecutive_limit_ups=3, gap_pct=5.0)
        g, _ = decisions.research_grade(c)
        self.assertEqual(g, "A", "M13：5.00% 属于'高开≥5%'档")
        c2 = self._cand(pool="连板", consecutive_limit_ups=3, gap_pct=4.999)
        g2, _ = decisions.research_grade(c2)
        self.assertNotEqual(g2, "A")

    def test_lifecycle_advance(self):
        with tempfile.TemporaryDirectory() as td:
            con = get_conn(os.path.join(td, "t.db"))
            d0 = "2026-09-10"
            sig = {"signal_id": decisions.signal_id("trend_close", "600100", d0),
                   "code": "sh600100", "strategy": "trend_close",
                   "rule_version": decisions.RULE_VERSION,
                   "created_at": d0, "data_date": d0, "status": "等待确认",
                   "zone_low": 9.9, "zone_high": 10.05, "stop": 9.4,
                   "invalid_if": "", "valid_until": "2026-09-30",
                   "reason": "", "status_reason": "", "changed_at": ""}
            con.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        tuple(sig.values()))
            # 情形1：收盘回到区间 → 条件满足
            ch = decisions.advance_signals(
                con, "2026-09-11", lambda c: (10.0, 9.8, 10.2))
            self.assertEqual(ch[0]["new"], "条件满足")
            # 情形2：超价取消
            con.execute("UPDATE signals SET status='等待确认'")
            ch = decisions.advance_signals(
                con, "2026-09-11", lambda c: (10.6, 10.2, 10.7))
            self.assertEqual(ch[0]["new"], "超价取消")
            # 情形3：跌破止损 → 结构失效
            con.execute("UPDATE signals SET status='等待确认'")
            ch = decisions.advance_signals(
                con, "2026-09-11", lambda c: (9.3, 9.2, 9.5))
            self.assertEqual(ch[0]["new"], "结构失效")
            # 情形4：双触发（盘中触止损+收盘回区间）→ 保守失效，不伪造顺序
            con.execute("UPDATE signals SET status='等待确认'")
            ch = decisions.advance_signals(
                con, "2026-09-11", lambda c: (10.0, 9.3, 10.2))
            self.assertEqual(ch[0]["new"], "结构失效")
            self.assertIn("顺序不可知", ch[0]["reason"])
            # 情形5：数据缺失 → 数据不足
            con.execute("UPDATE signals SET status='等待确认'")
            ch = decisions.advance_signals(con, "2026-09-11", lambda c: None)
            self.assertEqual(ch[0]["new"], "数据不足")
            # 重复运行：状态已终态 → 无新变化（无变化不重复提醒）
            ch2 = decisions.advance_signals(con, "2026-09-11", lambda c: None)
            self.assertEqual(ch2, [])
            con.close()


class TestExecutorRisk(unittest.TestCase):
    def _con(self, today="2026-09-11"):
        con = get_conn(":memory:")
        put_kline(con, "2026-09-10", "sh600100", 9.9, 10.1, 9.8, 10.0)
        put_kline(con, today, "sh600100", 10.0, 10.2, 9.9, 10.1)
        return con, today

    def test_m26_t1_by_batch(self):
        con, today = self._con()
        executor.ensure_account(con, today)
        # 昨日买入批次可卖、今日批次不可卖（同票共存）
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES('sh600100','2026-09-10',100,10,"
                    "100,'sim')")
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES('sh600100','2026-09-11',100,10.1,"
                    "0,'sim')")
        self.assertEqual(executor.available_qty(con, "sh600100", today), 100)
        oid, st, _ = executor.place_order(con, "sh600100", "sell", 150, 10.1,
                                          today)
        self.assertEqual(st, "rejected", "T+1 按批次：150 > 可卖 100")
        oid, st, _ = executor.place_order(con, "sh600100", "sell", 100, 10.1,
                                          today)
        self.assertEqual(st, "filled")
        # FIFO：昨日批次卖光被清理；今日批次保留（qty=100）但不可卖（available=0）
        q, a = con.execute("SELECT SUM(qty), COALESCE(SUM(available),0) "
                           "FROM position_batches").fetchone()
        self.assertEqual(q, 100, "当日批次必须保留（明日解锁，M26）")
        self.assertEqual(a, 0, "卖出后可卖数量归零")
        con.close()

    def test_m21_cumulative_exposure(self):
        con, today = self._con()
        executor.ensure_account(con, today)
        # 50000 元 = 50%（单笔在 1000~60000 内且 <70% 上限）
        oid, st, why = executor.place_order(con, "sh600100", "buy", 5000, 10.0,
                                            today)
        self.assertEqual(st, "filled")
        # 加仓后累计 75000/100000 = 75% > 70% → 拒（M21 按累计敞口）
        oid, st, why = executor.place_order(con, "sh600100", "buy", 2500, 10.0,
                                            today)
        self.assertEqual(st, "rejected")
        self.assertIn("累计敞口", why)
        con.close()

    def test_m22_m23_halt_and_sell_exempt(self):
        con, today = self._con()
        executor.ensure_account(con, today)
        con.execute("UPDATE account_state SET frozen=1 WHERE id=1")
        oid, st, why = executor.place_order(con, "sh600100", "buy", 1000, 10.0,
                                            today)
        self.assertEqual(st, "rejected", "熔断锁定不开新仓（M22）")
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES('sh600100','2026-09-10',1000,10,"
                    "1000,'sim')")
        oid, st, _ = executor.place_order(con, "sh600100", "sell", 1000, 10.1,
                                          today, risk_sell=True)
        self.assertEqual(st, "filled", "必要退出不受熔断/频率限制（M23）")
        con.close()

    def test_m27_limit_down_unsellable(self):
        con, today = self._con()
        executor.ensure_account(con, today)
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES('sh600100','2026-09-10',1000,10,"
                    "1000,'sim')")
        oid, st, why = executor.place_order(con, "sh600100", "sell", 1000,
                                            9.0, today, prev_close=10.0)
        self.assertEqual(st, "rejected")
        self.assertIn("跌停", why, "跌停卖不出：记录已触发未成交（M27）")
        con.close()

    def test_m24_m25_n07_exit_rules(self):
        con, today = self._con()
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES('sh600100','2026-09-10',1000,20,"
                    "1000,'sim')")
        # 持仓成本 20，现价 10.1 → 浮亏 -49.5%：硬止损与趋势止损同时触发，
        # 优先级 hard_stop 第一，且原因全量记录（M24）
        action, reasons, _ = executor.evaluate_exit(con, "sh600100", today)
        self.assertEqual(action, "SELL")
        self.assertIn("普通硬止损", reasons)
        self.assertIn("趋势/波段止损", reasons)
        self.assertEqual(reasons[0], "普通硬止损")
        # M25：日涨幅 +5% 但持仓亏损 → 不标止盈（止盈只看持仓收益）
        con.execute("UPDATE klines SET c=10.5, h=10.6 WHERE code='sh600100' "
                    "AND date=?", (today,))
        action, reasons, _ = executor.evaluate_exit(con, "sh600100", today)
        self.assertNotIn("持仓浮盈止盈", reasons)
        # N07：ATR 保护线只收紧
        p1 = executor.evaluate_exit(con, "sh600100", today, protect_prev=None)
        p2 = executor.evaluate_exit(con, "sh600100", today, protect_prev=99.0)
        self.assertIn("SELL", (p2[0],), "传入更高保护线仍应触发")
        con.close()

    def test_account_net_value(self):
        con, today = self._con()
        executor.ensure_account(con, today)
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES('sh600100','2026-09-10',1000,10,"
                    "1000,'sim')")
        con.execute("UPDATE account_state SET cash=50000 WHERE id=1")
        eq = executor.equity(con)
        self.assertAlmostEqual(eq, 50000 + 1000 * 10.1, places=1,
                               msg="净值 = 现金 + 持仓市值（M32）")
        con.close()


class TestNotifierM37(unittest.TestCase):
    """WxPusher 主通道架构下的 M36/M37 纪律（多账户 + 备用通道）。"""

    def _live(self):
        orig = notifier.load_config
        notifier.load_config = lambda: {
            "push_dry_run": False, "serverchan_key": "fake-sc",
            "wxpusher_accounts": [{"name": "A", "app_token": "AT1",
                                   "uids": ["U1"]}],
            "wxpusher_routes": {"*": ["A"]}}
        return orig

    def test_uncertain_no_blind_retry(self):
        import pipeline.wxpusher as wx
        orig = self._live()
        o_wxsend, o_accounts = wx.send, wx.load_accounts
        with tempfile.TemporaryDirectory() as td:
            notifier.DIST_LEDGER = os.path.join(td, "l.json")
            con = get_conn(os.path.join(td, "t.db"))
            try:
                wx.load_accounts = lambda: notifier.load_config()["wxpusher_accounts"]
                wx.send = lambda acct, t, c, timeout=12: ("uncertain", "timeout")
                r = notifier.push("m", "t", "600000", date="2026-09-12", con=con)
                self.assertEqual(r["status"], "uncertain")
                self.assertNotIn("serverchan", r["results"],
                                 "受理不确定不盲目双发（M37）")
                # 全部账户明确失败 → ServerChan 备用补发（M36）
                wx.send = lambda acct, t, c, timeout=12: ("failed", "400")
                notifier._send_serverchan = lambda *a: ("sent", "ok")
                r2 = notifier.push("m2", "t", "600001", date="2026-09-12", con=con)
                self.assertEqual(r2["results"]["serverchan"]["role"], "fallback")
                self.assertEqual(r2["status"], "sent")
            finally:
                wx.send = o_wxsend
                wx.load_accounts = o_accounts
                notifier.load_config = orig
            con.close()

    def test_force_bypass_dedup_for_risk(self):
        import pipeline.wxpusher as wx
        orig = self._live()
        o_wxsend, o_accounts = wx.send, wx.load_accounts
        with tempfile.TemporaryDirectory() as td:
            notifier.DIST_LEDGER = os.path.join(td, "l.json")
            con = get_conn(os.path.join(td, "t.db"))
            try:
                wx.load_accounts = lambda: notifier.load_config()["wxpusher_accounts"]
                wx.send = lambda acct, t, c, timeout=12: ("sent", "ok")
                r1 = notifier.push("risk", "t", "600000", date="2026-09-12",
                                   con=con, force=True)
                r2 = notifier.push("risk", "t", "600000", date="2026-09-12",
                                   con=con, force=True)
                self.assertTrue(r1["sent"] and r2["sent"],
                                "重要退出风险不被普通去重拦截")
            finally:
                wx.send = o_wxsend
                wx.load_accounts = o_accounts
                notifier.load_config = orig
            con.close()

    def test_n11_redact(self):
        fake = "MY_FAKE_SENDKEY_12345"
        msg = f"https://sctapi.ftqq.com/{fake}.send?token=abc"
        out = core_redact_helper(msg, fake)
        self.assertNotIn(fake, out)
        self.assertNotIn("token=abc", out)
        self.assertIn("***", out)


def core_redact_helper(text, secret):
    from pipeline.core import redact
    return redact(text, secret)


class TestRender(unittest.TestCase):
    def test_m35_brief_and_n10_card(self):
        d = {"code": "600100", "name": "示例", "status": "等待确认",
             "zone": [9.9, 10.05], "stop": 9.4, "invalid_if": "跌破止损 9.40",
             "valid_until": "2026-09-18", "reason": "回踩MA5企稳",
             "research_grade": "B", "score": 72}
        html = notifier.render_brief("2026-09-12", d, [d],
                                     [{"code": "600000", "old": "等待确认",
                                       "new": "超价取消", "reason": "超上限"}],
                                     {"reviewed": 120, "data_date": "2026-09-12",
                                      "valid_until": "2026-09-18"})
        for need in ("首选观察", "备选观察", "计划变化", "数据说明",
                     "不追价上限", "失效条件", "超价取消"):
            self.assertIn(need, html)
        self.assertLess(len(re.sub(r"<[^>]+>", "", html)), 2500,
                        "主报告应简洁（M35：300~600字目标量级）")

    def test_m38_authenticated_encryption(self):
        blob = __import__("pipeline.publish", fromlist=["encrypt_bytes"]) \
            .encrypt_bytes(b'{"a":1}', "口令X")
        pub = sys.modules["pipeline.publish"]
        self.assertEqual(pub.decrypt_bytes(blob, "口令X"), b'{"a":1}')
        with self.assertRaises(Exception):
            pub.decrypt_bytes(blob[:-1] + bytes([blob[-1] ^ 1]), "口令X"), \
                "密文被篡改必须失败（M38 完整性校验）"
        # auth.js 同步静态断言
        js_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "site_template", "auth.js")
        with open(js_path, encoding="utf-8") as f:
            js = f.read()
        self.assertTrue("tag mismatch" in js or "口令错误" in js)
        self.assertIn("PBKDF2", js)
        self.assertIn("512", js, "派生 64 字节（加密+MAC 双密钥）")


import re  # noqa: E402  （render 断言用）


if __name__ == "__main__":
    unittest.main(verbosity=1)
