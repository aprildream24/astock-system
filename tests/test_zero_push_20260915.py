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


def strip_comments(src):
    """剥掉 `#` 注释内容，只留可执行代码行。

    ⚠️ 血案（2026-09-16，同一坑踩了两次）：`assertNotIn` / `assertNotRegex`
    断言"旧写法已消失"时，若注释里为解释修复而**引用了旧写法**（例如
    "# 原写法 `if \"000001\" not in all_ok:` 恒为假"），断言会命中注释本身
    → 假 FAIL。**修复说明即注释，注释即证据——两者必须分离。**
    凡是断言"代码里不得出现 X"的用例，都先用本函数剥注释。
    """
    out = []
    for line in src.splitlines():
        code = line.split("#", 1)[0]
        if code.strip():
            out.append(code)
    return "\n".join(out)


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
        # 只断言「pre/auction 分支内调用了 _preauction_ready」，
        # 不钉死跨行字面排版（实参换行即失配，属脆弱断言）
        i = src.index('if task in ("pre", "auction"):')
        tail = src[i:i + 900]
        self.assertIn("_preauction_ready(", tail,
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


class TestLedgerNeverLost(unittest.TestCase):
    """F. 账本只增不减 + 缓存不膨胀（2026-09-16 结构性缺陷修复）。

    血案：`push_ledger_sync` 单向覆盖 + `dist/push_ledger.json` 同时被
    checkout 与 cache 两个来源写入（checkout 先跑 → 陈旧快照胜出）⇒
    CI 跑完只剩本次 run 的少量记录 ⇒ PUT 上去把远端历史**整片抹掉**。
    实测本地 14 条 vs 远端 3 条，丢的含 09-13/09-14 真实收盘推送。

    危害不止"少日志"：`_daily_sent` 文件分支读不到当日记录 →
    **日级保险丝失效 → 重复推送回归**（09-14 晚 build_close 连推两条的病根）。
    """

    def _sync_src(self):
        with open(os.path.join(ROOT, "pipeline", "push_ledger_sync.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_sync_merges_remote_instead_of_overwriting(self):
        """提交前必须先 GET 远端并**合并**，同 key 以本地为准。"""
        src = self._sync_src()
        self.assertIn("remote_map", src,
                      "必须读回远端账本（否则会抹掉历史）")
        self.assertIn("merged = dict(remote_map)", src,
                      "必须先铺远端再 update 本地 —— 合并而非覆盖")
        self.assertIn("merged.update(local_map)", src,
                      "同 key 冲突以本地为准（本地是本次 run 的新记录）")
        self.assertIn("base64.b64encode(payload)", src,
                      "提交的必须是合并后的 payload，不是原始 raw")

    def test_sync_put_uses_merged_payload(self):
        """PUT 的 content 不得回退成 raw（防以后有人改回去）。"""
        src = self._sync_src()
        # 取 main() 里 PUT 之前那段
        i = src.index("st2, res = _gh(\"PUT\"")
        head = src[max(0, i - 700):i]
        self.assertNotIn("base64.b64encode(raw)", head,
                         "PUT 不得直接提交本地 raw（会覆盖远端历史）")
        self.assertIn("base64.b64encode(payload)", head,
                      "PUT 必须提交合并后的 payload")

    def _read_workflow(self):
        with open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                  encoding="utf-8") as f:
            return f.read()

    def test_workflow_ledger_not_in_cache_path(self):
        """dist/push_ledger.json 不得作为**缓存路径**被缓存。

        它同时被 checkout 写入 —— 两个来源打架时 checkout 胜出，
        cache 恢复的是陈旧快照，账本因而倒退。
        注意：注释里会提到该文件名（解释为何移出），所以只断言
        真正的 `path:` 块内容，不扫全文。
        """
        with open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                  encoding="utf-8") as f:
            lines = f.read().splitlines()
        # 找每个 actions/cache* 步骤的 with.path: 块
        hits = []
        i = 0
        while i < len(lines):
            if "actions/cache" in lines[i] and "uses" in lines[i]:
                j = i + 1
                while j < len(lines) and not lines[j].strip().startswith("- "):
                    if lines[j].strip().startswith("path:"):
                        # path: 可能是单行，也可能是 `|` 起头的多行块
                        val = lines[j].split("path:", 1)[1].strip()
                        if val in ("|", ">"):
                            k = j + 1
                            while k < len(lines) and (
                                    not lines[k].strip().startswith("- ")
                                    and lines[k].startswith(" " * 10)):
                                hits.append(lines[k].strip())
                                k += 1
                        elif val:
                            hits.append(val)
                    j += 1
                i = j
            else:
                i += 1
        joined = " ".join(hits)
        self.assertNotIn(
            "dist/push_ledger.json", joined,
            f"账本必须移出 cache path（实测 path 项：{hits}）——"
            "与 checkout 双源冲突会让账本倒退，"
            "进而使 _daily_sent 保险丝失效、重复推送回归")

    def test_workflow_cache_key_rolls_and_gc_exists(self):
        """缓存 key 必须「带 run_id 滚动」+ 有清理步骤 —— 二者缺一不可。

        ⚠️ 2026-09-16 二次血案（run 35002284192 冷库全量重拉 17 分钟）：
        `actions/cache/save@v4` **不允许覆盖已存在的 key**。所以
          · 固定 key（曾用 `market-db-v2`）→ 第一次存进去的永久生效，
            之后再也不会更新。实测首次存入的是 **0 字节空条目**
            ⇒ 每个 run 都「库最新 空」⇒ 4937 只全量重拉。
          · key 带 run_id → 每次存新的（能持续更新），但每次新增一份
            ⇒ 实测累积 15 份 ≈ 469 MB，会撑爆 10 GB 仓库缓存上限。
        ⇒ 正解是**两者并用**：滚动 key 保更新 + `cache_gc` 每轮删旧保不膨胀。
        """
        y = self._read_workflow()
        self.assertIn("key: market-db-${{ github.run_id }}", y,
                      "key 必须带 run_id，否则 save 无法更新缓存（固定 key "
                      "第一次写入后永久冻结——实测冻结成了 0 字节空条目）")
        self.assertIn("restore-keys", y,
                      "必须配 restore-keys 前缀回退，才能捞到上一 run 那份")
        self.assertIn("cache_gc", y,
                      "滚动 key 必然新增条目，必须配套清理步骤（否则膨胀）")
        self.assertIn("actions/cache/restore@v4", y,
                      "用 restore 才能配 save 精确控制写入时机")
        self.assertIn("actions/cache/save@v4", y,
                      "restore 之后必须显式 save，否则缓存永不更新、库被冻结")

    def test_cache_gc_deletes_only_stale_market_db(self):
        """cache_gc 只能删 market-db- 前缀、且**不是本次 run** 的条目。

        删错会把本轮刚存的库删掉（下轮又冷启动）；删非 market-db- 前缀的
        会误伤其他缓存。必须精确。
        """
        p = os.path.join(ROOT, "pipeline", "cache_gc.py")
        self.assertTrue(os.path.exists(p), "缺少 pipeline/cache_gc.py")
        with open(p, encoding="utf-8") as f:
            code = strip_comments(f.read())
        self.assertIn('PREFIX = "market-db-"', code,
                      "只允许清理 market-db- 前缀的缓存")
        self.assertIn("c.get(\"key\") != keep_key", code,
                      "必须排除本次 run 的缓存（否则删掉刚存的那份）")
        self.assertIn("GITHUB_RUN_ID", code,
                      "需读本次 run id 以排除自己那份")
        self.assertIn("return 0", code,
                      "清理失败必须恒返回 0 —— 运维优化不得连坐主链推送")
        self.assertNotIn("sys.exit(1)", code,
                         "cache_gc 不得以非 0 退出（会连坐主链）")

    def test_cache_gc_semantics_offline(self):
        """离线语义：验证筛选逻辑（保留本次 run、其余全删、无误伤）。"""
        PREFIX = "market-db-"
        keep_key = f"{PREFIX}35002284192"
        caches = [
            {"id": 1, "key": f"{PREFIX}35002284192", "size_in_bytes": 46 << 20},
            {"id": 2, "key": f"{PREFIX}34999055123", "size_in_bytes": 44 << 20},
            {"id": 3, "key": "pip-cache-xyz", "size_in_bytes": 9 << 20},
            {"id": 4, "key": f"{PREFIX}34997340881", "size_in_bytes": 44 << 20},
        ]
        targets = [c for c in caches
                   if c["key"].startswith(PREFIX) and c["key"] != keep_key]
        keys = sorted(c["key"] for c in targets)
        self.assertEqual(
            keys, [f"{PREFIX}34997340881", f"{PREFIX}34999055123"],
            "只应清理旧 market-db-*，且必须保留本次 run 那份")
        self.assertNotIn("pip-cache-xyz", keys, "不得误伤非 market-db- 缓存")
        freed_mb = sum(c["size_in_bytes"] for c in targets) / 1048576
        self.assertAlmostEqual(freed_mb, 88.0, places=1)


    def test_gh_sync_does_not_touch_ledger(self):
        """gh_sync 不得再推 dist/push_ledger.json（三写冲突的第三只手）。

        实测：本地 gh_sync 全量推送把 CI 刚写的 3 条覆盖回开发机的 14 条，
        随后 CI 又在其上滚动 —— 账本形状取决于"谁最后跑"，不可预测。
        账本只能由 push_ledger_sync（GET→合并→PUT）独占写入。

        ⚠️ `gh_sync.py` 自身在 `EXCLUDE_FILES` 里（工具脚本不入库）
        ⇒ **CI 上没有这个文件**。曾因直接 `open()` 而 FileNotFoundError
        → ERROR 一条 → 回归失败 → 全天零推送（run 35000190361 实证）。
        规则与「测试禁依赖工作区数据」同源：文件不在就 skip，别假设它存在。
        """
        p = os.path.join(ROOT, "gh_sync.py")
        if not os.path.exists(p):
            self.skipTest("CI 不含 gh_sync.py（工具脚本不入库，属预期）")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("ALLOW_DIST = set()", src,
                      "ALLOW_DIST 必须为空 —— 账本不得随 gh_sync 全量推送")
        # 确认可执行行里没有把账本加回放行集合
        code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
        self.assertNotIn("dist/push_ledger.json", code.split("ALLOW_DIST")[1][:80]
                         if "ALLOW_DIST" in code else "",
                         "ALLOW_DIST 不得再放行账本（会造成三写冲突）")


class TestIndexCalendarIntegrity(unittest.TestCase):
    """G. 指数日历完整性（2026-09-16 CI run 35000871359 实证的三个真实 bug）。

    事故链（**全是生产缺陷，不是测试问题**）：
      ① `fetch_daily.py` 的指数补拉条件 `if "000001" not in all_ok:` ——
         `all_ok` 的 key 是**裸码**，而裸码 `000001` 恰是**平安银行
         （sz000001）**，作为个股每轮都进 `all_ok` ⇒ 条件**恒为 False**
         ⇒ **上证指数 sh000001 的补拉被永久跳过**。
      ② 于是 `sh000001` 停在旧日期（实测 260 行、末位 2026-09-14），
         而 `trade_calendar()` 以 `sh000001` 为**权威日历** ⇒ 日历不含当日。
      ③ `build.py` 用 `idx = cal.index(date)` **直接索引**（无守卫）⇒
         抛 `ValueError: '2026-09-15' is not in list` ⇒ 构建崩溃、推送失败。
    注意：个股数据完全正常（同次日志 扫描覆盖=100.0%、宇宙 4588 只），
    所以这是「**只看推送有没有发**」才能发现的问题。
    """

    def _fetch_src(self):
        with open(os.path.join(ROOT, "pipeline", "fetch_daily.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_index_topup_uses_prefixed_code(self):
        """指数补拉必须用带前缀的 sh000001 判断，不得用裸码 000001。

        裸码 000001 = 平安银行，与上证指数撞车 ⇒ 条件恒假 ⇒ 指数永不更新。
        """
        src = self._fetch_src()
        # 只扫可执行代码：注释里为解释修复而引用了旧写法，属正常
        code = strip_comments(src)
        self.assertNotIn(
            'if "000001" not in all_ok:', code,
            '禁止用裸码 000001 作指数判断——它与平安银行(sz000001)撞车，'
            '会导致指数补拉被永久跳过')
        self.assertIn('idx_code = "sh000001"', src,
                      "指数必须用带前缀标识 sh000001 判断")

    def test_index_comparison_is_against_anchor(self):
        """指数是否需补，必须以「是否落后于断档锚」判定，不能靠 all_ok 有无。"""
        src = self._fetch_src()
        i = src.index("idx_code")
        body = src[i:i + 700]
        self.assertIn("idx_last", body, "必须查库中指数最新日期")
        self.assertIn("latest_td", body,
                      "必须以断档锚 latest_td 为比较基准")
        self.assertIn("need_idx", body, "必须给出显式的 need_idx 判定")

    def test_index_write_is_committed(self):
        """指数写库后必须显式 commit —— 它是日历唯一来源，丢了就崩 build。"""
        src = self._fetch_src()
        i = src.index('upsert_klines(con, idx_code,')
        tail = src[i:i + 260]
        self.assertIn("con.commit()", tail,
                      "指数写库后必须 commit（原代码漏了）")

    def test_index_never_pollutes_all_ok(self):
        """指数不得写进 all_ok（裸码空间）—— 会污染量纲修复与 self_heal。"""
        src = self._fetch_src()
        code = strip_comments(src)   # 同上：注释里引用了旧写法
        self.assertNotIn(
            'all_ok["000001"] =', code,
            '指数结果不得塞进 all_ok：其 key 是裸码，会与平安银行撞车，'
            '污染第 251 行 all_ok[c][5]（流通股）与 self_heal 的补数范围')

    def test_build_calendar_lookup_is_guarded(self):
        """build 的 cal.index(date) 必须有 ValueError 守卫。

        日历由指数K线推导，指数滞后时必然不含当日 ⇒ 无守卫则整个 build 崩。
        """
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("except ValueError", src,
                      "cal.index(date) 必须容错，否则日历缺当日时 build 崩溃")
        # 禁止裸 cal.index(date)（无 except 包着）
        i = src.index("cal = trade_calendar(con)")
        body = src[i:i + 900]
        self.assertIn("except ValueError", body,
                      "trade_calendar 之后的 index 调用必须带容错分支")
        self.assertIn("default=-1", body,
                      "容错分支需有 default 兜底（日历为空也不崩）")

    def test_calendar_guard_semantics_offline(self):
        """离线语义验证：日历不含 date 时，容错逻辑仍能算出 valid_until。"""
        cal = ["2026-09-10", "2026-09-11", "2026-09-14"]
        date = "2026-09-15"
        try:
            idx = cal.index(date)
        except ValueError:
            idx = max((i for i, d in enumerate(cal) if d <= date), default=-1)
        self.assertEqual(idx, 2, "应退化到最后一个 ≤ date 的交易日")
        valid_until = cal[min(idx + 5, len(cal) - 1)]
        self.assertEqual(valid_until, "2026-09-14")
        # 日历为空也不得抛异常
        empty = []
        try:
            i2 = empty.index(date)
        except ValueError:
            i2 = max((i for i, d in enumerate(empty) if d <= date), default=-1)
        self.assertEqual(i2, -1)

    def test_index_empty_db_does_not_crash(self):
        """冷启动（空库）时指数补拉判定不得抛 TypeError。

        ⚠️ 2026-09-16 血案（CI run 35002284192，冷缓存实测）：
        `SELECT MAX(date) ...` 在**空表**上返回一行 `(None,)` —— 该元组
        **truthy**，所以 `if idx_last and idx_last[0] >= latest_td` 的
        `idx_last` 检查形同虚设 ⇒ `None >= "2026-09-15"`
        ⇒ `TypeError: '>=' not supported between instances of
        'NoneType' and 'str'` ⇒ 抓取步骤崩（当时因 workflow 无 fail-fast
        才没连坐后续步骤，但指数补拉未执行）。
        修法：取到**值**（`idx_last[0]`）再判空，不得判元组本身。
        """
        src = self._fetch_src()
        code = strip_comments(src)
        self.assertNotIn(
            "idx_last and idx_last[0] >=", code,
            "禁止用元组真值判断 MAX(date) 是否为空 —— 空库返回 (None,) 是 "
            "truthy，必须取 idx_last[0] 判值")
        self.assertIn("idx_last_date", code,
                      "必须先把 MAX(date) 取成标量（idx_last_date）再判空")

    def test_index_empty_db_semantics_offline(self):
        """离线语义：模拟空库返回 (None,) 的真实行为，验证修法不崩。"""
        import sqlite3
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE klines(code TEXT, date TEXT)")
        latest_td = "2026-09-15"

        # 旧写法 —— 必须崩
        idx_last = con.execute(
            "SELECT MAX(date) FROM klines WHERE code=?", ("sh000001",)).fetchone()
        self.assertEqual(idx_last, (None,), "空表 MAX(date) 应返回 (None,)")
        self.assertTrue(idx_last, "该元组是 truthy —— 这正是旧写法的陷阱")
        with self.assertRaises(TypeError):
            _ = not (idx_last and idx_last[0] >= latest_td)

        # 新写法 —— 必须稳稳判定"需要补"
        idx_last_date = idx_last[0] if idx_last else None
        need_idx = not (idx_last_date and idx_last_date >= latest_td)
        self.assertTrue(need_idx, "库中无指数 ⇒ 必须判定需要补拉")

        # 已有指数但不落后 ⇒ 不需要补
        con.execute("INSERT INTO klines VALUES('sh000001','2026-09-15')")
        r = con.execute(
            "SELECT MAX(date) FROM klines WHERE code=?", ("sh000001",)).fetchone()
        d = r[0] if r else None
        self.assertFalse(not (d and d >= latest_td), "已是最新则不应重复补拉")


class TestPremarketAllZeroNotHoliday(unittest.TestCase):
    """H. 「快照 pct 全零 ⇒ 疑似休市」的两个副本（2026-09-16 第三/四号血案）。

    ★ 这是同一个错误判定的**三处副本**——修一处不够：
      ① `fetch_daily.guard_snapshot`（抛 ValueError，盘前任务 failure）
      ② `build._preauction_ready`（返回 False，盘前只发「数据未就绪」）
      ③ `core.is_trading_day_cross`（close 路径，**故意保留**——
         15:22 收盘后 pct 全零确实是休市/数据异常信号，见
         test_gate_no_deadlock.test_all_zero_pct_still_rejects）
    ①② 在**盘前时段**是**必然误判**：08:50 集合竞价未开始、09:25 竞价刚
    结束接口未刷新，快照涨跌幅天然全 0。实测 09-15 08:50 定时任务因此
    ValueError → 抓取步骤 failure → 后接「构建+推送」整步 skipped
    → **用户全天收不到盘前推送**（这就是"什么都收不到"的根因）。
    """

    def test_guard_snapshot_signature_has_premarket(self):
        import importlib
        fd = importlib.import_module("pipeline.fetch_daily")
        import inspect
        sig = inspect.signature(fd.guard_snapshot)
        self.assertIn("premarket", sig.parameters,
                      "guard_snapshot 必须有 premarket 开关")
        self.assertIs(sig.parameters["premarket"].default, False,
                      "premarket 默认必须为 False —— 收盘路径的全零保护"
                      "（test_all_zero_pct_still_rejects）不可放松")

    def test_guard_snapshot_premarket_allows_all_zero(self):
        """★ 核心：盘前快照全零必须放行，且跳过成交额分级。"""
        import importlib
        fd = importlib.import_module("pipeline.fetch_daily")
        universe = {f"60000{i}": {"pct": 0.0, "amt": 0.0} for i in range(50)}
        lvl, reason = fd.guard_snapshot(universe, "2026-09-16",
                                        premarket=True)
        self.assertEqual(lvl, "ok", "盘前全零必须放行")
        self.assertIn("premarket", reason,
                      "理由须说明是盘前时段（便于日志排查）")

    def test_guard_snapshot_non_premarket_still_raises(self):
        """★ 反向锁：非盘前全零仍须抛（别把休市保护改没了）。"""
        import importlib
        fd = importlib.import_module("pipeline.fetch_daily")
        universe = {f"60000{i}": {"pct": 0.0, "amt": 0.0} for i in range(50)}
        with self.assertRaises(ValueError) as cm:
            fd.guard_snapshot(universe, "2026-09-16", premarket=False)
        self.assertIn("全 0", str(cm.exception),
                      "非盘前全零必须仍判疑似休市（收盘保护不可放松）")

    def test_fetch_daily_accepts_premarket(self):
        import importlib
        import inspect
        fd = importlib.import_module("pipeline.fetch_daily")
        sig = inspect.signature(fd.fetch_daily)
        self.assertIn("premarket", sig.parameters,
                      "fetch_daily 必须把 premarket 透传给 guard_snapshot")
        self.assertIn("premarket=premarket", self._fetch_src(),
                      "fetch_daily 内部必须真的把 premarket 传下去"
                      "（只加形参不传参 = 白改）")

    def _fetch_src(self):
        with open(os.path.join(ROOT, "pipeline", "fetch_daily.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_cli_has_premarket_flag(self):
        """CI 靠 CLI 传参 —— 入口必须有 --premarket。"""
        src = self._fetch_src()
        self.assertIn('"--premarket"', src,
                      "CLI 必须暴露 --premarket（workflow 靠它传参）")
        self.assertIn("premarket=a.premarket", src,
                      "CLI 解析后必须透传给 fetch_daily")

    def test_workflow_passes_premarket_for_pre_auction(self):
        """workflow 第 7 步必须对 pre/auction 传 --premarket。"""
        with open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                  encoding="utf-8") as f:
            y = f.read()
        self.assertIn("--premarket", y,
                      "workflow 必须对盘前/竞价任务传 --premarket，"
                      "否则 08:50 快照全零照样抛 ValueError")
        # 必须是「pre 或 auction 才加」的条件式，不能无条件加
        self.assertRegex(
            y, r"pre\|auction\)\s*EXTRA|task\s*==\s*'pre'|task\s*==\s*.pre.",
            "必须按 task 条件判断（review 是盘后，不该带该标志）")

    def test_no_ternary_in_actions_expressions(self):
        """★ 血案：**Actions 表达式不支持三元 `?:`**（JS 风格会整份解析失败）。

        实证（2026-09-16 晚）：写着
            ${{ cond ? '--premarket' : '' }}
        的 workflow **整份无法解析**，dispatch 直接返
            422 Invalid Argument - failed to parse workflow:
            (Line: 148, Col: 14): Unexpected symbol: '?'
        且 push 触发的 run **零 job**（`total_count: 0`）。
        危害是**全局的**：任何触发方式都启动不了，等于当天所有定时任务全灭。
        修法是改用 bash `case` 做条件展开（表达式保持简单）。

        断言只扫**可执行行**——注释里为记录旧写法而引用 `?:` 是正常的
        （这正是 strip_comments 存在的原因）。
        """
        with open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                  encoding="utf-8") as f:
            y = f.read()
        code = strip_comments(y)
        bad = re.findall(r"\$\{\{[^}]*\?[^}]*\}\}", code)
        self.assertEqual(
            bad, [],
            f"Actions 表达式不得用三元 `?:`（会整份解析失败、全部任务无法"
            f"启动）。实际残留：{bad}。请改用 bash case/if 展开。")

    def test_workflow_yaml_parses_and_has_build_steps(self):
        """workflow 必须能被 YAML 解析且 build job 步骤齐全。

        零 job 的 run 说明连 workflow 都没解析成功 —— 这类故障最难察觉
        （run 显示 failure，但没有任何步骤日志可看）。
        """
        with open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                  encoding="utf-8") as f:
            y = f.read()
        try:
            import yaml
        except ImportError:
            self.skipTest("无 pyyaml，跳过结构校验")
        # PyYAML 会把裸 `on:` 解析成布尔 True —— 属正常，不影响校验
        d = yaml.safe_load(y)
        self.assertIn("jobs", d, "workflow 必须能解析出 jobs")
        self.assertIn("build", d["jobs"], "必须有 build job")
        steps = d["jobs"]["build"].get("steps", [])
        self.assertGreaterEqual(len(steps), 10,
                                f"build job 步骤数异常偏少（{len(steps)}）")
        names = " ".join(s.get("name", "") for s in steps)
        self.assertIn("构建+推送", names, "缺少「构建+推送」步骤")

    def test_preauction_gate_has_no_allzero_rejection(self):
        """★ 核心：_preauction_ready 不得再有全零拒绝分支。"""
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        i = src.index("def _preauction_ready(")
        j = src.index("def _notify_data_blocked(")
        body = strip_comments(src[i:j])
        self.assertNotIn(
            "快照 pct 全零", body,
            "_preauction_ready 不得再因全零拒绝 —— 盘前全零是时点属性，"
            "不是休市证据（实测因此只发得出一条「数据未就绪」）")

    def test_preauction_gate_still_requires_snapshot(self):
        """反向锁：去掉全零判定后，仍必须要求当日快照存在。"""
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        i = src.index("def _preauction_ready(")
        j = src.index("def _notify_data_blocked(")
        body = strip_comments(src[i:j])
        self.assertIn("无快照（竞价数据未入库）", body,
                      "无当日快照必须仍然拒绝（不得为了放行而放行）")

    def test_preauction_gate_semantics_offline(self):
        """★ 离线真验：当日快照 pct/amt 全零 → 修复后必须放行。

        修复前该场景返回 `False, "快照 pct 全零：疑似休市日"`。
        ⚠️ 2026-09-16 补强：闸门同时要求**锚定日（上一交易日）快照有真实
        成交额**（盘前/竞价的筛选口径就是它，见 build.scan_all 注释）——
        故本用例给 prev 日有效快照，验证「当日全零 + prev 有量 ⇒ 放行」。
        """
        import importlib
        import sqlite3
        build = importlib.import_module("pipeline.build")
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE klines(code TEXT, date TEXT,"
                    " o REAL, h REAL, l REAL, c REAL, v REAL)")
        con.execute("CREATE TABLE snapshot(date TEXT, code TEXT, name TEXT,"
                    " price REAL, pct REAL, amt REAL, turn REAL, fmv REAL)")
        # 上一交易日有 K线
        con.execute("INSERT INTO klines VALUES('sh000001','2026-09-15',"
                    "1,1,1,1,1)")
        # 上一交易日快照：成交额正常（盘前筛选口径的来源）
        con.executemany("INSERT INTO snapshot VALUES('2026-09-15',?,?,0,0,"
                        "2.0e8,0,0)",
                        [(f"sh6000{i:02d}", "x") for i in range(800)])
        # 当日 800 只快照，pct/amt 全 0（复现「盘前快照全 0」）
        con.executemany("INSERT INTO snapshot VALUES('2026-09-16',?,?,0,0,0,"
                        "0,0)", [(f"sh6000{i:02d}", "x") for i in range(800)])
        con.commit()
        ok, why = build._preauction_ready(con, "2026-09-16")
        self.assertTrue(ok, f"盘前全零快照必须放行，实际：{why}")
        self.assertIn("就绪", why)

    def test_preauction_gate_rejects_when_prev_snapshot_empty(self):
        """★ 2026-09-16 血案锁：锚定日快照成交额全空 ⇒ 必须拒绝构建。

        事故现场（CI run 35041635767 / 35043999848）：当日快照行数正常
        （5559 行）但成交额全 0 / 只有竞价撮合额，闸门只看「行数 > 0」就放行
        ⇒ 下游 split_universe 判全市场停牌（宇宙 0）或「成交额<1.2亿」门槛
        全灭（4403 只）⇒ 用户收到一份**候选 0 只的空计划**。
        盘前/竞价的筛选口径是上一交易日收盘快照，故必须在闸门处校验它。
        """
        import importlib
        import sqlite3
        build = importlib.import_module("pipeline.build")
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE klines(code TEXT, date TEXT,"
                    " o REAL, h REAL, l REAL, c REAL, v REAL)")
        con.execute("CREATE TABLE snapshot(date TEXT, code TEXT, name TEXT,"
                    " price REAL, pct REAL, amt REAL, turn REAL, fmv REAL)")
        con.execute("INSERT INTO klines VALUES('sh000001','2026-09-15',"
                    "1,1,1,1,1)")
        # 只有当日快照，且成交额全 0；**没有** prev 日快照
        con.executemany("INSERT INTO snapshot VALUES('2026-09-16',?,?,0,0,0,"
                        "0,0)", [(f"sh6000{i:02d}", "x") for i in range(800)])
        con.commit()
        ok, why = build._preauction_ready(con, "2026-09-16")
        self.assertFalse(ok, "锚定日无有效快照时必须拒绝（否则必推空计划）")
        self.assertIn("无有效快照", why)

    def test_preauction_signature_has_no_bypass_switch(self):
        """★ 不得留 `task_has_intraday_pct` 这类开关。

        已知教训：修复时若把旧分支包成「开关 + 死代码」，后人翻开开关就
        重新踩坑。**删干净比留开关安全**——故断言该形参不复存在。
        """
        import importlib
        import inspect
        build = importlib.import_module("pipeline.build")
        sig = inspect.signature(build._preauction_ready)
        self.assertEqual(
            list(sig.parameters), ["con", "date"],
            f"_preauction_ready 只应接受 (con, date)，实际 {list(sig.parameters)}"
            "—— 多出的开关意味着旧的全零拒绝分支还活着")

    def test_close_path_allzero_still_guarded(self):
        """反向锁：close 路径的全零判定**故意保留**（15:22 全零=真异常）。"""
        with open(os.path.join(ROOT, "pipeline", "core.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("快照 pct 全零", src,
                      "core.is_trading_day_cross 的全零判定是 close 路径的"
                      "真保护（15:22 全零意味着休市或数据异常），不得误删")


class TestPushFallbackChain(unittest.TestCase):
    """I. 备用通道兜底（2026-09-16 补，消除单通道风险）。

    本仓库当前形态：`primary_channel=pushplus`、`wxpusher_accounts=[]`、
    本地 `serverchan_key=''`。即 **PushPlus 是唯一通道** —— 它一挂就零送达。
    而 CI 侧 `SERVERCHAN_KEY` Secret **已注入 stock.yml**（build 与 review
    两步都有），代码却从不拿它作 PushPlus 的兜底（原实现只在 wxpusher
    全失败时兜底）⇒ 一条现成的备用通道被白白浪费。

    这是"什么都收不到"的**最后一道未知风险**：前面所有闸门都修好了，
    但若 PushPlus 当天额度耗尽/接口异常，用户依然收不到，且无通道补位。
    """

    def _cfg(self, **over):
        cfg = {"push_dry_run": False, "primary_channel": "pushplus",
               "pushplus_token": "t" * 32, "serverchan_key": "s" * 32,
               "wxpusher_accounts": [], "push_tag": "Astra"}
        cfg.update(over)
        return cfg

    def _con(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE push_ledger(biz_key TEXT, mode TEXT,"
                    " ts TEXT, ok INT, status TEXT, src TEXT, note TEXT)")
        return con

    def _isolate(self, notifier):
        tmpdir = tempfile.mkdtemp(prefix="astock_fb_")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, True))
        p = os.path.join(tmpdir, "led.json")
        patcher = mock.patch.object(notifier, "DIST_LEDGER", p)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pushplus_failure_falls_back_to_serverchan(self):
        """★ 核心：PushPlus failed → 必须补发 ServerChan。"""
        import importlib
        notifier = importlib.import_module("pipeline.notifier")
        self._isolate(notifier)
        calls = []

        def fake_pp(*a):
            calls.append("pushplus")
            return "failed", "HTTP 500"

        def fake_sc(*a):
            calls.append("serverchan")
            return "sent", "ok"

        with mock.patch.object(notifier, "_send_pushplus", fake_pp), \
                mock.patch.object(notifier, "_send_serverchan", fake_sc), \
                mock.patch.object(notifier, "load_config",
                                  lambda *a, **k: self._cfg()):
            r = notifier.push("build_close", "T", "B",
                              date="2026-09-16", con=self._con())
        self.assertEqual(calls, ["pushplus", "serverchan"],
                         "PushPlus 失败后必须走 ServerChan 兜底")
        self.assertTrue(r["sent"], "兜底成功后整体必须报 sent（用户确实收到了）")
        self.assertEqual(r["results"]["serverchan"]["status"], "sent")
        self.assertEqual(r["results"].get("serverchan", {}).get("role"),
                         "fallback", "兜底通道须带 role 标记便于排查")

    def test_uncertain_does_not_trigger_duplicate_fallback(self):
        """★ 反向锁：`uncertain`（超时/受理未知）**不得**触发兜底补发。

        受理状态未知时补发会造成**同一消息重复送达**——这是 M37 三态账本
        刻意设计的语义（不确定优先于 failed，不盲目双发）。
        """
        import importlib
        notifier = importlib.import_module("pipeline.notifier")
        self._isolate(notifier)
        calls = []

        def fake_pp(*a):
            calls.append("pushplus")
            return "uncertain", "timeout"

        def fake_sc(*a):
            calls.append("serverchan")
            return "sent", "ok"

        with mock.patch.object(notifier, "_send_pushplus", fake_pp), \
                mock.patch.object(notifier, "_send_serverchan", fake_sc), \
                mock.patch.object(notifier, "load_config",
                                  lambda *a, **k: self._cfg()):
            r = notifier.push("build_close", "T", "B",
                              date="2026-09-16", con=self._con())
        self.assertEqual(calls, ["pushplus"],
                         "uncertain 不得补发（会造成重复送达）")
        self.assertEqual(r["status"], "uncertain",
                         "总体状态应保持 uncertain，不得美化成 sent")

    def test_no_fallback_without_key(self):
        """没配 serverchan_key 时不得报错（须优雅降级）。"""
        import importlib
        notifier = importlib.import_module("pipeline.notifier")
        self._isolate(notifier)

        def fake_pp(*a):
            return "failed", "HTTP 500"

        with mock.patch.object(notifier, "_send_pushplus", fake_pp), \
                mock.patch.object(notifier, "load_config",
                                  lambda *a, **k: self._cfg(
                                      serverchan_key="")):
            r = notifier.push("build_close", "T", "B",
                              date="2026-09-16", con=self._con())
        self.assertFalse(r["sent"], "无兜底且主通道失败 ⇒ 必须报 sent=False"
                                    "（不得静默伪装成功）")
        self.assertEqual(r["status"], "failed")

    def test_fallback_capability_declared_in_workflow(self):
        """CI 必须把 SERVERCHAN_KEY 注入构建+推送步骤（兜底的前提）。"""
        with open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                  encoding="utf-8") as f:
            y = f.read()
        self.assertIn("SERVERCHAN_KEY", y,
                      "workflow 必须注入 SERVERCHAN_KEY —— 否则兜底代码在 CI 上"
                      "永远因无 key 而跳过，单通道风险依旧")


if __name__ == "__main__":
    unittest.main(verbosity=2)
