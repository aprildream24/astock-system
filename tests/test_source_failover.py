# -*- coding: utf-8 -*-
"""守护：数据源域名失效必须能被**发现并绕过**（2026-09-15 实盘事故）。

真实事故链（用户"每天都告诉我没问题，结果一到实盘就是这样那样的问题"）：
    腾讯 K线域名 `ifzq.gtimg.cn` 返回 **HTTP 501**（端点下线），
    但 `fetch_text` 的熔断码只有 (403, 429, 418) ⇒ 501 既不算节流也不算熔断，
    被当普通失败重试 `retries=2` 次 + 指数退避 ⇒ 单票白等 6–15 秒。
    全市场 ~4900 只 ⇒ 抓取被拖过 45 分钟 timeout ⇒ 第 8 步「构建+推送」
    整步 skipped ⇒ 用户全天零推送。

    而本地单元回归**全绿**——因为测试从不真连网、也从不校验域名可用性。
    这正是"本机全绿、实盘翻车"的典型形态。

本测试锁死三条不变量（全部离线可跑，不依赖网络）：
    1. TX 域名必须是个**候选列表**（TX_HOSTS），不得写死单点；
    2. 端点级失效码（404/410/501）必须计入 DEAD_CODES，且 fetch_text
       对它 **不重试**（否则就是 6–15 秒白等的复现）；
    3. `_kline_one_tx` 必须**逐个域名尝试**，前面的挂了能自动换后面的，
       且某个域名 BanBlocked 时不会整体抛穿。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import core  # noqa: E402


class TestTxHostsArePlural(unittest.TestCase):
    """域名不得写死单点。"""

    def test_tx_hosts_is_sequence(self):
        self.assertTrue(hasattr(core, "TX_HOSTS"),
                        "必须有 TX_HOSTS 候选列表——单点域名一挂就全天零推送")
        self.assertIsInstance(core.TX_HOSTS, (list, tuple))
        self.assertGreaterEqual(len(core.TX_HOSTS), 2,
                                "至少要两个候选域名以便自动切换")
        for h in core.TX_HOSTS:
            self.assertIsInstance(h, str)
            self.assertTrue(h and "." in h, f"域名形态可疑：{h}")

    def test_tx_host_backcompat(self):
        """旧代码引用的 TX_HOST 仍须可用，且指向首选候选。"""
        self.assertEqual(core.TX_HOST, core.TX_HOSTS[0])


class TestDeadCodes(unittest.TestCase):
    """端点级失效码必须存在且被特殊处理。"""

    def test_dead_codes_defined(self):
        self.assertTrue(hasattr(core, "DEAD_CODES"),
                        "必须有 DEAD_CODES——501 这类端点下线码要能被识别")
        for c in (501, 404, 410):
            self.assertIn(c, core.DEAD_CODES,
                          f"HTTP {c} 属端点级失效，必须在 DEAD_CODES 里")

    def test_dead_code_does_not_retry(self):
        """501 必须**立即抛出**，不得走重试退避（那正是 6–15 秒白等）。"""
        import urllib.error
        calls = {"n": 0}

        def boom(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 501, "Not Implemented", {}, None)

        url = "https://dead.example.com/kline?x=1"
        with mock.patch.object(core.urllib.request, "urlopen", boom), \
                mock.patch.object(core.time, "sleep") as slp:
            with self.assertRaises(urllib.error.HTTPError):
                core.fetch_text(url, retries=2)
        self.assertEqual(calls["n"], 1,
                         f"501 只应请求 1 次，实际 {calls['n']} 次"
                         "——重试就是白等 6–15 秒的元凶")
        self.assertFalse(slp.called, "501 不应触发重试退避 sleep")

    def test_ban_blocked_short_circuits(self):
        """已熔断的 host 必须直接 BanBlocked，不发请求。"""
        host = "blocked.example.com"
        g = core.guard(host)
        for _ in range(20):
            g.note_fail()
        self.assertTrue(g.blocked(), "连续失败后 guard 应处于熔断态")
        called = {"n": 0}

        def boom(req, timeout=None):
            called["n"] += 1
            raise AssertionError("熔断后不该再发请求")

        with mock.patch.object(core.urllib.request, "urlopen", boom):
            with self.assertRaises(core.BanBlocked):
                core.fetch_text(f"https://{host}/x", retries=0)
        self.assertEqual(called["n"], 0, "熔断后必须零请求")


class TestKlineOneTxFallsOver(unittest.TestCase):
    """腾讯取数必须在候选域名间自动切换。"""

    def test_falls_over_to_second_host(self):
        """首选域名失败 → 自动用第二个域名拿到数据。"""
        good = {"data": {"sh600000": {"qfqday": [
            ["2026-09-15", "9.0", "9.1", "9.2", "8.9", "1000"]]}}}

        def fake_fetch(url, **kw):
            if core.TX_HOSTS[0] in url:
                raise RuntimeError("primary dead")
            import json as _j
            return _j.dumps(good)

        with mock.patch.object(core, "fetch_text", fake_fetch):
            rows = core._kline_one_tx("600000", "sh", 20)
        self.assertEqual(len(rows), 1, "首选挂了必须能从备选域名拿到数据")
        self.assertEqual(rows[0][0], "2026-09-15")

    def test_all_hosts_dead_raises_not_crashes(self):
        """全部域名都挂 → 抛异常让上游换源，不得静默返回空。"""
        import urllib.error

        def fake_fetch(url, **kw):
            raise urllib.error.HTTPError(url, 501, "dead", {}, None)

        with mock.patch.object(core, "fetch_text", fake_fetch):
            with self.assertRaises(Exception):
                core._kline_one_tx("600000", "sh", 20)

    def test_ban_blocked_on_one_host_still_tries_other(self):
        """某域名 BanBlocked → 换下一个，不整体抛穿。"""
        good = {"data": {"sh600000": {"qfqday": [
            ["2026-09-15", "9.0", "9.1", "9.2", "8.9", "1000"]]}}}

        def fake_fetch(url, **kw):
            if core.TX_HOSTS[0] in url:
                raise core.BanBlocked(core.TX_HOSTS[0])
            import json as _j
            return _j.dumps(good)

        with mock.patch.object(core, "fetch_text", fake_fetch):
            rows = core._kline_one_tx("600000", "sh", 20)
        self.assertEqual(len(rows), 1)


class TestBatchGuardChecksAllTxHosts(unittest.TestCase):
    """kline_batch 的早期放弃判定要看**全部**候选域名。"""

    def setUp(self):
        # 用独一无二的 host 名，天然规避别的测试残留的熔断状态
        self.saved_hosts = core.TX_HOSTS
        self.saved_em = core.EM_HOST
        core.TX_HOSTS = (f"bx-a-{id(self)}.example.com",
                         f"bx-b-{id(self)}.example.com")
        core.EM_HOST = f"bx-em-{id(self)}.example.com"

    def tearDown(self):
        core.TX_HOSTS = self.saved_hosts
        core.EM_HOST = self.saved_em

    def test_gives_up_only_when_all_tx_blocked(self):
        # 熔断东财 + 第一个 TX 域名；第二个 TX 域名保持健康
        for _ in range(20):
            core.guard(core.EM_HOST).note_fail()
            core.guard(core.TX_HOSTS[0]).note_fail()
        self.assertTrue(core.guard(core.EM_HOST).blocked())
        self.assertTrue(core.guard(core.TX_HOSTS[0]).blocked())
        self.assertFalse(core.guard(core.TX_HOSTS[1]).blocked(),
                         "第二个域名应仍健康，不得被前面域名的失败连坐")
        # 只要还有一个域名活着，就不该被判定为"全挂"
        alive = [h for h in core.TX_HOSTS if not core.guard(h).blocked()]
        self.assertTrue(alive, "至少应有一个 TX 域名可用")

    def test_gives_up_when_everything_blocked(self):
        for _ in range(20):
            core.guard(core.EM_HOST).note_fail()
            for h in core.TX_HOSTS:
                core.guard(h).note_fail()
        self.assertTrue(core.guard(core.EM_HOST).blocked())
        self.assertTrue(all(core.guard(h).blocked() for h in core.TX_HOSTS))
        # 全挂时 kline_batch 应立刻返回空，不发任何请求
        with mock.patch.object(core.urllib.request, "urlopen",
                               side_effect=AssertionError("不该发请求")):
            self.assertEqual(core.kline_batch([("600000", "sh")], days=20), {})


if __name__ == "__main__":
    unittest.main()
