# -*- coding: utf-8 -*-
"""GitHub 单通道部署 + WxPusher 多账户 + 自选股建议 回归。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import build as bld  # noqa: E402
from pipeline import decisions, notifier, watchlist, wxpusher  # noqa: E402
from pipeline.core import get_conn  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATES = [(date(2026, 9, 11) - timedelta(days=44 - i)).isoformat()
         for i in range(45)]


def _mk_con():
    con = get_conn(":memory:")
    put = lambda d, code, o, h, l, c: con.execute(  # noqa: E731
        "INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
        (code, d, o, h, l, c, 1e6, None, None, None))
    for d in DATES:
        put(d, "sh000001", 3000, 3010, 2995, 3005)
    # 自选①：上行趋势票（现价 10.5，无箱体 → 近端阶梯）
    c = 10.0
    for i, d in enumerate(DATES[:-1]):
        c *= 1 + (2.0 if i % 5 != 4 else -0.5) / 100
        put(d, "sh600100", c * 0.995, c * 1.008, c * 0.992, c)
    put(DATES[-1], "sh600100", c * 0.995, c * 1.01, c * 0.99, c * 1.01)
    # 自选②：持续阴跌+末日崩盘 → 真破位票
    c2 = 25.0
    for i, d in enumerate(DATES[:-1]):
        c2 *= 1 + (-0.8 if i % 5 == 4 else -0.2) / 100
        put(d, "sz000200", c2 * 0.995, c2 * 1.004, c2 * 0.99, c2)
    put(DATES[-1], "sz000200", c2 * 0.92, c2 * 0.93, c2 * 0.85, c2 * 0.86)
    # 自选③：科创板（不可交易市场）
    for i, d in enumerate(DATES):
        put(d, "sh688981", 50, 51, 49.5, 50.2)
    con.commit()
    return con


class TestWxPusher(unittest.TestCase):
    def test_account_merge_and_route(self):
        local = [{"name": "本地", "app_token": "AT_A", "uids": ["U1"]}]
        env = [{"name": "CI号", "app_token": "AT_B", "uids": ["U2"]}]
        accounts = list(local) + list(env)
        # 无路由 → 全部
        self.assertEqual(len(wxpusher.resolve_targets(
            "build_close", accounts=accounts, cfg={})), 2)
        # 精确路由
        cfg = {"wxpusher_routes": {"build_close": ["CI号"]}}
        t = wxpusher.resolve_targets("build_close", accounts=accounts, cfg=cfg)
        self.assertEqual([a["name"] for a in t], ["CI号"])
        # 前缀路由
        cfg2 = {"wxpusher_routes": {"watch": ["本地"]}}
        t2 = wxpusher.resolve_targets("watch_advice", accounts=accounts, cfg=cfg2)
        self.assertEqual([a["name"] for a in t2], ["本地"])
        # 星号兜底
        cfg3 = {"wxpusher_routes": {"*": ["本地"]}}
        t3 = wxpusher.resolve_targets("exec_scan", accounts=accounts, cfg=cfg3)
        self.assertEqual([a["name"] for a in t3], ["本地"])

    def test_send_status_classification(self):
        import io
        import urllib.error
        from unittest import mock
        acct = {"name": "A", "app_token": "AT", "uids": ["U"]}
        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def read(self):
                return json.dumps({"code": 1000, "msg": "成功"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            st, _ = wxpusher.send(acct, "t", "<p>x</p>")
            self.assertEqual(st, "sent")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                "u", 400, "bad token", None, io.BytesIO(b'{"code":1001}'))):
            st, _ = wxpusher.send(acct, "t", "<p>x</p>")
            self.assertEqual(st, "failed")
        with mock.patch("urllib.request.urlopen",
                        side_effect=__import__("socket").timeout()):
            st, _ = wxpusher.send(acct, "t", "<p>x</p>")
            self.assertEqual(st, "uncertain", "超时=受理不确定，不盲目重试")

    def test_push_multi_account_results(self):
        orig = notifier.load_config
        notifier.load_config = lambda: {
            "push_dry_run": False, "serverchan_key": "",
            "wxpusher_accounts": [
                {"name": "A", "app_token": "AT1", "uids": ["U1"]},
                {"name": "B", "app_token": "AT2", "uids": ["U2"]}],
            "wxpusher_routes": {"*": ["A", "B"]}}
        import pipeline.wxpusher as wx
        o_wxsend, o_accounts = wx.send, wx.load_accounts
        wx.send = lambda acct, t, c, timeout=12: ("sent", "ok")
        wx.load_accounts = lambda: notifier.load_config()["wxpusher_accounts"]
        try:
            with tempfile.TemporaryDirectory() as td:
                notifier.DIST_LEDGER = os.path.join(td, "l.json")
                con = get_conn(os.path.join(td, "t.db"))
                r = notifier.push("m", "t", "600000", date="2026-09-12", con=con)
                self.assertEqual(r["status"], "sent")
                self.assertEqual(set(r["results"]),
                                 {"wxpusher:A", "wxpusher:B"},
                                 "两账户都发送且分别记录")
                con.close()
        finally:
            wx.send = o_wxsend
            wx.load_accounts = o_accounts
            notifier.load_config = orig


class TestWatchAdvice(unittest.TestCase):
    def test_context_translation(self):
        con = _mk_con()
        advice = watchlist.build_watch_advice(
            con, DATES[-1], ["sh600100", "sz000200", "sh688981"],
            holdings_codes=["sh600100"])
        by = {a["code"]: a for a in advice}
        # 趋势票收盘创高 → 过热/等回踩/微超之一（未持仓语境禁"持有"）
        a1 = by["sh600100"]
        self.assertIn(a1["action"], ("微超", "等回踩", "过热", "可买（回落至买区）"))
        self.assertNotIn("持有", a1["advice"],
                         "未持仓票禁止显示'持有'（规格书 十）")
        # 破位票 → 当日急跌不接刀（-14% 落进买区也不是好买点）
        a2 = by["sz000200"]
        self.assertEqual(a2["action"], "急跌",
                         "单日深跌必须先观察企稳，不得当日喊买")
        # 科创板 → 不可交易标注
        a3 = by["sh688981"]
        self.assertFalse(a3["tradable"])
        self.assertIn("不可交易", a3["advice"])
        con.close()

    def test_summary_lines(self):
        lines = watchlist.summary_lines([
            {"code": "600100", "name": "示例", "action": "微超",
             "advice": "小仓试探", "dist_pct": 1.2}])
        self.assertIn("示例", lines[1])
        self.assertIn("小仓试探", lines[1])


class TestPushTag(unittest.TestCase):
    """防混淆标识：标题前缀 【{tag}·来源】+ 正文角标，主通道可切换。"""

    def test_pushplus_primary_tagged(self):
        orig = notifier.load_config
        notifier.load_config = lambda: {
            "push_dry_run": False, "serverchan_key": "",
            "pushplus_token": "PP", "primary_channel": "pushplus",
            "push_tag": "Astra"}
        o_pp = notifier._send_pushplus
        captured = {}
        try:
            def fake_pp(token, title, content):
                captured["title"] = title
                captured["content"] = content
                return "sent", "ok"
            notifier._send_pushplus = fake_pp
            with tempfile.TemporaryDirectory() as td:
                notifier.DIST_LEDGER = os.path.join(td, "l.json")
                con = get_conn(os.path.join(td, "t.db"))
                r = notifier.push("build_close", "收盘观察 09-11", "<p>x</p>",
                                  date="2026-09-12", con=con)
                self.assertEqual(r["status"], "sent")
                self.assertTrue(captured["title"].startswith("【Astra·PushPlus】"),
                                f"标题必须带来源标识: {captured['title']}")
                self.assertIn("PushPlus", captured["content"][:120],
                              "正文顶部应有同源角标")
                con.close()
        finally:
            notifier._send_pushplus = o_pp
            notifier.load_config = orig

    def test_wxpusher_tag_per_account(self):
        import pipeline.wxpusher as wx
        orig = notifier.load_config
        notifier.load_config = lambda: {
            "push_dry_run": False, "serverchan_key": "",
            "primary_channel": "wxpusher", "push_tag": "Astra",
            "wxpusher_accounts": [
                {"name": "主号", "app_token": "AT1", "uids": ["U1"]},
                {"name": "家人", "app_token": "AT2", "uids": ["U2"]}],
            "wxpusher_routes": {"*": ["主号", "家人"]}}
        o_wxsend, o_accounts = wx.send, wx.load_accounts
        titles = []
        try:
            wx.load_accounts = lambda: notifier.load_config()["wxpusher_accounts"]

            def fake_send(acct, t, c, timeout=12):
                titles.append(t)
                return "sent", "ok"
            wx.send = fake_send
            with tempfile.TemporaryDirectory() as td:
                notifier.DIST_LEDGER = os.path.join(td, "l.json")
                con = get_conn(os.path.join(td, "t.db"))
                notifier.push("m", "收盘观察", "<p>x</p>",
                              date="2026-09-12", con=con)
                self.assertEqual(sorted(titles),
                                 ["【Astra·主号】收盘观察",
                                  "【Astra·家人】收盘观察"],
                                 "每个账户标题都带自己的来源标识")
                con.close()
        finally:
            wx.send = o_wxsend
            wx.load_accounts = o_accounts
            notifier.load_config = orig


class TestGitHubOnly(unittest.TestCase):
    def test_workflow_pages_and_no_cf(self):
        y = open(os.path.join(BASE, ".github", "workflows", "stock.yml"),
                 encoding="utf-8").read()
        self.assertIn("deploy-pages@v4", y, "GitHub Pages 部署在位")
        self.assertIn("WXPUSHER_CONF", y, "WxPusher Secret 在位")
        self.assertIn("SITE_USERS", y, "站点口令 Secret 在位")
        self.assertNotIn("wrangler", y.lower(), "不得再有 Cloudflare 痕迹")
        self.assertNotIn("CLOUDFLARE", y)
        self.assertFalse(os.path.exists(os.path.join(BASE, "scf")),
                         "SCF 双轨已移除")
        self.assertFalse(os.path.exists(os.path.join(BASE, "tools",
                                                     "deploy_pages.py")))
        for f in os.listdir(BASE):
            pass  # site 目录由构建生成，不检查


if __name__ == "__main__":
    unittest.main(verbosity=1)
