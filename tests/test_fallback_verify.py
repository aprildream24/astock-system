# -*- coding: utf-8 -*-
"""盘中快照多源兜底 + 日驱动前序核验的回归锁（2026-09-27）。

锁四组不变量：

A. 兜底只属于盘中口径：fetch_universe() 不带 fallback 参数时行为零变化
   （主链/质量闸永远吃 EM 原口径）；max_stocks 受限拉取不触发兜底。
B. 兜底链顺序 EM → tx → sina；且只有 EM < 500 只（源异常阈值）才触发。
C. 新浪返回的是键名不带引号的 JS 字面量——解析器必须容错，残缺行跳过。
D. 前序核验判据 = 云端账本"status=sent 且 ts=今天"；账本读不到时
   绝不告警（宁可漏报）；close 缺失自动补发；intraday_* 永不列必达。
"""
import base64
import importlib
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestUniverseFallback(unittest.TestCase):
    """A/B. 兜底的触发条件与顺序。"""

    def setUp(self):
        self.fd = importlib.import_module("pipeline.fetch_daily")

    @staticmethod
    def _mk(n, code0="600000"):
        """凑 n 只票（兜底阈值 ≥500 的测试夹具）。"""
        return {f"{int(code0) + i:06d}": {"name": "x", "price": 1.0,
                                          "pct": 0.0, "vol": 1, "amt": 1,
                                          "turn": 1, "fmv": 1}
                for i in range(n)}

    def test_no_fallback_param_unchanged(self):
        """主链口径零变化：不带 fallback → EM 结果原样返回，兜底函数不碰。"""
        em = self._mk(600)
        with mock.patch.object(self.fd, "_universe_em", return_value=em), \
                mock.patch.object(self.fd, "_universe_tx") as tx, \
                mock.patch.object(self.fd, "_universe_sina") as sina:
            got = self.fd.fetch_universe()
        self.assertEqual(got, em)
        tx.assert_not_called()
        sina.assert_not_called()

    def test_fallback_triggers_only_below_threshold(self):
        """EM ≥500 只 → 不兜底；<500 只 → 按 sina → tx 顺序兜底（09-27 实测
        新浪可用、腾讯 400，故新浪为主）。"""
        with mock.patch.object(self.fd, "_universe_em",
                               return_value=self._mk(600)), \
                mock.patch.object(self.fd, "_universe_sina") as sina:
            self.fd.fetch_universe(fallback=True)
        sina.assert_not_called()

        with mock.patch.object(self.fd, "_universe_em",
                               return_value=self._mk(100)), \
                mock.patch.object(self.fd, "_universe_sina",
                                  return_value=self._mk(600, "2")) as sina2, \
                mock.patch.object(self.fd, "_universe_tx") as tx:
            got = self.fd.fetch_universe(fallback=True)
        sina2.assert_called_once()
        tx.assert_not_called()                       # sina 成功就不再走 tx
        self.assertIn("000002", got)

    def test_fallback_falls_through_to_tx(self):
        with mock.patch.object(self.fd, "_universe_em",
                               return_value=self._mk(100)), \
                mock.patch.object(self.fd, "_universe_sina", return_value={}), \
                mock.patch.object(self.fd, "_universe_tx",
                                  return_value=self._mk(600, "1")) as tx:
            got = self.fd.fetch_universe(fallback=True)
        tx.assert_called_once()
        self.assertIn("000001", got)

    def test_max_stocks_never_falls_back(self):
        with mock.patch.object(self.fd, "_universe_em", return_value={}), \
                mock.patch.object(self.fd, "_universe_tx") as tx:
            got = self.fd.fetch_universe(max_stocks=10, fallback=True)
        self.assertEqual(got, {})
        tx.assert_not_called()


