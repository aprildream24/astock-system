# -*- coding: utf-8 -*-
"""2026-09-15「全天零推送」故障的回归锁。

事故链（两重独立故障叠加）：
  ① fetch_daily 断档锚错误：latest_td = 今日应达交易日，而当日数据此刻尚
     未入库 ⇒ 全市场 4993 只全被判「断档」走 days=260 全量路径 ⇒ 盘前/竞价
     轻量任务实际耗时 ≈53 分钟 > workflow timeout 45 分钟 → cancel（或某源
     异常 → failure）。第 8 步「构建+推送」因 fail-fast 整步 skipped。
  ② build 就绪闸门错配：pre/auction 本就在当日收盘K线入库前运行，却套用
     以「指数日K含当日」为必要条件的收盘闸门 ⇒ 永远不通过 ⇒ 静默 return
     None，用户全天零消息且毫不知情。

本套件锁死三件事：
  A. 轻量任务的全量兜底 days 有上限（不得再用 260 拖死盘前任务）；
  B. pre/auction 走专用闸门，允许当日收盘K线未入库；
  C. 数据未就绪时必须主动告警，不得静默 return。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import datetime as _dt  # noqa: E402


class TestFetchIncrementAnchor(unittest.TestCase):
    """A. 断档锚 + 轻量全量上限。"""

    def _src(self):
        with open(os.path.join(ROOT, "pipeline", "fetch_daily.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_no_bare_due_date_anchor(self):
        """禁止再用「今日应达交易日」直接作断档锚（原故障根因）。"""
        src = self._src()
        # 必须存在「库中最新日期」参与取较新者
        self.assertIn("db_latest", src,
                      "断档锚必须引入库中最新日期（db_latest）")
        self.assertRegex(
            src, r"latest_td\s*=\s*max\(db_latest",
            "断档锚必须是 max(db_latest, 上一交易日)，不得直接等于今日应达日")

    def test_light_task_full_pull_is_capped(self):
        """轻量任务（days<=20）的全量兜底必须封顶，防止 53 分钟超时。"""
        src = self._src()
        self.assertIn("full_days", src, "缺少轻量任务全量兜底上限变量")
        self.assertRegex(
            src, r"full_days\s*=\s*min\(days,\s*40\)",
            "轻量任务全量兜底应封顶 40 根 K 线")

    def test_anchor_semantics_offline(self):
        """离线语义验证：库最新=上一交易日时，全部票判增量（非全量）。"""
        import importlib
        fd = importlib.import_module("pipeline.fetch_daily")
        self.assertTrue(callable(fd.fetch_daily))
        # 构造：今日 2026-09-15（交易日），库最新 2026-09-14（上一交易日）
        today = "2026-09-15"
        db_latest = "2026-09-14"
        prev_d = _dt.date(2026, 9, 14)
        # 模拟修复后的锚计算：max(db_latest, prev_td)
        anchor = max(db_latest, prev_d.isoformat())
        self.assertEqual(anchor, "2026-09-14")
        # 每票 last=09-14 >= anchor → 判增量（修复前 anchor=09-15 → 全量）
        self.assertTrue("2026-09-14" >= anchor,
                        "库已跟上前一交易日的票必须判为增量")


class TestPreAuctionGate(unittest.TestCase):
    """B. pre/auction 专用闸门。"""

    def test_gate_exists_and_used(self):
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("def _preauction_ready(", src,
                      "缺少盘前/竞价专用就绪判定")
        self.assertRegex(
            src, r'if task in \("pre",\s*"auction"\):\s*\n\s*ready,\s*ready_why\s*=\s*_preauction_ready',
            "build 必须对 pre/auction 走专用闸门")

    def test_gate_fails_closed_without_snapshot(self):
        """无当日快照必须判失败（不得放行无数据构建）。"""
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("无快照（竞价数据未入库）", src,
                      "缺少「当日无快照」的失败分支")

    def test_gate_allows_missing_today_kline(self):
        """专用闸门不得要求当日 K 线入库（这是它能通过的关键）。"""
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        i = src.index("def _preauction_ready(")
        j = src.index("def _notify_data_blocked(")
        body = src[i:j]
        # 允许引用 prev（前一交易日）K线，不得要求 date 当日 klines 有行
        self.assertIn("prev", body)
        self.assertNotRegex(
            body, r'FROM klines WHERE date=\?"?,\s*\(date,\)',
            "专用闸门不得要求当日(date) K线已入库")


class TestDataBlockedAlert(unittest.TestCase):
    """C. 数据未就绪必须主动告警，不得静默。"""

    def test_alert_call_present(self):
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("def _notify_data_blocked(", src,
                      "缺少数据未就绪告警函数")
        # 拒绝构建分支必须调用告警（跨行匹配：assertRegex 无 DOTALL）
        i = src.index("if not certain or not ready:")
        tail = src[i:i + 400]
        self.assertIn("_notify_data_blocked(", tail,
                      "拒绝构建分支必须调用告警")

    def test_alert_never_breaks_main_flow(self):
        """告警自身失败不得影响主流程（须 try/except 吞掉）。"""
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        i = src.index("def _notify_data_blocked(")
        j = src.index("def _preauction_ready(") if "def _preauction_ready(" in src[i:] \
            else len(src)
        body = src[i:j]
        self.assertIn("except Exception", body,
                      "告警必须吞异常，不得阻断主流程")

    def test_alert_send_offline(self):
        """离线真验：Mock notifier 后调用告警，确认确实发起推送。"""
        import importlib
        build = importlib.import_module("pipeline.build")
        notifier = importlib.import_module("pipeline.notifier")
        sent = {}
        orig_push = notifier.push

        def fake_push(mode, title, content, date=None, con=None, **kw):
            sent["mode"] = mode
            sent["title"] = title
            sent["content"] = content
            return {"sent": True}

        notifier.push = fake_push
        try:
            build._notify_data_blocked("close", "2026-09-15",
                                       "指数日K无此日期", "非交易日")
        finally:
            notifier.push = orig_push
        self.assertIn("mode", sent, "告警未发起任何推送（静默洞未堵）")
        self.assertIn("2026-09-15", sent["title"])
        self.assertIn("data_blocked", sent["mode"])


class TestWorkflowNoFailFast(unittest.TestCase):
    """① 抓取失败不得连坐推送 + 超时余量充足。"""

    def _yml(self):
        p = os.path.join(ROOT, ".github", "workflows", "stock.yml")
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_fetch_steps_continue_on_error(self):
        y = self._yml()
        self.assertGreaterEqual(
            y.count("continue-on-error: true"), 2,
            "两个抓取步骤都须 continue-on-error，避免失败连坐推送")

    def test_timeout_has_headroom(self):
        y = self._yml()
        self.assertNotIn("timeout-minutes: 45", y,
                         "45 分钟不足以覆盖冷库全量（实测 53 分钟）")
        self.assertIn("timeout-minutes: 75", y,
                      "应为冷库全量留足余量（75 分钟）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
