# -*- coding: utf-8 -*-
"""云端全链路演练通道（2026-09-19 用户：「全部在网络上运行一次，
该推送的信息全部推送一遍」）回归锁。

演练纪律：
  · ASTOCK_REHEARSAL=1 → 账本 mode 加 rehearsal_ 前缀（与正式日熔丝隔离）；
  · 标题加【演练】；标题的任务标签仍按原始 mode 渲染；
  · build 锚定最近交易日（周末也能用真实数据演练）；
  · workflow rehearsal 分支链式跑 pre→auction→close→review 且 force。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pipeline import notifier  # noqa: E402


class TestRehearsal(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = notifier.load_config
        notifier.load_config = lambda: {
            "push_dry_run": False, "primary_channel": "pushplus",
            "push_tag": "Astra", "serverchan_key": "",
            "pushplus_token": "PP"}

    def tearDown(self):
        notifier.load_config = self._orig_cfg
        os.environ.pop("ASTOCK_REHEARSAL", None)

    def _push(self, con):
        captured = {}
        orig = notifier._send_pushplus

        def fake_pp(token, title, content):
            captured["title"] = title
            return "sent", "ok"
        notifier._send_pushplus = fake_pp
        try:
            r = notifier.push("build_close", "收盘观察 09-18", "<p>x</p>",
                              date="2026-09-18", con=con)
        finally:
            notifier._send_pushplus = orig
        return r, captured

    def test_rehearsal_prefix_and_title(self):
        os.environ["ASTOCK_REHEARSAL"] = "1"
        with tempfile.TemporaryDirectory() as td:
            notifier.DIST_LEDGER = os.path.join(td, "l.json")
            con = get_conn_safe(td)
            r, cap = self._push(con)
            self.assertTrue(r["sent"])
            self.assertTrue(cap["title"].startswith("【演练】"),
                            f"标题必须带演练标识: {cap['title']}")
            self.assertIn("【收盘】", cap["title"],
                          "任务标签仍按原始 mode 渲染")
            row = con.execute("SELECT mode FROM push_ledger").fetchone()
            self.assertTrue(row[0].startswith("rehearsal_"),
                            f"账本 mode 必须带前缀: {row[0]}")
            con.close()

    def test_rehearsal_fuse_isolated_from_production(self):
        """演练推送后，正式 mode 同日推送不被日熔丝拦截（通道隔离核心断言）。"""
        os.environ["ASTOCK_REHEARSAL"] = "1"
        with tempfile.TemporaryDirectory() as td:
            notifier.DIST_LEDGER = os.path.join(td, "l.json")
            con = get_conn_safe(td)
            self._push(con)
            n_rh = con.execute(
                "SELECT COUNT(*) FROM push_ledger WHERE mode LIKE 'rehearsal_%'"
            ).fetchone()[0]
            self.assertEqual(n_rh, 1)
            # 正式通道同日同内容：应照常发送（未被演练熔丝吞掉）
            os.environ.pop("ASTOCK_REHEARSAL", None)
            r, _ = self._push_prod(con)
            self.assertTrue(r["sent"], "正式通道不得被演练占用")
            con.close()

    def _push_prod(self, con):
        captured = {}
        orig = notifier._send_pushplus

        def fake_pp(token, title, content):
            captured["title"] = title
            return "sent", "ok"
        notifier._send_pushplus = fake_pp
        try:
            r = notifier.push("build_close", "收盘观察 09-18", "<p>x</p>",
                              date="2026-09-18", con=con)
        finally:
            notifier._send_pushplus = orig
        return r, captured

    def test_no_env_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            notifier.DIST_LEDGER = os.path.join(td, "l.json")
            con = get_conn_safe(td)
            r, cap = self._push(con)
            self.assertTrue(r["sent"])
            self.assertFalse(cap["title"].startswith("【演练】"),
                             "未开演练时行为零变化")
            row = con.execute("SELECT mode FROM push_ledger").fetchone()
            self.assertEqual(row[0], "build_close")
            con.close()


def get_conn_safe(td):
    from pipeline.core import get_conn
    return get_conn(os.path.join(td, "t.db"))


class TestRehearsalWiring(unittest.TestCase):
    def test_build_anchors_latest_trading_day(self):
        src = open(os.path.join(ROOT, "pipeline", "build.py"),
                   encoding="utf-8").read()
        self.assertIn('os.environ.get("ASTOCK_REHEARSAL") == "1"', src)
        self.assertIn("trade_calendar(con)[-1]", src,
                      "演练必须锚定最近交易日")

    def test_workflow_rehearsal_branch(self):
        y = open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                 encoding="utf-8").read()
        self.assertIn('TASK" = "rehearsal"', y)
        self.assertIn("export ASTOCK_REHEARSAL=1", y)
        self.assertIn("export ASTOCK_FORCE_PUSH=1", y)
        for t in ("--task pre", "--task auction", "--task close",
                  "--task review"):
            self.assertIn(t, y, f"演练链必须包含 {t}")
        self.assertNotIn("?:", y.replace("https://", "").replace("http://", ""),
                         "三元字面量红线（workflow 解析血案）")

    def test_notifier_wiring(self):
        src = open(os.path.join(ROOT, "pipeline", "notifier.py"),
                   encoding="utf-8").read()
        self.assertIn("rehearsal_" + '" + str(mode)', src.replace(
            '("rehearsal_" + str(mode)) if _rh else mode',
            'rehearsal_" + str(mode)'))
        self.assertIn("【演练】", src)
        # 账本与 dist 镜像都必须用 ledger_mode（而非原始 mode）
        self.assertIn('dist[key] = {"mode": ledger_mode', src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