class TestSinaParser(unittest.TestCase):
    """C. 新浪 JS 字面量解析容错。"""

    def setUp(self):
        self.fd = importlib.import_module("pipeline.fetch_daily")

    def test_parse_unquoted_keys(self):
        """键名带引号（09-27 实测线上形态）与不带引号（历史形态）都要能解析。"""
        raw_quoted = ('[{"symbol":"sh600000","name":"浦发银行",'
                      '"trade":"10.20","changepercent":"0.99",'
                      '"volume":"123456","amount":"1260000.0",'
                      '"turnoverratio":"0.51"},'
                      '{"symbol":"sz000001","name":"平安银行",'
                      '"trade":"9.90","changepercent":-1.20}]')
        out = {}
        self.fd._parse_sina_page(raw_quoted, out)
        self.assertEqual(set(out), {"600000", "000001"})
        self.assertAlmostEqual(out["600000"]["price"], 10.20)
        self.assertAlmostEqual(out["000001"]["pct"], -1.20)
        self.assertEqual(out["600000"]["name"], "浦发银行")
        # 历史无引号形态
        out2 = {}
        self.fd._parse_sina_page(
            '[{symbol:"sh600000",name:"浦发银行",trade:"10.20",'
            'changepercent:"0.99"}]', out2)
        self.assertAlmostEqual(out2["600000"]["price"], 10.20)

    def test_parse_skips_broken_rows(self):
        raw = ('[{symbol:"sh600000",name:"浦发银行",trade:"10.20",'
               'changepercent:"0.99"},{broken,,,},{symbol:"x"}]')
        out = {}
        self.fd._parse_sina_page(raw, out)           # 不得抛异常
        self.assertEqual(set(out), {"600000"})

    def test_tx_shape(self):
        """腾讯解析：rank_stocks → code(zsh600000)/zxj/zdf。"""
        fd = self.fd
        pages = [
            json.dumps({"code": 0, "data": {
                "total": 2,
                "rank_stocks": [
                    {"code": "sh600000", "name": "浦发银行",
                     "zxj": "10.20", "zdf": "0.99"},
                    {"code": "sz000001", "name": "平安银行",
                     "zxj": "9.90", "zdf": "-1.20"}]}}),
            json.dumps({"code": 0, "data": {"total": 2,
                                            "rank_stocks": []}}),
        ]
        with mock.patch.object(fd, "fetch_text", side_effect=pages), \
                mock.patch.object(fd.time, "sleep"):
            got = fd._universe_tx()
        self.assertEqual(set(got), {"600000", "000001"})
        self.assertAlmostEqual(got["600000"]["price"], 10.20)
        self.assertIsNone(got["600000"]["fmv"], "tx 兜底 fmv 必须留空")


class TestDriverVerify(unittest.TestCase):
    """D. 前序核验语义。"""

    def setUp(self):
        self.dd = importlib.import_module("tools.day_driver")
        self.today = "2026-09-24"

    def _ledger(self, entries):
        payload = json.dumps(entries).encode()

        def fake_req(method, url, token, body=None, timeout=25):
            self.assertIn("push_ledger.json", url)
            return 200, {"content": base64.b64encode(payload).decode()}
        return fake_req

    def test_all_sent_no_alert(self):
        ledger = {"k1": {"mode": "build_pre", "ts": self.today + " 08:53:00",
                         "status": "sent"},
                  "k2": {"mode": "build_auction",
                         "ts": self.today + " 09:27:00", "status": "sent"}}
        with mock.patch.object(self.dd, "_req", self._ledger(ledger)):
            n = self.dd.verify_prior("morning", "tok", "x/y", self.today,
                                     "end", dry=True)
        self.assertEqual(n, 0)

    def test_missing_close_triggers_catchup(self):
        """close 缺失 → 补发 dispatch + 告警；notifier 走 mock 不真发。"""
        ledger = {"k1": {"mode": "build_pre", "ts": self.today + " 08:53",
                         "status": "sent"}}
        calls = []

        def fake_dispatch(token, repo, task, extra=None, retries=3):
            calls.append(task)
            return True

        with mock.patch.object(self.dd, "_req", self._ledger(ledger)), \
                mock.patch.object(self.dd, "dispatch", fake_dispatch), \
                mock.patch("pipeline.notifier.push",
                           return_value={"sent": True}):
            # dry=False 才会走补发路径（dispatch 已 mock，不会真发）
            n = self.dd.verify_prior("afternoon", "tok", "x/y", self.today,
                                     "end", dry=False)
        self.assertEqual(n, 0, "补发成功后不再计告警缺失")
        self.assertEqual(calls, ["close"])

    def test_unreadable_ledger_never_alerts(self):
        """账本读不到 → 静默跳过（宁可漏报不可误报）。"""
        def fail_req(method, url, token, body=None, timeout=25):
            raise OSError("network down")
        with mock.patch.object(self.dd, "_req", fail_req), \
                mock.patch.object(self.dd, "dispatch") as disp, \
                mock.patch("pipeline.notifier.push") as push:
            n = self.dd.verify_prior("evening", "tok", "x/y", self.today,
                                     "start", dry=True)
        self.assertEqual(n, 0)
        disp.assert_not_called()
        push.assert_not_called()

    def test_intraday_modes_never_required(self):
        """intraday_am/pm/live 常常合法静默——绝不能进必达清单。"""
        for part in self.dd.VERIFY_START:
            for m in self.dd.VERIFY_START[part] + self.dd.VERIFY_END[part]:
                self.assertTrue(m.startswith("build_"),
                                f"{part} 必达清单混入了 {m}")

    def test_non_sent_status_ignored(self):
        """pending/uncertain 不算送达（这正是核验的意义）。"""
        ledger = {"k1": {"mode": "build_pre", "ts": self.today + " 08:53",
                         "status": "uncertain"}}
        calls = []

        def fake_dispatch(token, repo, task, extra=None, retries=3):
            calls.append(task)
            return True

        with mock.patch.object(self.dd, "_req", self._ledger(ledger)), \
                mock.patch.object(self.dd, "dispatch", fake_dispatch), \
                mock.patch("pipeline.notifier.push",
                           return_value={"sent": True}):
            self.dd.verify_prior("morning", "tok", "x/y", self.today,
                                 "end", dry=True)
        self.assertEqual(calls, [], "pre 无补发手段，只告警")


if __name__ == "__main__":
    unittest.main()
