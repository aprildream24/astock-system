# -*- coding: utf-8 -*-
"""盘前/竞价「筛选快照口径」回归锁 —— 2026-09-16 血案。

事故：09-16 用户全天只收到一条消息，且那一条是**候选 0 只的空计划**。
    · 08:50 pre   ：CI 日志「宇宙0只 / 覆盖0.0% / 候选0 / 剔除4937」，
                    推送另因 PushPlus SSL 握手超时**彻底没发出去**；
    · 09:25 auction：CI 日志「宇宙4573只 / 覆盖100.0% / 候选0 / 剔除4944」，
                    推送成功但内容为空壳。
根因（本地受控复现逐条吻合 CI 数字）：
    盘前/竞价的筛选口径走了**当日快照**。当日快照此刻的成交额要么全 0
    （08:50 集合竞价未开始）要么只有撮合额（09:25 全市场约 114 亿）：
      · split_universe 判「无成交 ⇒ 停牌/退市」⇒ 全市场判死 → 宇宙 0；
      · scan_all 的「成交额<1.2亿」门槛 ⇒ 4403 只被剔除 → 候选 0。
修法：bar_anchor（上一交易日）同时作为**快照口径基准日**
    （snap_date = bar_anchor or date），停牌判定传 asof=snap_date；
    闸门 `_preauction_ready` 增加「锚定日快照必须有真实成交额」校验；
    推送侧加传输层重试 + 「绝不推空壳」保险丝。
"""
import os
import sqlite3
import ssl
import sys
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import build as bld          # noqa: E402
from pipeline import notifier              # noqa: E402

PREV = "2026-09-15"
DATE = "2026-09-16"


def _mkdb(day_amt, prev_amt, n=200):
    """构造内存库：prev 日 K线+快照、当日快照。"""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE klines(code TEXT, date TEXT, o REAL, h REAL,"
                " l REAL, c REAL, v REAL)")
    con.execute("CREATE TABLE snapshot(date TEXT, code TEXT, name TEXT,"
                " price REAL, pct REAL, amt REAL, turn REAL, fmv REAL)")
    con.execute("INSERT INTO klines VALUES('sh000001',?,1,1,1,1,1)", (PREV,))
    codes = [(f"sh6000{i:02d}", "x") for i in range(n)]
    if prev_amt is not None:
        con.executemany("INSERT INTO snapshot VALUES(?,?,?,0,0,?,0,0)",
                        [(PREV, c, nm, prev_amt) for c, nm in codes])
    if day_amt is not None:
        con.executemany("INSERT INTO snapshot VALUES(?,?,?,0,0,?,0,0)",
                        [(DATE, c, nm, day_amt) for c, nm in codes])
    con.commit()
    return con


class TestSnapshotScopeIsAnchored(unittest.TestCase):
    """核心：盘前/竞价的量能判定必须落在锚定日（上一交易日）。"""

    def test_split_universe_accepts_asof(self):
        import inspect
        sig = inspect.signature(bld.split_universe)
        self.assertIn("asof", sig.parameters, "split_universe 必须支持 asof")
        self.assertIsNone(sig.parameters["asof"].default,
                          "asof 默认 None（收盘路径行为不变）")

    def test_scan_all_binds_snapshot_to_bar_anchor(self):
        """scan_all 必须把 bar_anchor 同时当作快照口径基准日。"""
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("snap_date = bar_anchor or date", src,
                      "盘前快照口径必须锚定上一交易日")
        self.assertIn("asof=snap_date", src,
                      "split_universe 必须收到锚定基准日")

    def test_offline_control_group(self):
        """离线对照：同一份数据，两种口径给出相反结论。

        用当日快照（旧行为）⇒ 全市场判「停牌」；
        用锚定日快照（修复后）⇒ 正常存活。
        """
        con = _mkdb(day_amt=0.0, prev_amt=2.0e8)
        self.addCleanup(con.close)
        day_snap = bld._snapshot(con, DATE)
        prev_snap = bld._snapshot(con, PREV)

        alive_old, dead_old = bld.split_universe(
            con, DATE, day_snap, asof=DATE)
        self.assertEqual(len(alive_old), 0,
                         "当日成交额全 0 ⇒ 必须复现出「宇宙 0」")
        self.assertGreater(len(dead_old), 0)

        alive_new, dead_new = bld.split_universe(
            con, DATE, prev_snap, asof=PREV)
        self.assertGreater(len(alive_new), 0,
                           "锚定日快照有量 ⇒ 标的必须存活（修复生效）")
        self.assertEqual(len(dead_new), 0)

    def test_snapshot_date_for_skips_zero_amount_days(self):
        """站点补算口径：跳过无成交额的当日，回退到最近有量日。"""
        con = _mkdb(day_amt=0.0, prev_amt=2.0e8)
        self.addCleanup(con.close)
        self.assertEqual(bld._snapshot_date_for(con, DATE), PREV)
        # 当日有量时（收盘后）就用当日
        con2 = _mkdb(day_amt=3.0e8, prev_amt=2.0e8)
        self.addCleanup(con2.close)
        self.assertEqual(bld._snapshot_date_for(con2, DATE), DATE)

    def test_coverage_snapshot_uses_effective_snap_date(self):
        con = _mkdb(day_amt=0.0, prev_amt=2.0e8)
        self.addCleanup(con.close)
        bld.LAST_SCAN_COVERAGE.clear()
        cov = bld.coverage_snapshot(con, DATE)
        self.assertGreater(cov["universe"], 0,
                           "盘前站点补算不得算出宇宙 0")
        self.assertEqual(cov["snap_date"], PREV)


