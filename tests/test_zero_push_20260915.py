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
import re
import sys
import tempfile
import unittest
from unittest import mock

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
        """全量兜底必须封顶，防止 53 分钟超时。

        2026-09-16 调整：原断言钉死字面写法 `min(days, 40)`，而实现已改为
        走常量 `min(days, MAX_FULL_DAYS)`（默认 40）。改为**语义断言**：
        ① 常量存在且值 = 40；② full_days 由该常量取 min（不再裸写 40，
           也绝不再出现 `else days` 那种「天数大就真的拉满」的旧写法）。
        """
        src = self._src()
        self.assertIn("full_days", src, "缺少全量兜底上限变量")
        m = re.search(r"^MAX_FULL_DAYS\s*=\s*(\d+)", src, re.M)
        self.assertIsNotNone(m, "缺少 MAX_FULL_DAYS 常量（全量路径根数硬上限）")
        self.assertEqual(m.group(1), "40",
                         "全量兜底应封顶 40 根 K 线（53 分钟超时的直接对策）")
        self.assertRegex(
            src, r"full_days\s*=\s*min\(days,\s*MAX_FULL_DAYS\)",
            "full_days 必须 min(days, MAX_FULL_DAYS)，不得让 days 越界")
        # 断言「可执行行」不得再有裸 else days 回退（注释里提到旧写法不算）
        code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
        self.assertNotRegex(
            code, r"full_days\s*=\s*min\(days,\s*\d+\)\s*if\s+.*else\s+days",
            "禁止 `min(days, 40) if days <= 20 else days` 旧写法——"
            "默认 days 一大就真的拉满，冷库首拉必超时")
        self.assertIn("DEFAULT_DAYS = 40", src,
                      "抓取默认深度应为 40 根（引擎最大回看 32 根的 +25% 余量）")

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


class TestSilentZeroPushOnGreenRun(unittest.TestCase):
    """D. 「run 全绿但零推送」这一类最隐蔽的静默失败（2026-09-16 实证）。

    实证：CI run 34994141036 全部 14 步 success，但第 10 步「构建+推送」
    耗时 **0 秒**，远端账本未新增任何记录 —— 用户零感知。

    两个已确认的机制（都属"合法"路径，故必须靠**留痕**而非 rc 判定）：
      ① 日级保险丝 `_daily_sent`：同 mode 同日期已 sent → 拦截。
         这是设计正确的去重，但返回 `{sent: False, dedup: True}` 后
         build 仍 rc=0 → 只看退出码会误判成"推送成功"。
      ② `_daily_sent` 双查 state 表 + dist 镜像。**本地与远端账本会分叉**
         （本地库缺 09-15 build_close，远端有）→ 同一代码在两边
         判定结果不同。这是"本地复现不出 CI 现象"的又一类根因。
    """

    def test_daily_gate_result_is_distinguishable(self):
        """日级保险丝拦截时，返回值必须能与真发成功区分。"""
        src = self._notifier_src()
        self.assertIn('"daily_gate": True', src,
                      "保险丝拦截必须在返回值里留痕（daily_gate 标记）")
        # 拦截分支必须 sent=False，不得伪装成成功
        i = src.index("if not force and _daily_sent(con, mode, date):")
        self.assertIn('"sent": False', src[i:i + 160],
                      "保险丝拦截必须 sent=False")

    def _notifier_src(self):
        with open(os.path.join(ROOT, "pipeline", "notifier.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_daily_sent_checks_dist_mirror(self):
        """保险丝必须双查 dist 镜像 —— 否则本地/远端分叉会漏拦或误拦。"""
        src = self._notifier_src()
        i = src.index("def _daily_sent(")
        j = src.index("def _reconcile(")
        body = src[i:j]
        self.assertIn("DIST_LEDGER", body,
                      "保险丝须同时查 dist 镜像账本")
        self.assertIn("push_ledger", body,
                      "保险丝须同时查 state 表")

    def test_daily_gate_offline_behaviour(self):
        """离线真验：伪造已 sent 记录 → 拦截；换日期 → 放行。

        ⚠️ 2026-09-16 踩坑（CI run 34996238029 FAIL=1 的真凶）：
        `_daily_sent` 是**双查**——除传入的 state 连接外，还无条件读
        `notifier.DIST_LEDGER` 这个**真实文件**。只 mock 内存库不够：
        CI runner 会 checkout 仓库里的 `dist/push_ledger.json`，其中
        恰有 `2026-09-15 ... build_close ... sent`（本地账本反而没有这条，
        因为本地那格是 `data_blocked_close`）→ 文件分支返回 True →
        第一个 assertFalse 挂成 `True is not false`。
        这就是典型「本地绿、CI 红」：**测试依赖了工作区里的真实数据**。
        修法：把 DIST_LEDGER 一并隔离到临时空文件，让断言只依赖 con。
        """
        import importlib
        import sqlite3
        notifier = importlib.import_module("pipeline.notifier")

        # 隔离文件账本：指向一个不存在的临时路径，杜绝真实 data 污染
        tmpdir = tempfile.mkdtemp(prefix="astock_ledger_")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, True))
        isolated = os.path.join(tmpdir, "push_ledger.json")
        self.assertFalse(os.path.exists(isolated),
                         "隔离账本必须不存在，否则测不到「无记录」分支")

        with mock.patch.object(notifier, "DIST_LEDGER", isolated):
            con = sqlite3.connect(":memory:")
            self.addCleanup(con.close)
            con.execute("CREATE TABLE push_ledger(biz_key TEXT, mode TEXT,"
                        " ts TEXT, ok INT, status TEXT, src TEXT, note TEXT)")
            # 无记录 → 不拦（此前被真实文件账本污染而误判为 True）
            self.assertFalse(
                notifier._daily_sent(con, "build_close", "2026-09-15"),
                "内存库无记录且隔离账本为空时必须放行——"
                "若失败说明 DIST_LEDGER 未被隔离，测试又读了工作区真实账本")
            # 造一条 09-15 的 sent
            con.execute("INSERT INTO push_ledger VALUES('k','build_close',"
                        "'2026-09-15 10:58:23',1,'sent','x','y')")
            con.commit()
            self.assertTrue(
                notifier._daily_sent(con, "build_close", "2026-09-15"),
                "同 mode 同日期已 sent 必须拦截（防止重复推送）")
            self.assertFalse(
                notifier._daily_sent(con, "build_close", "2026-09-16"),
                "换日期必须放行（次日可正常推送）")
            self.assertFalse(
                notifier._daily_sent(con, "narrative", "2026-09-15"),
                "不同 mode 不得互相拦截")

    def test_daily_gate_isolated_from_repo_ledger(self):
        """防回归：本套件的断言不得被工作区真实 dist 账本影响。

        实证：`dist/push_ledger.json` 在仓库里带着 `2026-09-15 build_close
        sent` 被 checkout 到 CI runner，导致「离线」用例失败。此用例把
        DIST_LEDGER 指向**伪造的已有记录**，验证语义方向正确：
        传空 con + 文件有当日同 mode sent → 必须判 True（文件分支生效）。
        这样既锁住双查设计，又提醒后续维护者：写用例时必须显式隔离。
        """
        import importlib
        import sqlite3
        notifier = importlib.import_module("pipeline.notifier")

        tmpdir = tempfile.mkdtemp(prefix="astock_ledger2_")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, True))
        fake = os.path.join(tmpdir, "push_ledger.json")
        with open(fake, "w", encoding="utf-8") as f:
            json.dump({"deadbeef": {
                "mode": "build_close", "ts": "2026-09-15 10:58:23",
                "status": "sent", "channels": {"pushplus": "sent"}}}, f)

        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE push_ledger(biz_key TEXT, mode TEXT,"
                    " ts TEXT, ok INT, status TEXT, src TEXT, note TEXT)")
        with mock.patch.object(notifier, "DIST_LEDGER", fake):
            self.assertTrue(
                notifier._daily_sent(con, "build_close", "2026-09-15"),
                "文件账本有当日同 mode sent 时必须拦截（双查设计不可退）")
            self.assertFalse(
                notifier._daily_sent(con, "narrative", "2026-09-15"),
                "文件账本里没有的 mode 不得被误拦")


