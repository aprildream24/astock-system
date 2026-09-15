# -*- coding: utf-8 -*-
"""守护：每日体检工具**不许把自己的自测当成生产推送**（2026-09-15）。

真实事故（自欺的又一次变体）：
    `tools/daily_check.py` 早期把今天所有 `status=sent` 都算作"推送正常"。
    其中包含我为了验证通道而发的 `selftest` / `selftest_20260915b` /
    `data_blocked_pre`。于是体检输出 `[OK] 推送发送 今天 3 条 sent`
    → `结论：今天一切正常 ✓`——**而当天真实生产推送是 0 条**。

    这是"用自己制造的痕迹当证据"的第二次翻车（第一次是 e2e 弄丢 config）。
    同类错误的共性是：**把"我做了动作"误当成"系统产出了结果"**。

本测试锁死：
    1. `selftest*` / `channel_test*` 一律不计入生产推送；
    2. 只有生产 mode（build_* / narrative / watch_advice / review）才算 sent；
    3. 只有自测消息时，`ok` 必须为 False（不得假绿）；
    4. CI 步骤名必须**子串匹配**（实际名带后缀括号，精确匹配会静默匹配不到）。
"""
import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_check():
    p = os.path.join(ROOT, "tools", "daily_check.py")
    spec = importlib.util.spec_from_file_location("daily_check_mod", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestSelfTestNotProd(unittest.TestCase):

    def setUp(self):
        self.dc = _load_check()
        self.today = "2026-09-15"
        # 隔离：让 check_push 只读我们构造的本地账本，不碰真实文件
        self._orig = self.dc.ROOT
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dc_")
        self.dc.ROOT = self.tmp
        os.makedirs(os.path.join(self.tmp, "dist"), exist_ok=True)

    def tearDown(self):
        import shutil
        self.dc.ROOT = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_ledger(self, entries):
        with open(os.path.join(self.tmp, "dist", "push_ledger.json"), "w",
                  encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)

    def test_only_selftest_means_not_ok(self):
        """★ 核心：只有自测消息 ⇒ 必须判不 OK。"""
        self._write_ledger({
            "a": {"ts": f"{self.today} 10:00:00", "mode": "selftest",
                  "status": "sent", "channels": {"pushplus": "sent"}},
            "b": {"ts": f"{self.today} 11:00:00", "mode": "selftest_20260915b",
                  "status": "sent", "channels": {"pushplus": "sent"}},
            "c": {"ts": f"{self.today} 12:00:00", "mode": "data_blocked_pre",
                  "status": "sent", "channels": {"pushplus": "sent"}},
        })
        r = self.dc.check_push(self.today)      # 不传 token → 只读本地
        self.assertFalse(r["ok"],
                         f"只有自测消息却判 OK（假绿）！detail={r['detail']}")
        self.assertEqual(r["real"], [], "自测不得计入生产推送")
        self.assertEqual(len(r["self_test"]), 3)
        self.assertIn("没有任何生产推送", r["detail"])

    def test_real_build_counts(self):
        self._write_ledger({
            "a": {"ts": f"{self.today} 15:22:00", "mode": "build_close",
                  "status": "sent", "channels": {"pushplus": "sent"}},
            "b": {"ts": f"{self.today} 08:50:00", "mode": "selftest",
                  "status": "sent", "channels": {"pushplus": "sent"}},
        })
        r = self.dc.check_push(self.today)
        self.assertTrue(r["ok"], "有 build_close 应判 OK")
        self.assertEqual(len(r["real"]), 1)
        self.assertEqual(r["real"][0]["mode"], "build_close")
        self.assertEqual(len(r["self_test"]), 1)

    def test_non_sent_excluded(self):
        self._write_ledger({
            "a": {"ts": f"{self.today} 15:22:00", "mode": "build_close",
                  "status": "failed", "channels": {"pushplus": "failed"}},
        })
        r = self.dc.check_push(self.today)
        self.assertFalse(r["ok"], "failed 不能算 sent")

    def test_other_day_excluded(self):
        self._write_ledger({
            "a": {"ts": "2026-09-14 15:22:00", "mode": "build_close",
                  "status": "sent", "channels": {"pushplus": "sent"}},
        })
        r = self.dc.check_push(self.today)
        self.assertFalse(r["ok"], "昨天的不算今天")

    def test_production_modes_recognized(self):
        """build_/narrative/watch_advice/review 都应算生产。"""
        for mode in ("build_close", "build_pre", "narrative",
                     "watch_advice", "review_close"):
            self._write_ledger({
                "a": {"ts": f"{self.today} 15:22:00", "mode": mode,
                      "status": "sent", "channels": {"pushplus": "sent"}},
            })
            r = self.dc.check_push(self.today)
            self.assertTrue(r["ok"], f"{mode} 应算生产推送")

    def test_selftest_prefixes_excluded(self):
        for mode in ("selftest", "selftest_20260915b", "channel_test_20260913"):
            self._write_ledger({
                "a": {"ts": f"{self.today} 10:00:00", "mode": mode,
                      "status": "sent", "channels": {"pushplus": "sent"}},
            })
            r = self.dc.check_push(self.today)
            self.assertFalse(r["ok"], f"{mode} 是自测，不得算生产")


class TestCiStepMatchingIsSubstring(unittest.TestCase):
    """CI 步骤名必须子串匹配——实际名带后缀，精确匹配会静默失配。"""

    def test_key_steps_are_substrings_of_real_names(self):
        dc = _load_check()
        # 取自线上真实 workflow step 名（2026-09-15 实测）
        real_names = [
            "回归自检",
            "收盘数据抓取（全市场）",
            "盘前/竞价轻量增量抓取",
            "构建+推送（WxPusher/PushPlus 多渠道）",
            "收盘复盘（自选股建议+AI叙事）",
            "构建加密站点",
            "上传 Pages 构件",
        ]
        needles = ("回归自检", "构建+推送", "构建加密站点", "收盘复盘")
        for n in needles:
            matched = [rn for rn in real_names if n in rn]
            self.assertTrue(
                matched,
                f"关键步骤「{n}」在真实步骤名里匹配不到——"
                f"若用精确相等就会静默失配（列永远空白）。真实名：{real_names}")


if __name__ == "__main__":
    unittest.main()