class TestPushTransportRetry(unittest.TestCase):
    """推送传输层失败必须重试（血案：一次 SSL 握手超时 = 零送达）。"""

    def test_transport_failed_classification(self):
        self.assertTrue(notifier._transport_failed(
            ssl.SSLError("The handshake operation timed out")),
            "TLS 握手失败 = 未送达，必须可重试")
        self.assertTrue(notifier._transport_failed(
            ConnectionResetError("reset")))
        self.assertFalse(notifier._transport_failed(TimeoutError("read t/o")),
                         "读超时可能已受理，不得盲目重试双发")

    def test_pushplus_retries_then_succeeds(self):
        calls = {"n": 0}

        def fake(req, timeout=10):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ssl.SSLError("The handshake operation timed out")

            class R:
                status = 200
            return R()

        with mock.patch.object(notifier.urllib.request, "urlopen", fake), \
                mock.patch.object(notifier.time, "sleep", lambda s: None):
            st, detail = notifier._send_pushplus("tok", "标题", "<p>x</p>")
        self.assertEqual(st, "sent", f"重试后应送达，实际 {st}/{detail}")
        self.assertEqual(calls["n"], 3, "应恰好重试到第 3 次成功")

    def test_pushplus_4xx_does_not_retry(self):
        calls = {"n": 0}

        def fake(req, timeout=10):
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

        with mock.patch.object(notifier.urllib.request, "urlopen", fake), \
                mock.patch.object(notifier.time, "sleep", lambda s: None):
            st, _ = notifier._send_pushplus("tok", "t", "x")
        self.assertEqual(st, "failed")
        self.assertEqual(calls["n"], 1, "4xx 是确定性拒绝，不得重试")


class TestNoEmptyPlanSentinel(unittest.TestCase):
    """最后一道闸：口径异常时绝不推空壳。"""

    def test_sentinel_present_in_build(self):
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_u == 0 or (_ncand == 0 and _covp < 90)", src,
                      "必须保留「宇宙 0 / 候选 0 且覆盖不达标」保险丝")

    def test_scope_rejects_empty_snapshot(self):
        """锚定日快照无成交额 ⇒ _preauction_ready 必须拒绝。"""
        con = _mkdb(day_amt=0.0, prev_amt=None)
        self.addCleanup(con.close)
        ok, why = bld._preauction_ready(con, DATE)
        self.assertFalse(ok)
        self.assertIn("无有效快照", why)

    def test_scope_accepts_when_anchor_has_amount(self):
        con = _mkdb(day_amt=0.0, prev_amt=2.0e8)
        self.addCleanup(con.close)
        ok, why = bld._preauction_ready(con, DATE)
        self.assertTrue(ok, f"锚定日有量 + 当日快照在 ⇒ 必须放行，实际 {why}")


class TestFreshnessSemantics(unittest.TestCase):
    """新鲜度语义：只有「比锚定日更旧」才算陈旧。

    血案同日发现：原 `rows[-1][0] != expected_bar` 把「已拿到当日实时K线」
    的票也判成陈旧（CI 日志「数据新鲜0 陈旧19」即此），误导定位方向。
    """

    def test_only_older_bar_is_stale(self):
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("if rows[-1][0] < expected_bar:", src,
                      "新鲜度必须用「更旧」判定，不能用「不等于」")

    def test_date_string_order_is_chronological(self):
        """锁住该比较的前提：ISO 日期字符串字典序 = 时间序。"""
        self.assertTrue("2026-09-15" < "2026-09-16")
        self.assertFalse("2026-09-16" < "2026-09-15")


if __name__ == "__main__":
    unittest.main()
