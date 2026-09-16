# -*- coding: utf-8 -*-
"""盘前/竞价任务的 K线新鲜度锚（2026-09-16 血案锁定）。

血案：pre（08:50）/ auction（09:25）跑在**当日收盘K线入库之前**，
而 `scan_all.bars_of` 用 `rows[-1][0] != date` 判陈旧 ⇒ 全市场 4937 只
被判「K线未更新至{date}」⇒ 覆盖 0.0%、**候选 0 只**
⇒ 用户收到一份没有任何标的的盘前计划（实测 2026-09-16 早）。

修法：`scan_all(con, date, bar_anchor=None)`，盘前任务由 build 传入
上一交易日作锚；收盘/复盘保持锚定当日（None → date）。

硬规则：禁网络、禁依赖工作区真实数据、断言源码前先剥注释。
"""
import inspect
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import build as B  # noqa: E402

SRC = open(os.path.join(ROOT, "pipeline", "build.py"),
           encoding="utf-8").read()


def strip_comments(src):
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines())


class TestScanAllAnchorSignature(unittest.TestCase):

    def test_accepts_bar_anchor(self):
        sig = inspect.signature(B.scan_all)
        self.assertIn("bar_anchor", sig.parameters)

    def test_bar_anchor_is_optional(self):
        """收盘/复盘路径不传参时必须仍锚定当日 —— 老调用方不得破坏。"""
        sig = inspect.signature(B.scan_all)
        self.assertIsNone(sig.parameters["bar_anchor"].default)


class TestAnchorSemantics(unittest.TestCase):
    """源码级语义（剥注释后再断言，避免命中修复说明里的旧写法）。"""

    def test_expected_bar_defaults_to_date(self):
        code = strip_comments(SRC)
        self.assertIn("expected_bar = bar_anchor or date", code)

    def test_freshness_uses_expected_bar_not_date(self):
        """新鲜度只对「比锚定日**更旧**」判陈旧。

        ⚠️ 2026-09-16 同日二次修：原为 `!= expected_bar`，把「已拿到当日
        实时K线」的票也判成陈旧（CI 日志「数据新鲜0 陈旧19」即此），
        既误剔标的、又把排查带偏。判陈旧=数据不够新 ⇒ 只应比较「更旧」。
        """
        code = strip_comments(SRC)
        self.assertIn("if rows[-1][0] < expected_bar:", code)

    def test_no_bare_date_freshness_check(self):
        """不得回退成锚定当日（那正是候选 0 的根因）。"""
        code = strip_comments(SRC)
        self.assertNotIn("if rows[-1][0] != date:", code)
        self.assertNotIn("if rows[-1][0] < date:", code)

    def test_have_bar_uses_expected_bar(self):
        """覆盖率统计口径必须与判定一致，否则覆盖 100% 却候选 0。"""
        code = strip_comments(SRC)
        self.assertIn("(expected_bar,)).fetchall()", code)

    def test_preauction_tasks_pass_prev_trading_day(self):
        code = strip_comments(SRC)
        self.assertIn('_bar_anchor = (core.prev_trading_day(con, date)', code)
        self.assertIn('if task in ("pre", "auction") else None)', code)

    def test_close_tasks_keep_none_anchor(self):
        """close/review 必须锚定当日 —— 盘前放宽不得外溢到收盘。"""
        code = strip_comments(SRC)
        self.assertIn("scan_all(con, date, bar_anchor=_bar_anchor)", code)


if __name__ == "__main__":
    unittest.main()
