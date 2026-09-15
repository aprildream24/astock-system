# -*- coding: utf-8 -*-
"""周末 / 法定节假日的休市静默（2026-09-16 新增）。

守护目标：
  1. 休市日 build 早退且**一天只提示一条**（mode 固定为 data_holiday，
     不随 task 变化 —— 否则四个任务各发一条，一天 4 条告警扰民）；
  2. 交易日完全不受影响（不得误杀）；
  3. 日历本身判定正确（周末 / 法定节假日 / 未收录年份的保守行为）。

硬规则遵守：禁网络、禁依赖工作区真实数据、断言源码前先剥注释。
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import build as B  # noqa: E402
from pipeline.trade_calendar import is_trade_day, why_closed  # noqa: E402

SRC = open(os.path.join(ROOT, "pipeline", "build.py"),
           encoding="utf-8").read()


def strip_comments(src):
    """剥掉行注释——修复说明里常引用旧写法，会污染"不得出现 X"类断言。"""
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines())


class _PushRecorder:
    """替换 notifier.push，记录 (mode, date) 而不真发。"""

    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        mode = a[0] if a else k.get("mode")
        self.calls.append((mode, k.get("date")))
        return {"sent": True, "mocked": True}


class TestCalendarSemantics(unittest.TestCase):
    """日历判定本身必须正确。"""

    def test_weekend_is_closed(self):
        # 2026-09-19 周六 / 2026-09-20 周日
        self.assertFalse(is_trade_day("2026-09-19"))
        self.assertFalse(is_trade_day("2026-09-20"))

    def test_weekday_not_in_holiday_is_open(self):
        self.assertTrue(is_trade_day("2026-09-16"))   # 周三
        self.assertTrue(is_trade_day("2026-09-15"))   # 周二

    def test_national_holiday_is_closed(self):
        for d in ("2026-10-01", "2026-10-07", "2026-02-17", "2026-09-25"):
            with self.subTest(d=d):
                self.assertFalse(is_trade_day(d))

    def test_uncovered_year_is_conservatively_open(self):
        """未收录年份一律视为交易日 —— 宁可多推，绝不因日历缺失漏推。"""
        # 2027-01-01 实际是元旦，但年份未收录时保守判为可交易
        self.assertTrue(is_trade_day("2027-01-01"))

    def test_why_closed_wording(self):
        self.assertEqual(why_closed("2026-09-19"), "周末休市")
        self.assertEqual(why_closed("2026-10-01"), "法定节假日休市")
        self.assertEqual(why_closed("2026-09-16"), "")


class TestHolidayBuildSilence(unittest.TestCase):
    """休市日：早退 + 固定 mode（一天一条）。"""

    def _run(self, task, date):
        rec = _PushRecorder()
        # get_conn 返回占位对象即可：休市分支在任何查询之前就早退
        with mock.patch.object(B, "get_conn", lambda: object()), \
             mock.patch("pipeline.notifier.push", rec):
            r = B.build(task, date)
        return r, rec.calls

    def test_holiday_returns_none(self):
        for task in ("pre", "auction", "close", "review"):
            with self.subTest(task=task):
                r, _ = self._run(task, "2026-10-01")
                self.assertIsNone(r)

    def test_all_tasks_share_one_mode(self):
        """四个任务必须用**同一个** mode —— 日级保险丝按 mode+date 去重，
        mode 若随 task 变化就会一天发 4 条。"""
        modes = set()
        for task in ("pre", "auction", "close", "review"):
            _, calls = self._run(task, "2026-10-01")
            self.assertEqual(len(calls), 1, f"{task} 应只推一次")
            modes.add(calls[0][0])
        self.assertEqual(modes, {"data_holiday"})

    def test_mode_is_not_task_suffixed(self):
        _, calls = self._run("close", "2026-10-01")
        self.assertNotIn("close", calls[0][0])
        self.assertFalse(calls[0][0].startswith("data_blocked_"))

    def test_weekend_also_silent_single_push(self):
        r, calls = self._run("close", "2026-09-19")   # 周六
        self.assertIsNone(r)
        self.assertEqual([c[0] for c in calls], ["data_holiday"])

    def test_holiday_push_carries_date(self):
        """date 必须传给 push —— 保险丝按 mode+date 判定，缺 date 会串日。"""
        _, calls = self._run("close", "2026-10-01")
        self.assertEqual(calls[0][1], "2026-10-01")


class TestTradingDayNotAffected(unittest.TestCase):
    """交易日不得被休市分支误杀。"""

    def test_trade_day_does_not_enter_holiday_branch(self):
        """09-16 是交易日 ⇒ 不得发出 data_holiday（否则就是把正常日当休市）。"""
        rec = _PushRecorder()
        real_conn = B.get_conn

        def fake_conn():
            return object()

        with mock.patch.object(B, "get_conn", fake_conn), \
             mock.patch("pipeline.notifier.push", rec), \
             mock.patch.object(B, "_preauction_ready",
                               lambda con, d: (False, "mock-未就绪")):
            # 交易日 + 就绪失败 ⇒ 应走 data_blocked_* 而不是 data_holiday
            r = B.build("pre", "2026-09-16")
        self.assertIsNone(r)
        self.assertTrue(rec.calls, "就绪失败时仍应发告警（不得静默）")
        self.assertNotIn("data_holiday", [c[0] for c in rec.calls])
        self.assertEqual(rec.calls[0][0], "data_blocked_pre")
        self.assertIsNotNone(real_conn)


class TestHolidayCodeInvariants(unittest.TestCase):
    """源码级不变量（断言前先剥注释）。"""

    def test_fixed_mode_literal_present(self):
        code = strip_comments(SRC)
        self.assertIn('notifier.push("data_holiday"', code)

    def test_holiday_gate_precedes_ready_checks(self):
        """休市门必须是 build 的**第一道**门，不能排在就绪判定之后。"""
        code = strip_comments(SRC)
        i_gate = code.find("if not _cal_trade(date):")
        i_ready = code.find("if task in (\"pre\", \"auction\"):")
        self.assertGreater(i_gate, 0, "未找到休市门")
        self.assertGreater(i_ready, 0, "未找到就绪判定")
        self.assertLess(i_gate, i_ready,
                        "休市门必须早于就绪判定，否则休市日仍会先查快照")

    def test_no_task_suffixed_holiday_mode(self):
        code = strip_comments(SRC)
        self.assertNotIn("data_holiday_{task}", code)
        self.assertNotIn('f"data_holiday', code)


if __name__ == "__main__":
    unittest.main()
