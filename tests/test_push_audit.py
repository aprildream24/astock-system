# -*- coding: utf-8 -*-
"""推送验收/自动补发（pipeline/push_audit.py）回归锁。

为什么必须有这套测试：
  验收器的失效方式比生产者更隐蔽——它一旦"判错"，要么**静默放过**
  真事故（等于没有验收），要么**误判成缺失**而反复补发（自己变成
  新的重复推送源）。两条都必须用断言钉死。

血案背景（2026-09-16 08:50）：CI 每一步 conclusion 都是 success，但
账本里 `build_pre` 状态是 `uncertain`，用户端零消息 —— 只看步骤结论
必然漏。故本套件的核心用例就是「uncertain 不算 sent」。
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import push_audit as pa  # noqa: E402

CST = timezone(timedelta(hours=8))
D = "2026-09-16"                 # 周三，交易日


def _ledger(rows):
    """rows: [(mode, ts, status)] → 账本 dict（key 用 mode 做可读标识）。"""
    out = {}
    for i, (mode, ts, status) in enumerate(rows):
        out[f"k{i}"] = {"mode": mode, "ts": ts, "status": status,
                        "channels": {"pushplus": status}}
    return out


def _at(hm):
    return datetime.strptime(f"{D} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=CST)


class Base(unittest.TestCase):
    def setUp(self):
        # 一律不允许真实出网：任何未 mock 的网络调用都会显式失败。
        for fn in ("remote_ledger", "busy_runs", "dispatch"):
            p = mock.patch.object(pa, fn, side_effect=AssertionError(
                f"测试不得调用 {fn}（未 mock，会出网）"))
            p.start()
            self.addCleanup(p.stop)


# ---------------------------------------------------------------------------
# 1. 账本解析
# ---------------------------------------------------------------------------

class TestLedgerParse(Base):
    def test_rows_today_filters_and_sorts(self):
        led = _ledger([
            ("build_pre", f"{D} 08:50:00", "sent"),
            ("build_close", "2026-09-15 15:22:00", "sent"),
            ("build_auction", f"{D} 09:25:00", "sent"),
        ])
        rows = pa.rows_today(led, D)
        self.assertEqual([r["mode"] for r in rows],
                         ["build_pre", "build_auction"])

    def test_uncertain_is_not_sent(self):
        """★ 核心血案：uncertain 不代表送达，绝不能算 sent。"""
        led = _ledger([("build_pre", f"{D} 09:00:00", "uncertain")])
        self.assertEqual(pa.sent_modes(led, D), set())

    def test_failed_is_not_sent(self):
        led = _ledger([("build_close", f"{D} 15:22:00", "failed")])
        self.assertEqual(pa.sent_modes(led, D), set())

    def test_alert_modes_detected(self):
        led = _ledger([
            ("data_blocked_close", f"{D} 08:25:00", "sent"),
            ("data_holiday", f"{D} 08:00:00", "sent"),
            ("build_close", f"{D} 15:22:00", "sent"),
        ])
        self.assertEqual(pa.alerted_modes(led, D),
                         {"data_blocked_close", "data_holiday"})

    def test_empty_and_dirty_ledger_safe(self):
        self.assertEqual(pa.rows_today({}, D), [])
        self.assertEqual(pa.rows_today(None, D), [])
        # 脏数据（值不是 dict / 缺字段）不得抛异常
        self.assertEqual(pa.rows_today({"a": "oops", "b": None}, D), [])
        led = {"a": {"mode": "build_close"}}          # 缺 ts
        self.assertEqual(pa.rows_today(led, D), [])


# ---------------------------------------------------------------------------
# 2. 判定矩阵
# ---------------------------------------------------------------------------

class TestAnalyze(Base):
    def test_all_sent_is_ok(self):
        led = _ledger([("build_pre", f"{D} 08:50:00", "sent"),
                       ("build_auction", f"{D} 09:25:00", "sent")])
        r = pa.analyze(led, D, ["pre", "auction"], now=_at("10:00"))
        self.assertEqual(len(r["ok"]), 2)
        self.assertEqual(r["missing"], [])

    def test_missing_when_due_and_no_alert(self):
        r = pa.analyze(_ledger([]), D, ["close"], now=_at("15:45"))
        self.assertEqual([m["task"] for m in r["missing"]], ["close"])
        self.assertEqual(r["warn"], [])

    def test_pending_before_due(self):
        """未到点不得判缺失（否则会跟正常定时触发打架）。"""
        r = pa.analyze(_ledger([]), D, ["close"], now=_at("15:00"))
        self.assertEqual([m["task"] for m in r["pending"]], ["close"])
        self.assertEqual(r["missing"], [])

    def test_alert_makes_it_warn_not_missing(self):
        """已发显式告警 = 系统已主动告知用户，不算静默失败 → 不补发。"""
        led = _ledger([("data_blocked_close", f"{D} 15:23:00", "sent")])
        r = pa.analyze(led, D, ["close"], now=_at("15:45"))
        self.assertEqual([w["task"] for w in r["warn"]], ["close"])
        self.assertEqual(r["missing"], [])

    def test_review_needs_both_modes(self):
        """review 的 build_review 发了但 narrative 没发 → 仍算缺失。"""
        led = _ledger([("build_review", f"{D} 20:02:00", "sent")])
        r = pa.analyze(led, D, ["review"], now=_at("20:20"))
        self.assertEqual(len(r["ok"]), 0)
        self.assertEqual([m["modes"] for m in r["missing"]], [["narrative"]])

    def test_unknown_task_is_noop(self):
        """site / intraday 本就不产出这些 mode → 不得误报缺失。"""
        for t in ("site", "intraday", "watch-sync"):
            r = pa.analyze(_ledger([]), D, [t], now=_at("23:00"))
            self.assertEqual(r["missing"], [], f"{t} 不该被判缺失")


# ---------------------------------------------------------------------------
# 3. 补发计划
# ---------------------------------------------------------------------------

class TestPlan(Base):
    def test_plan_dedups_shared_task(self):
        """build_review 与 narrative 共用 review → 只补发一次。"""
        plan = pa._plan_dispatches([
            {"task": "review", "modes": ["build_review", "narrative"]}])
        self.assertEqual(plan, ["review"])

    def test_plan_covers_all_four(self):
        plan = pa._plan_dispatches([
            {"task": "pre", "modes": ["build_pre"]},
            {"task": "auction", "modes": ["build_auction"]},
            {"task": "close", "modes": ["build_close"]},
            {"task": "review", "modes": ["build_review"]},
        ])
        self.assertEqual(sorted(plan),
                         ["auction", "close", "pre", "review"])

    def test_expect_map_is_complete(self):
        self.assertEqual(pa.TASK_EXPECT["review"], ["build_review", "narrative"])
        for m in ("build_pre", "build_auction", "build_close",
                  "build_review", "narrative"):
            self.assertIn(m, pa.MODE_TASK)
        self.assertEqual(pa.MODE_TASK["narrative"], "review")
        # 四个主任务都要能被 slot 覆盖
        flat = {t for v in pa.SLOT_TASKS.values() for t in v}
        self.assertEqual(flat, {"pre", "auction", "close", "review"})


# ---------------------------------------------------------------------------
# 4. CLI 行为（含"不许乱补发"的保守边界）
# ---------------------------------------------------------------------------

class TestCli(Base):
    def _run(self, argv, ledger, busy=(), dispatched=None, now="15:45"):
        """now 必须显式钉死：`due()` 默认读真实钟点，否则本套件在 15:00 前
        跑会全部落进 pending（测试随"跑的时刻"变色 = 假绿/假红）。"""
        dispatched = dispatched if dispatched is not None else []
        pa.remote_ledger = mock.Mock(return_value=ledger)
        pa.busy_runs = mock.Mock(return_value=list(busy))
        pa.dispatch = mock.Mock(
            side_effect=lambda t, *a, **k: (dispatched.append(t), (True, "204"))[1])
        return pa.main(list(argv) + ["--now", now]), dispatched

    def test_non_trade_day_skips(self):
        # 2026-09-19 是周六
        rc, d = self._run(["--slot", "close", "--date", "2026-09-19"], {})
        self.assertEqual(rc, 0)
        self.assertEqual(d, [])

    def test_unreachable_ledger_never_dispatches(self):
        """★ 读不到账本 = 无法证明"没发" ⇒ 绝不动作（否则自己成了重复源）。"""
        rc, d = self._run(["--slot", "close", "--date", D], None)
        self.assertEqual(rc, 0)
        self.assertEqual(d, [])

    def test_busy_run_blocks_dispatch(self):
        rc, d = self._run(["--slot", "close", "--date", D], {}, busy=[{"id": 1}])
        self.assertEqual(rc, 0)
        self.assertEqual(d, [])

    def test_missing_close_dispatches_once(self):
        rc, d = self._run(["--slot", "close", "--date", D], {})
        self.assertEqual(rc, 0)
        self.assertEqual(d, ["close"])

    def test_review_slot_also_covers_close(self):
        """15:45 那次审计常被"收盘 run 还在跑"保守跳过 → 20:20 必须兜住
        「收盘推送丢了」这个最严重场景。"""
        rc, d = self._run(["--slot", "review", "--date", D], {}, now="20:20")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(d), ["close", "review"])

    def test_dry_run_does_not_dispatch(self):
        rc, d = self._run(["--slot", "close", "--date", D, "--dry"], {})
        self.assertEqual(rc, 0)
        self.assertEqual(d, [])

    def test_task_mode_reports_but_never_self_dispatches(self):
        """跑完即自检：只报告（rc=1），补发交 watchdog —— 同一 run 里
        再触发自己只会白烧额度。"""
        rc, d = self._run(["--task", "close", "--date", D], {})
        self.assertEqual(rc, 1)
        self.assertEqual(d, [])

    def test_task_mode_ok_when_sent(self):
        led = _ledger([("build_close", f"{D} 15:22:00", "sent")])
        rc, d = self._run(["--task", "close", "--date", D], led)
        self.assertEqual(rc, 0)
        self.assertEqual(d, [])

    def test_dispatch_failure_returns_1(self):
        pa.remote_ledger = mock.Mock(return_value={})
        pa.busy_runs = mock.Mock(return_value=[])
        pa.dispatch = mock.Mock(return_value=(False, "HTTP 403"))
        rc = pa.main(["--slot", "close", "--date", D, "--now", "15:45"])
        self.assertEqual(rc, 1)

    def test_bad_now_is_usage_error(self):
        rc = pa.main(["--slot", "close", "--date", D, "--now", "25:99"])
        self.assertEqual(rc, 2)

    def test_no_args_noop(self):
        rc = pa.main(["--date", D])
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# 5. 布线 / 边界（源码级断言）
# ---------------------------------------------------------------------------

def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestWiring(unittest.TestCase):
    def test_audit_never_pushes(self):
        """验收器绝不能自己发推送 —— 否则"验收"就成了第三个推送源。"""
        src = _src("pipeline/push_audit.py")
        self.assertNotIn("notifier.push", src)
        self.assertNotIn("from . import notifier", src)

    def test_audit_writes_no_file(self):
        """只读：不得写任何文件（尤其不能碰账本）。
        注意 `urlopen(` 含 "open("、`json.dumps(` 含 "json.dump"，都必须用
        词边界排除，否则假 FAIL（本套件自己就先踩了一次）。"""
        src = _src("pipeline/push_audit.py")
        self.assertIsNone(re.search(r"(?<![A-Za-z_.])open\(", src),
                          "验收器不得直接 open() 文件")
        self.assertIsNone(re.search(r"json\.dump\(", src),
                          "验收器不得写 JSON 文件")

    def test_watchdog_workflow_exists_and_wired(self):
        w = _src(".github/workflows/watchdog.yml")
        self.assertIn("push_audit", w)
        self.assertIn("--slot", w)
        self.assertIn("secrets.GH_PAT", w)
        self.assertIn("workflow_dispatch", w)

    def test_workflow_files_have_no_ternary_literal(self):
        """★ GitHub 会在 run 块**内**的注释里照样解析花括号表达式，
        三元字面量会让整份 workflow 解析失败（dispatch 422、零 job）。
        workflow 的断言必须查**原文**，不能剥注释（与 Python 源码相反）。"""
        pat = re.compile(r"\$\{\{[^{}]*\?[^{}]*\}\}")
        for rel in (".github/workflows/stock.yml",
                    ".github/workflows/watchdog.yml"):
            self.assertIsNone(pat.search(_src(rel)),
                              f"{rel} 含三元表达式字面量 → workflow 会解析失败")

    def test_stock_workflow_has_selfcheck_step(self):
        w = _src(".github/workflows/stock.yml")
        self.assertIn("推送验收（自检）", w)
        self.assertIn("push_audit --task", w)

    def test_selfcheck_step_cannot_break_main_chain(self):
        """体检步骤必须 continue-on-error —— 新增步骤连坐推送是最贵的事故。"""
        w = _src(".github/workflows/stock.yml")
        i = w.find("推送验收（自检）")
        self.assertGreater(i, 0)
        block = w[i:i + 700]
        self.assertIn("continue-on-error: true", block)

    def test_site_task_no_longer_runs_full_market_fetch(self):
        """site 是纯展示层：数据新鲜度由 close 负责，site 自己再拉全市场
        纯属白烧 ~6 分钟（site 正是验证 CI 新代码最常用的入口）。
        断言必须只取该步骤的 if 块——注释里会提到旧写法。"""
        w = _src(".github/workflows/stock.yml")
        i = w.find("- name: 收盘数据抓取（全市场）")
        self.assertGreater(i, 0)
        block = w[i:i + 1200]
        cond = block[block.find("if: >-"):block.find("continue-on-error")]
        self.assertNotIn("'site'", cond,
                         "site 不得再进全市场抓取条件（白烧 6 分钟）")
        self.assertIn("'close'", cond)

    def test_intraday_still_isolated(self):
        """M41 的零污染红线不得被本轮改动破坏。"""
        w = _src(".github/workflows/stock.yml")
        self.assertIn("--task intraday", w)
        src = _src("pipeline/push_audit.py")
        self.assertNotIn("fetch_daily", src)
        self.assertNotIn("fetch_universe", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
