# -*- coding: utf-8 -*-
"""定时器守门（pipeline/timer_guard.py）回归锁。

为什么必须有：主链权威触发**全部**来自 cron-job.org。定时器没了
⇒ 零 run ⇒ 零推送 ⇒ 彻底静默，而云端 watchdog 自己也是靠这些定时器
触发的（"守夜人睡着了"）—— 这一层只能由独立守门覆盖。
同时它也是"误报源"的候选：判错就会半夜推一条假告警，故误报边界要钉死。
"""
import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import timer_guard as tg  # noqa: E402


def _job(title, enabled=True):
    return {"jobId": abs(hash(title)) % 9999, "title": title, "enabled": enabled,
            "schedule": {"hours": [8], "minutes": [50]}}


def _all_jobs(enabled=True):
    return [_job(t, enabled) for t in tg.REQUIRED]


class TestAuditJobs(unittest.TestCase):
    def test_all_present_and_enabled(self):
        r = tg.audit_jobs(_all_jobs())
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["disabled"], [])
        self.assertEqual(len(r["titles"]), len(tg.REQUIRED))

    def test_missing_detected(self):
        jobs = [j for j in _all_jobs() if j["title"] != "astock-close"]
        r = tg.audit_jobs(jobs)
        self.assertEqual(r["missing"], ["astock-close"])
        self.assertEqual(r["disabled"], [])

    def test_disabled_detected(self):
        jobs = [j for j in _all_jobs() if j["title"] != "astock-review"]
        jobs.append(_job("astock-review", enabled=False))
        r = tg.audit_jobs(jobs)
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["disabled"], ["astock-review"])

    def test_other_system_jobs_not_flagged(self):
        """另一套项目（fisk9r/stock-analysis）的任务与本题无关，不得误判。"""
        jobs = _all_jobs() + [_job("stock-anomaly-am"), _job("exec-patrol-1"),
                              _job("返利站数据更新")]
        r = tg.audit_jobs(jobs)
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["disabled"], [])
        self.assertEqual(r["other"], 0)

    def test_unknown_jobs_reported_as_other_only(self):
        jobs = _all_jobs() + [_job("someone-else")]
        r = tg.audit_jobs(jobs)
        self.assertEqual(r["other"], 1)
        self.assertEqual(r["missing"], [])

    def test_empty_and_dirty_input_safe(self):
        self.assertEqual(len(tg.audit_jobs([])["missing"]), len(tg.REQUIRED))
        self.assertEqual(len(tg.audit_jobs(None)["missing"]), len(tg.REQUIRED))
        # 无 title 的任务不得让守门崩
        r = tg.audit_jobs([{"enabled": True}, _job("astock-pre")])
        self.assertIn("astock-pre", r["titles"])


class TestCli(unittest.TestCase):
    def setUp(self):
        p = mock.patch("pipeline.notifier.push",
                       side_effect=AssertionError("不该推送"))
        self.push = p.start()
        self.addCleanup(p.stop)

    def _run(self, argv, jobs, err=None):
        tg.fetch_jobs = mock.Mock(return_value=(list(jobs), err))
        return tg.main(argv)

    def test_no_key_skips_without_network(self):
        tg.fetch_jobs = mock.Mock(side_effect=AssertionError("不该出网"))
        with mock.patch.dict(os.environ, {"CRONJOB_API_KEY": ""}, clear=False):
            self.assertEqual(tg.main(["--dry"]), 0)

    def test_network_error_does_not_alert(self):
        """网络抖动 ≠ 定时器故障：绝不能半夜误吵。"""
        with mock.patch.dict(os.environ, {"CRONJOB_API_KEY": "k"}):
            rc = self._run([], [], err="URLError: timed out")
        self.assertEqual(rc, 0)
        self.push.assert_not_called()

    def test_all_ok_is_silent(self):
        with mock.patch.dict(os.environ, {"CRONJOB_API_KEY": "k"}):
            rc = self._run([], _all_jobs())
        self.assertEqual(rc, 0)
        self.push.assert_not_called()

    def test_missing_timer_alerts(self):
        with mock.patch.dict(os.environ, {"CRONJOB_API_KEY": "k"}):
            rc = self._run([], [j for j in _all_jobs()
                               if j["title"] != "astock-close"])
        self.assertEqual(rc, 1)
        self.push.assert_called_once()
        args, kw = self.push.call_args
        self.assertEqual(args[0], "watchdog_alert")
        self.assertTrue(kw.get("force"), "定时器故障属确定性事故，须绕过去重")

    def test_disabled_timer_alerts(self):
        jobs = [j for j in _all_jobs() if j["title"] != "astock-audit-review"]
        jobs.append(_job("astock-audit-review", enabled=False))
        with mock.patch.dict(os.environ, {"CRONJOB_API_KEY": "k"}):
            rc = self._run([], jobs)
        self.assertEqual(rc, 1)
        self.push.assert_called_once()

    def test_auth_failure_alerts(self):
        """key 失效 = 守门自己瞎了，必须让人知道。"""
        with mock.patch.dict(os.environ, {"CRONJOB_API_KEY": "k"}):
            rc = self._run([], [], err="HTTP 403")
        self.assertEqual(rc, 1)
        self.push.assert_called_once()

    def test_dry_never_pushes(self):
        with mock.patch.dict(os.environ, {"CRONJOB_API_KEY": "k"}):
            rc = self._run(["--dry"], [])
        self.assertEqual(rc, 0)
        self.push.assert_not_called()


class TestWiring(unittest.TestCase):
    def test_required_covers_intraday_and_audit(self):
        for t in ("astock-pre", "astock-auction", "astock-close", "astock-review",
                  "astock-intraday-am", "astock-intraday-pm",
                  "astock-audit-am", "astock-audit-close", "astock-audit-review"):
            self.assertIn(t, tg.REQUIRED)

    def test_titles_are_exact_no_wildcard(self):
        """云端定时器由 setup 脚本按 title 匹配做"先删旧再建新" ⇒
        标题必须是可精确匹配的常量字符串（带通配会批量误删/重建）。"""
        for t in tg.REQUIRED:
            self.assertRegex(t, r"^astock-[a-z-]+$")

    def test_guard_does_not_touch_other_system(self):
        """另一套项目的任务前缀必须被认识，避免守门把它们当"多余"清理。"""
        self.assertIn("stock-", tg.OTHER_PREFIX)
        self.assertIn("exec-", tg.OTHER_PREFIX)

    def test_guard_is_read_only(self):
        with open(os.path.join(ROOT, "pipeline", "timer_guard.py"),
                  encoding="utf-8") as f:
            src = f.read()
        # 只允许 GET：不得出现任何写操作 / 改定时器的调用
        self.assertNotIn("method=\"PUT\"", src)
        self.assertNotIn("method=\"DELETE\"", src)
        self.assertNotIn("/jobs/", src)      # 改单个任务走 /jobs/{id}

    def test_watchdog_workflow_runs_timer_guard(self):
        with open(os.path.join(ROOT, ".github", "workflows", "watchdog.yml"),
                  encoding="utf-8") as f:
            w = f.read()
        self.assertIn("timer_guard", w)
        self.assertIn("CRONJOB_API_KEY", w)

    def test_no_ternary_literal_in_watchdog_wf(self):
        with open(os.path.join(ROOT, ".github", "workflows", "watchdog.yml"),
                  encoding="utf-8") as f:
            w = f.read()
        self.assertIsNone(re.search(r"\$\{\{[^{}]*\?[^{}]*\}\}", w))


if __name__ == "__main__":
    unittest.main(verbosity=2)
