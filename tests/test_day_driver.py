# -*- coding: utf-8 -*-
"""纯 GitHub 日驱动 + 保留期清理 + live 循环并发守门的回归锁（2026-09-27）。

锁四组不变量：

A. 日驱动时点表 = 原 cron-job.org 权威时点表
   pre 08:50 / auction 09:25 / intraday-am 09:45 / audit-am 10:00 /
   pm 14:40 / close 15:22 / audit-close 15:45 / review 20:02 / audit 20:20。
   错一个时点 = 那个时刻的推送消失。

B. 双点火幂等：同日已有实例在跑/已成功 → 后到点火退出；
   失败实例不拦（备份点火要能接管）。

C. snapshot_live 保留期：live 每 10 分钟写 ~4500 行，不清理则
   GH cache 10GB 上限被无声吃穿。只许留今昨两天。

D. live 循环并发守门：多条触发路径（auction/am 顺链 + 手动）下
   已有实例在跑 → 后到退出。
"""
import datetime as _dt
import importlib
import io
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BJT = _dt.timezone(_dt.timedelta(hours=8))


def _mkcon():
    core = importlib.import_module("pipeline.core")
    con = sqlite3.connect(":memory:")
    con.executescript(core._SCHEMA)
    return con


class TestDriverPlan(unittest.TestCase):
    """A. 时点表必须与原 cron-job 权威时点逐字对齐。"""

    def setUp(self):
        self.dd = importlib.import_module("tools.day_driver")

    def test_morning_plan(self):
        self.assertEqual(
            [(t, k) for t, k, _ in self.dd.PLAN["morning"]],
            [("08:50", "pre"), ("09:25", "auction"),
             ("09:45", "intraday"), ("10:00", "pre")])
        self.assertEqual(self.dd.PLAN["morning"][2][2], {"slot": "am"})

    def test_afternoon_plan(self):
        self.assertEqual(
            [(t, k) for t, k, _ in self.dd.PLAN["afternoon"]],
            [("14:40", "intraday"), ("15:22", "close"),
             ("15:45", "close")])
        self.assertEqual(self.dd.PLAN["afternoon"][0][2], {"slot": "pm"})

    def test_evening_plan(self):
        self.assertEqual(
            [(t, k) for t, k, _ in self.dd.PLAN["evening"]],
            [("20:02", "review"), ("20:20", "review")])