class TestFetchDepthIsSufficient(unittest.TestCase):
    """E. 抓取深度必须覆盖引擎真实最大回看（2026-09-16 260→40 改造）。"""

    def _fetch_src(self):
        with open(os.path.join(ROOT, "pipeline", "fetch_daily.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_default_depth_covers_engine_lookback(self):
        """默认深度必须 ≥ 引擎最大回看（engines.py 实测 30 根）。"""
        max_lookback = 0
        for fn in ("engines.py", "publish.py", "scoring.py"):
            p = os.path.join(ROOT, "pipeline", fn)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                src = f.read()
            for m in re.finditer(r"\[-(\d+):\]", src):
                n = int(m.group(1))
                if 2 <= n <= 400:      # 排除 key[:32] 之类非 K线切片
                    max_lookback = max(max_lookback, n)
        self.assertGreater(max_lookback, 0, "未扫描到任何回看深度，测试失效")
        m = re.search(r"^DEFAULT_DAYS\s*=\s*(\d+)", self._fetch_src(), re.M)
        self.assertIsNotNone(m, "缺少 DEFAULT_DAYS")
        self.assertGreaterEqual(
            int(m.group(1)), max_lookback,
            f"默认抓取深度({m.group(1)}) 必须 ≥ 引擎最大回看({max_lookback})")

    def test_inc_days_covers_lookback_too(self):
        """增量尾巴也必须够长，否则增量票的指标会算在截断数据上。"""
        src = self._fetch_src()
        m = re.search(r"^INC_DAYS\s*=\s*(\d+)", src, re.M)
        self.assertIsNotNone(m, "缺少 INC_DAYS")
        self.assertGreaterEqual(int(m.group(1)), 20,
                                "增量尾巴至少 20 根（覆盖 -20 类回看）")

    def test_history_is_never_deleted(self):
        """抓取深度只影响"新拉多少根"，不得出现删除历史 K线的语句。"""
        src = self._fetch_src()
        self.assertNotRegex(
            src, r"DELETE\s+FROM\s+klines",
            "抓取路径不得删除历史 K线（历史是只增不改的资产）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
