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


class TestChainStatus(unittest.TestCase):
    """主链卡死判定：连续失败才告警，且要能区分「补发救不了」的情形。"""

    def _runs(self, conclusions):
        return [{"id": 1000 + i, "status": "completed",
                 "conclusion": c, "created_at": "2026-09-16T07:00:00Z"}
                for i, c in enumerate(conclusions)]

    def _patch(self, conclusions, steps=(), err=None):
        def fake(url, token=None, timeout=25):
            if err:
                raise OSError(err)
            if "workflows" in url:
                return {"workflow_runs": self._runs(conclusions)}
            return {"jobs": []}
        for m in (mock.patch.object(tg, "_get_json", side_effect=fake),
                  mock.patch.object(tg, "failed_steps",
                                    return_value=list(steps))):
            m.start()
            self.addCleanup(m.stop)

    def test_api_unreachable_is_unknown_never_alerts(self):
        self._patch([], err="boom")
        self.assertEqual(tg.chain_status()[0], "unknown")

    def test_too_few_completed_runs_is_unknown(self):
        self._patch(["failure", "failure"])
        self.assertEqual(tg.chain_status()[0], "unknown")

    def test_any_success_means_ok(self):
        self._patch(["failure", "success", "failure"])
        self.assertEqual(tg.chain_status()[0], "ok")

    def test_regression_failure_is_blocked_and_says_backfill_useless(self):
        self._patch(["failure"] * 3, steps=["回归自检"])
        state, detail = tg.chain_status()
        self.assertEqual(state, "blocked")
        self.assertIn("回归自检", detail)
        self.assertIn("补发救不了", detail,
                      "必须点明补发无用，否则会陷入无声的补发循环")

    def test_other_step_failure_reports_step_names(self):
        self._patch(["failure"] * 3, steps=["收盘数据抓取（全市场）"])
        state, detail = tg.chain_status()
        self.assertEqual(state, "blocked")
        self.assertIn("收盘数据抓取", detail)
        self.assertNotIn("补发救不了", detail)


class TestCli(unittest.TestCase):
    def setUp(self):
        # 两类网络调用一律不许真发：任何未 mock 的调用都显式失败。
        p = mock.patch("pipeline.notifier.push",
                       side_effect=AssertionError("不该推送"))
        self.push = p.start()
        self.addCleanup(p.stop)
        for name in ("fetch_jobs", "chain_status"):
            m = mock.patch.object(
                tg, name, side_effect=AssertionError(f"不该调用 {name}"))
            m.start()
            self.addCleanup(m.stop)
        self.env = mock.patch.dict(os.environ, {"CRONJOB_API_KEY": "k"},
                                   clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, argv, jobs, err=None, chain=("ok", "")):
        tg.fetch_jobs = mock.Mock(return_value=(list(jobs), err))
        tg.chain_status = mock.Mock(return_value=chain)
        return tg.main(argv)

    def test_no_key_skips_without_network(self):
        """没配 key 时定时器检查不得出网（否则等于把守门变成网络依赖）。"""
        with mock.patch.dict(os.environ, {"CRONJOB_API_KEY": ""}, clear=False):
            rc = self._run(["--dry"], [])
        self.assertEqual(rc, 0)
        tg.fetch_jobs.assert_not_called()
        self.push.assert_not_called()

    def test_network_error_does_not_alert(self):
        """网络抖动 ≠ 定时器故障：绝不能半夜误吵。"""
        rc = self._run([], [], err="URLError: timed out")
        self.assertEqual(rc, 0)
        self.push.assert_not_called()

    def test_all_ok_is_silent(self):
        rc = self._run([], _all_jobs())
        self.assertEqual(rc, 0)
        self.push.assert_not_called()

    def test_missing_timer_alerts(self):
        rc = self._run([], [j for j in _all_jobs()
                           if j["title"] != "astock-close"])
        self.assertEqual(rc, 1)
        self.push.assert_called_once()
        args, kw = self.push.call_args
        self.assertEqual(args[0], "watchdog_alert")
        self.assertTrue(kw.get("force"), "基础设施故障属确定性事故，须绕过去重")

    def test_disabled_timer_alerts(self):
        jobs = [j for j in _all_jobs() if j["title"] != "astock-audit-review"]
        jobs.append(_job("astock-audit-review", enabled=False))
        rc = self._run([], jobs)
        self.assertEqual(rc, 1)
        self.push.assert_called_once()

    def test_auth_failure_alerts(self):
        """key 失效 = 守门自己瞎了，必须让人知道。"""
        rc = self._run([], [], err="HTTP 403")
        self.assertEqual(rc, 1)
        self.push.assert_called_once()

    def test_chain_blocked_alerts_even_when_timers_fine(self):
        """定时器全好但代码坏了 ⇒ 依然告警（09-16 实测两例：全天零推送）。"""
        rc = self._run([], _all_jobs(),
                       chain=("blocked", "最近 3 个 run 全部失败：回归自检"))
        self.assertEqual(rc, 1)
        self.push.assert_called_once()
        self.assertIn("回归自检", self.push.call_args[0][2])

    def test_chain_unknown_does_not_alert(self):
        rc = self._run([], _all_jobs(), chain=("unknown", "API 不可达"))
        self.assertEqual(rc, 0)
        self.push.assert_not_called()

    def test_dry_never_pushes(self):
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
        # 链路检查要读 Actions API（有 GH_PAT 才不会撞匿名限额），
        # 且告警必须能真实送达 ⇒ 推送凭据也必须在场。
        self.assertIn("secrets.GH_PAT", w)
        self.assertIn("secrets.PUSHPLUS_TOKEN", w)

    def test_guard_covers_chain_not_only_timers(self):
        """守门必须同时覆盖「主链卡死」——09-16 实测两例全天零推送都属这一类，
        而补发救不了（同一份坏代码照样挂），只能靠告警让人来修。"""
        self.assertTrue(callable(tg.chain_status))
        with open(os.path.join(ROOT, "pipeline", "timer_guard.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("CHAIN_LOOK", src)
        self.assertIn("补发救不了", src)

    def test_no_ternary_literal_in_watchdog_wf(self):
        with open(os.path.join(ROOT, ".github", "workflows", "watchdog.yml"),
                  encoding="utf-8") as f:
            w = f.read()
        self.assertIsNone(re.search(r"\$\{\{[^{}]*\?[^{}]*\}\}", w))


if __name__ == "__main__":
    unittest.main(verbosity=2)