class TestDriverBehavior(unittest.TestCase):
    """B. 幂等 / 非交易日 / dispatch 语义。"""

    def setUp(self):
        self.dd = importlib.import_module("tools.day_driver")

    def test_wait_until_past_returns_zero(self):
        now = _dt.datetime(2026, 9, 24, 9, 0, tzinfo=BJT)
        tgt, slept = self.dd.wait_until("08:50", now=now)
        self.assertEqual(slept, 0.0, "已过点必须立即执行（退化但不断链）")

    def test_wait_until_future_sleeps(self):
        now = _dt.datetime(2026, 9, 24, 8, 0, tzinfo=BJT)
        with mock.patch.object(self.dd.time, "sleep") as sl:
            tgt, slept = self.dd.wait_until("08:50", now=now)
        self.assertEqual(slept, 3000.0)
        sl.assert_called()                       # 分段睡（日志报活）

    def test_non_trade_day_exits_without_dispatch(self):
        con = _mkcon()
        # 2026-09-26 是周六
        with mock.patch.object(self.dd, "dispatch") as disp:
            n = self.dd.run_part("morning", "day-morning.yml", token="",
                                 repo="x/y",
                                 now=_dt.datetime(2026, 9, 26, 8, 0,
                                                  tzinfo=BJT))
        self.assertEqual(n, (0, 0), "非交易日 = 无事可做，不是失败")
        disp.assert_not_called()

    def test_run_part_dispatches_in_order(self):
        calls = []

        def fake_dispatch(token, repo, task, extra=None, retries=3):
            calls.append((task, dict(extra or {})))
            return True

        with mock.patch.object(self.dd, "dispatch", fake_dispatch), \
                mock.patch.object(self.dd.time, "sleep"):
            n = self.dd.run_part(
                "morning", "day-morning.yml", token="", repo="x/y",
                now=_dt.datetime(2026, 9, 24, 8, 0, tzinfo=BJT))
        self.assertEqual(n, (4, 4))
        self.assertEqual([c[0] for c in calls],
                         ["pre", "auction", "intraday", "pre"])
        self.assertEqual(calls[2][1], {"slot": "am"})

    def test_another_alive_semantics(self):
        """自我排除 / 他日排除 / 在跑或成功拦截 / 失败放行。"""
        cases = {
            "in_progress": True, "completed_success": True,
            "completed_failure": False,
        }
        for concl, expect in cases.items():
            runs = [{"id": 999, "status": "completed" if
                     concl.startswith("completed") else "in_progress",
                     "conclusion": None if concl == "in_progress"
                     else concl.split("_")[1],
                     "created_at": "2026-09-24T01:00:00Z"}]
            with mock.patch.object(
                    self.dd, "_req",
                    return_value=(200, {"workflow_runs": runs})),                     mock.patch.object(self.dd, "bj_now",
                                      lambda: _dt.datetime(2026, 9, 24, 9, 30,
                                                           tzinfo=BJT)):
                got = self.dd.another_alive("t", "x/y", "day-morning.yml",
                                            my_run_id=123)
            self.assertEqual(got, expect, f"{concl} 判定错误")

    def test_another_alive_ignores_self_and_other_days(self):
        runs = [
            {"id": 123, "status": "in_progress", "conclusion": None,
             "created_at": "2026-09-24T01:00:00Z"},   # 自己 → 排除
            {"id": 555, "status": "in_progress", "conclusion": None,
             "created_at": "2026-09-23T01:00:00Z"},   # 昨天 → 排除
        ]
        with mock.patch.object(self.dd, "_req",
                               return_value=(200, {"workflow_runs": runs})),                 mock.patch.object(self.dd, "bj_now",
                                  lambda: _dt.datetime(2026, 9, 24, 9, 30,
                                                       tzinfo=BJT)):
            self.assertFalse(self.dd.another_alive(
                "t", "x/y", "day-morning.yml", my_run_id=123))

    def test_workflow_files_match_plan(self):
        """三个 driver workflow 必须各自带 schedule 双点火/单点火，
        且时区一律 UTC（北京时间注释必须在场，防后人改成北京时刻）。"""
        for wf, crons in (
                ("day-morning.yml", ("50 23 * * 0-4", "25 0 * * 1-5")),
                ("day-afternoon.yml", ("50 5 * * 1-5", "20 6 * * 1-5")),
                ("day-evening.yml", ("30 11 * * 1-5",))):
            src = open(os.path.join(ROOT, ".github", "workflows", wf),
                       encoding="utf-8").read()
            for c in crons:
                self.assertIn(f'cron: "{c}"', src, f"{wf} 缺点火 {c}")
            self.assertIn("北京", src)
            self.assertIn("tools.day_driver", src)


class TestRetentionPurge(unittest.TestCase):
    """C. snapshot_live 保留期（膨胀 bug 回归锁）。"""

    def test_purge_keeps_today_and_yesterday(self):
        intra = importlib.import_module("pipeline.intraday")
        con = _mkcon()
        rows = []
        for d, slot in (("2026-09-10", "live"), ("2026-09-22", "live"),
                        ("2026-09-23", "am"), ("2026-09-24", "live"),
                        ("2026-10-01", "live")):   # 未来脏数据也必须清
            rows.append((d, slot, "sh600519", "茅台", 20.0, 1.0, 3e8))
        con.executemany("INSERT OR REPLACE INTO snapshot_live VALUES"
                        "(?,?,?,?,?,?,?)", rows)
        intra._purge_old(con, "2026-09-24")
        left = {r[0] for r in con.execute(
            "SELECT DISTINCT date FROM snapshot_live").fetchall()}
        self.assertEqual(left, {"2026-09-23", "2026-09-24"},
                         f"必须只留今昨两天，实际 {left}")

    def test_purge_keeps_7d_alerts(self):
        intra = importlib.import_module("pipeline.intraday")
        con = _mkcon()
        con.executemany(
            "INSERT OR REPLACE INTO live_alerts VALUES(?,?,?,?,?)",
            [("2026-09-10", "zone", "sh600519", "", ""),
             ("2026-09-19", "zone", "sh600519", "", ""),
             ("2026-09-24", "zone", "sh600519", "", "")])
        intra._purge_old(con, "2026-09-24")
        left = {r[0] for r in con.execute(
            "SELECT date FROM live_alerts").fetchall()}
        self.assertEqual(left, {"2026-09-19", "2026-09-24"})

    def test_purge_runs_on_every_intraday_call(self):
        """窗口内的每次巡检都必须先清理（结构性检查，防止调用被挪丢）。"""
        src = open(os.path.join(ROOT, "pipeline", "intraday.py"),
                   encoding="utf-8").read()
        i = src.find("\n    _purge_old(con, date)")   # 缩进的调用点
        j = src.find("def run(")
        self.assertGreater(i, j, "_purge_old 必须在 run() 内被调用")


class TestLiveLoopGuard(unittest.TestCase):
    """D. live 循环并发守门。"""

    def setUp(self):
        self.ll = importlib.import_module("tools.live_loop")

    def _fake_urlopen(self, runs):
        payload = json.dumps({"workflow_runs": runs}).encode()

        def fake(req, timeout=25):
            return io.BytesIO(payload)
        return fake

    def test_blocks_when_other_instance_running(self):
        runs = [{"id": 999, "status": "in_progress", "conclusion": None,
                 "created_at": "2026-09-24T01:00:00Z"}]
        with mock.patch("urllib.request.urlopen", self._fake_urlopen(runs)):
            with mock.patch.object(self.ll, "_bj_now",
                                   lambda: _dt.datetime(2026, 9, 24, 9, 30,
                                                        tzinfo=BJT)):
                self.assertTrue(self.ll._already_running("t", "x/y", 123))

    def test_success_exit_does_not_block(self):
        """09-30 二次修：守门退出/正常跑完的 success 实例不算占用——
        否则一次 30 秒的守门退出会把当天后续派发全部挡死（实测踩坑）。"""
        runs = [{"id": 999, "status": "completed", "conclusion": "success",
                 "created_at": "2026-09-24T01:00:00Z"}]
        with mock.patch("urllib.request.urlopen", self._fake_urlopen(runs)):
            with mock.patch.object(self.ll, "_bj_now",
                                   lambda: _dt.datetime(2026, 9, 24, 9, 30,
                                                        tzinfo=BJT)):
                self.assertFalse(self.ll._already_running("t", "x/y", 123))
        queued = [{"id": 999, "status": "queued", "conclusion": None,
                   "created_at": "2026-09-24T01:00:00Z"}]
        with mock.patch("urllib.request.urlopen", self._fake_urlopen(queued)):
            with mock.patch.object(self.ll, "_bj_now",
                                   lambda: _dt.datetime(2026, 9, 24, 9, 30,
                                                        tzinfo=BJT)):
                self.assertTrue(self.ll._already_running("t", "x/y", 123),
                                "排队中也要算占用")

    def test_ignores_self_and_failures_and_other_days(self):
        runs = [
            {"id": 123, "status": "in_progress", "conclusion": None,
             "created_at": "2026-09-24T01:00:00Z"},          # 自己
            {"id": 777, "status": "completed", "conclusion": "failure",
             "created_at": "2026-09-24T02:00:00Z"},          # 失败
            {"id": 888, "status": "in_progress", "conclusion": None,
             "created_at": "2026-09-23T01:00:00Z"},          # 昨天
        ]
        with mock.patch("urllib.request.urlopen", self._fake_urlopen(runs)):
            with mock.patch.object(self.ll, "_bj_now",
                                   lambda: _dt.datetime(2026, 9, 24, 9, 30,
                                                        tzinfo=BJT)):
                self.assertFalse(self.ll._already_running("t", "x/y", 123))

    def test_no_token_passes_through(self):
        self.assertFalse(self.ll._already_running("", "x/y", 123),
                         "无 token 必须放行（事件去重兜底）")

    def test_am_fallback_wired_in_stock_yml(self):
        src = open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                   encoding="utf-8").read()
        i = src.find("触发盘中买点巡检长循环")
        self.assertGreater(i, 0)
        seg = src[i:src.find("- name:", i + 10)]
        self.assertIn("== 'auction'", seg)
        self.assertIn("slot == 'am'", seg, "am 备份点火必须接线")


class TestCronjobRetired(unittest.TestCase):
    """cron-job 定时器退役闸：置位后守门绝不再报"缺失定时器"。"""

    def test_retired_gate_silences_timer_check(self):
        tg = importlib.import_module("pipeline.timer_guard")
        problems = []
        with mock.patch.dict(os.environ, {"ASTOCK_CRONJOB_RETIRED": "1"}):
            tg._check_timers(problems)
        self.assertEqual(problems, [])

    def test_watchdog_sets_retired_flag(self):
        src = open(os.path.join(ROOT, ".github", "workflows", "watchdog.yml"),
                   encoding="utf-8").read()
        self.assertIn("ASTOCK_CRONJOB_RETIRED", src)


if __name__ == "__main__":
    unittest.main()
