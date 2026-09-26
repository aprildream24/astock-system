# -*- coding: utf-8 -*-
"""live 高频买点巡检的回归锁（2026-09-26 新增）。

锁三组**不可回退**的不变量：

A. 事件级去重（本特性的灵魂）
   live 每 10 分钟一轮，去重单位必须是「事件 (date, kind, code)」而不是
   「轮次/天数」：同票同事件当天只报一次，新事件即刻 force 推送。
   账本 live_alerts 丢了这条，就是把推送量放大 20 倍或把新事件全部吞掉。

B. am/pm 语义不变
   早盘/尾盘摘要仍是「日熔丝一天一条」；live 的引入不得改变它们的
   推送纪律（存量行为回归锁）。

C. 模拟盘 REJECT 一次性
   高频轮询下「到价未成交」同票当天只报一次（orders 有全量留痕，
   exec_log 只放打扰标记），否则资金不足的票一天连报 20 遍。
"""
import datetime as _dt
import importlib
import os
import sys
import sqlite3
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATE = "2026-09-16"          # 真实交易日（周三）
BJT = _dt.timezone(_dt.timedelta(hours=8))


def _mkcon():
    core = importlib.import_module("pipeline.core")
    con = sqlite3.connect(":memory:")
    con.executescript(core._SCHEMA)      # 用**真** schema：顺带验证建表存在
    return con


def _plan(con, code, name, lo, hi, stop=None, action="现在买"):
    con.execute("INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (DATE, code, name, "趋势", action, lo, hi, stop, None, None,
                 80.0, "", None))
    con.commit()


def _snap(pairs, pad=True, pad_to=520):
    out = {c: {"name": "票" + c[-2:], "price": p, "pct": pc,
               "vol": 1e6, "amt": 3e8, "turn": 1.2, "fmv": 5e10}
           for c, p, pc in pairs}
    if pad:
        i = 0
        while len(out) < pad_to:
            out["%06d" % (900000 + i)] = {
                "name": "占位", "price": 10.0, "pct": 0.0, "vol": 1e5,
                "amt": 1e6, "turn": 0.5, "fmv": 1e9}
            i += 1
    return out


def _run(slot, snap, held=None, now=None, con=None, dry=False):
    """跑一次盘中校验：mock 网络与凭据，push 走真实逻辑（含账本/去重）。"""
    intra = importlib.import_module("pipeline.intraday")
    fetch_daily = importlib.import_module("pipeline.fetch_daily")
    notifier = importlib.import_module("pipeline.notifier")
    build = importlib.import_module("pipeline.build")
    con = con or _mkcon()
    tmp_ledger = os.path.join(tempfile.gettempdir(),
                              "_intraday_live_test_ledger.json")
    with mock.patch.object(fetch_daily, "fetch_universe",
                           lambda *a, **k: snap), \
            mock.patch.object(build, "load_holdings",
                              lambda: (held or [])), \
            mock.patch.object(notifier, "_send_pushplus",
                              lambda *a, **k: ("sent", "ok")), \
            mock.patch.object(notifier, "load_config",
                              lambda: {"primary_channel": "pushplus",
                                       "pushplus_token": "tok"}), \
            mock.patch.object(notifier, "DIST_LEDGER", tmp_ledger):
        res = intra.run(slot=slot, date=DATE, con=con, dry=dry,
                        now=now or _dt.datetime(2026, 9, 16, 10, 10,
                                                tzinfo=BJT))
    if os.path.exists(tmp_ledger):
        os.remove(tmp_ledger)
    return res, con, notifier


class _LedgerIsolated(unittest.TestCase):
    """与 test_intraday_scope 同款：账本隔离必须覆盖断言本身。"""

    @classmethod
    def setUpClass(cls):
        notifier = importlib.import_module("pipeline.notifier")
        cls._tmp_dist = os.path.join(
            tempfile.gettempdir(), "_intraday_live_dist_%s.json" % id(cls))
        if os.path.exists(cls._tmp_dist):
            os.remove(cls._tmp_dist)
        cls._patch = mock.patch.object(notifier, "DIST_LEDGER", cls._tmp_dist)
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()
        if os.path.exists(getattr(cls, "_tmp_dist", "")):
            os.remove(cls._tmp_dist)


class TestLiveWindow(_LedgerIsolated):
    """live 覆盖整个连续交易时段；午休/盘后必须被守门拦下。"""

    def setUp(self):
        self.intra = importlib.import_module("pipeline.intraday")

    def _w(self, h, m):
        return self.intra.in_window(
            "live", _dt.datetime(2026, 9, 16, h, m, tzinfo=BJT))

    def test_morning_session(self):
        self.assertTrue(self._w(9, 40))
        self.assertTrue(self._w(11, 30))
        self.assertFalse(self._w(9, 0), "集合竞价前不属于 live 窗口")
        self.assertFalse(self._w(11, 50), "午休前收尾超出 am 窗口缓冲")

    def test_afternoon_session(self):
        self.assertTrue(self._w(13, 10))
        self.assertTrue(self._w(15, 0))
        self.assertFalse(self._w(12, 0), "午休必须拦下")
        self.assertFalse(self._w(15, 30), "收盘后必须拦下")

    def test_am_pm_windows_unchanged(self):
        """存量窗口回归锁：am/pm 的边界不因 live 引入而漂移。"""
        f = self.intra.in_window
        self.assertTrue(f("am", _dt.datetime(2026, 9, 16, 9, 45, tzinfo=BJT)))
        self.assertFalse(f("am", _dt.datetime(2026, 9, 16, 13, 0, tzinfo=BJT)))
        self.assertTrue(f("pm", _dt.datetime(2026, 9, 16, 14, 40, tzinfo=BJT)))
        self.assertFalse(f("pm", _dt.datetime(2026, 9, 16, 10, 0, tzinfo=BJT)))


class TestEventDedup(_LedgerIsolated):
    """A. 事件级去重：同票同事件当天只报一次，新事件即刻再推。"""

    def test_first_zone_entry_pushes(self):
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        res, con, _ = _run("live", _snap([("600519", 20.5, 1.0)]), con=con)
        self.assertTrue(res["pushed"], f"live 首次进买区必须推，实际 {res}")
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM live_alerts WHERE kind='zone'"
                        ).fetchone()[0], 1)

    def test_same_stock_not_repeated_next_round(self):
        """仍停在买区里的票，下一轮不得再推（去重的灵魂）。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        snap = _snap([("600519", 20.5, 1.0)])
        r1, con, _ = _run("live", snap, con=con)
        self.assertTrue(r1["pushed"])
        r2, con, _ = _run("live", snap, con=con)   # 10 分钟后，价没动
        self.assertFalse(r2["pushed"], "同一事件第二轮必须静默")
        self.assertIn("无新事件", r2["reason"])
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM live_alerts").fetchone()[0], 1,
            "账本不得重复记账")

    def test_new_stock_still_pushes_same_day(self):
        """★ 与日熔丝的本质区别：同一天后来的新票必须能推出去。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        _plan(con, "sz000001", "平安银行", 9.0, 9.5)
        r1, con, _ = _run("live", _snap([("600519", 20.5, 1.0)]), con=con)
        self.assertTrue(r1["pushed"])
        # 10 分钟后平安银行也进买区（茅台仍在区内）
        r2, con, _ = _run("live",
                          _snap([("600519", 20.5, 1.0), ("000001", 9.2, 0.3)]),
                          con=con)
        self.assertTrue(r2["pushed"], "新事件必须立刻推（不能被日熔丝吞掉）")
        kinds = {r[0] for r in con.execute(
            "SELECT code FROM live_alerts WHERE kind='zone'").fetchall()}
        self.assertEqual(kinds, {"sh600519", "sz000001"})

    def test_force_push_used_for_live(self):
        """live 必须 force 推送：绕过「每 mode 一天一条」的日级保险丝，
        否则当天第二个事件永远发不出（事件去重已在账本层完成）。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        _run("live", _snap([("600519", 20.5, 1.0)]), con=con)
        _plan(con, "sz000001", "平安银行", 9.0, 9.5)
        r2, _, notifier = _run("live", _snap([("600519", 20.5, 1.0),
                                              ("000001", 9.2, 0.3)]), con=con)
        self.assertTrue(r2["pushed"])
        self.assertTrue(notifier._daily_sent(con, "intraday_live", DATE),
                        "日熔丝已命中仍推出 = force 生效的证据")

    def test_hold_stop_reported_once_per_day(self):
        """持仓触止损：当天只报一次（事故级信号也不该每 10 分钟轰炸）。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)   # 涨飞，不占组
        held = [{"code": "sz000001", "name": "平安银行", "stop": 9.5}]
        snap = _snap([("600519", 25.0, 6.0), ("000001", 9.1, -4.0)])
        r1, con, _ = _run("live", snap, held=held, con=con)
        self.assertTrue(r1["pushed"])
        r2, con, _ = _run("live", snap, held=held, con=con)
        self.assertFalse(r2["pushed"], "同一持仓止损当天不得重复报")
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM live_alerts WHERE kind='stop'"
                        ).fetchone()[0], 1)

    def test_dry_run_never_marks_ledger(self):
        """dry 只算不推也不记账——否则验收跑一遍会把真实事件标记为已报。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        res, con, _ = _run("live", _snap([("600519", 20.5, 1.0)]),
                           con=con, dry=True)
        self.assertIn("dry-run", res["reason"])
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM live_alerts").fetchone()[0], 0)


class TestLegacySlotsUnchanged(_LedgerIsolated):
    """B. am/pm 摘要仍是「日熔丝一天一条」，不受 live 影响。"""

    def test_pm_second_run_hits_daily_gate(self):
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        snap = _snap([("600519", 20.5, 1.0)])
        r1, con, _ = _run("pm", snap, con=con,
                          now=_dt.datetime(2026, 9, 16, 14, 40, tzinfo=BJT))
        self.assertTrue(r1["pushed"])
        r2, _, _ = _run("pm", snap, con=con,
                        now=_dt.datetime(2026, 9, 16, 14, 40, tzinfo=BJT))
        self.assertFalse(r2["pushed"])
        self.assertTrue(r2["push"].get("daily_gate") or r2["push"].get("dedup"),
                        f"pm 第二次必须被日熔丝拦截，实际 {r2}")

    def test_zone_marked_by_pm_not_repushed_by_live(self):
        """早盘/尾盘摘要里报过的买区票，live 不再对同一只重复开火。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        snap = _snap([("600519", 20.5, 1.0)])
        r1, con, _ = _run("pm", snap, con=con,
                          now=_dt.datetime(2026, 9, 16, 14, 40, tzinfo=BJT))
        self.assertTrue(r1["pushed"])
        r2, _, _ = _run("live", snap, con=con,
                        now=_dt.datetime(2026, 9, 16, 14, 50, tzinfo=BJT))
        self.assertFalse(r2["pushed"], "pm 已报过的票 live 不得再报")


class TestRejectOnce(_LedgerIsolated):
    """C. 模拟盘「到价未成交」当天一次性。"""

    def test_same_day_reject_only_once(self):
        ex = importlib.import_module("pipeline.executor")
        con = _mkcon()
        today = DATE
        with mock.patch.object(ex, "_now", lambda: today + " 10:00:00"):
            a = ex._reject_once(con, "sh600519", today, "资金不足")
            b = ex._reject_once(con, "sh600519", today, "资金不足")
        self.assertEqual(a[1], "REJECT")
        self.assertEqual(b[1], "SKIP", "同票同天第二次拒绝必须降级为 SKIP")

    def test_reject_unblocked_next_day(self):
        ex = importlib.import_module("pipeline.executor")
        con = _mkcon()
        with mock.patch.object(ex, "_now",
                               lambda: DATE + " 10:00:00"):
            ex._reject_once(con, "sh600519", DATE, "资金不足")
        with mock.patch.object(ex, "_now",
                               lambda: "2026-09-17 10:00:00"):
            a = ex._reject_once(con, "sh600519", "2026-09-17", "资金不足")
        self.assertEqual(a[1], "REJECT", "跨日必须重新可报")


class TestTimerLive(_LedgerIsolated):
    """定时器安装脚本：调度口径反推、报文改写、守门登记。"""

    def setUp(self):
        self.tl = importlib.import_module("tools.timer_live")
        self.tg = importlib.import_module("pipeline.timer_guard")

    def test_required_excludes_live_until_key_valid(self):
        """cron-job Secret key 无效（GET /jobs 404）期间 live 定时器建不出；
        REQUIRED 若含它会让守门每个 audit 时点都误报。盘中高频巡检由
        intraday-live.yml 长驻循环顶班。用户补 key 建成定时器后，
        把 "astock-intraday-live" 加回 REQUIRED（tools/timer_live.py 已备好）。"""
        self.assertNotIn("astock-intraday-live", self.tg.REQUIRED)
        # 创建脚本必须常备：key 一到即可建成（克隆 am、每 10 分钟、slot=live）
        self.assertEqual(self.tl.LIVE_TITLE, "astock-intraday-live")
        self.assertEqual(self.tl.CLONE_FROM, "astock-intraday-am")

    def test_live_loop_module_wired(self):
        """长驻循环模块存在且触发间隔/收盘退出常量正确（防误改）。"""
        ll = importlib.import_module("tools.live_loop")
        self.assertEqual(ll.STEP, 10)
        self.assertEqual(ll.CLOSE_MIN, 15 * 60)
        self.assertEqual(ll.MORNING_START, 9 * 60 + 28)

    def test_live_loop_mark_alignment(self):
        """刻度对齐到 :00/:10/:20…（STEP=10）：09:31:07 → 等 533s 到 09:40；
        正点刻度 09:30:00 → 等 600s；差 1s 到刻度 → 等 1s。"""
        ll = importlib.import_module("tools.live_loop")
        bjt = _dt.timezone(_dt.timedelta(hours=8))
        s = ll._sleep_to_next_mark(
            _dt.datetime(2026, 9, 24, 9, 31, 7, tzinfo=bjt))
        self.assertEqual(s, 533)
        s2 = ll._sleep_to_next_mark(
            _dt.datetime(2026, 9, 24, 9, 30, 0, 0, tzinfo=bjt))
        self.assertEqual(s2, 600)
        s3 = ll._sleep_to_next_mark(
            _dt.datetime(2026, 9, 24, 9, 39, 59, tzinfo=bjt))
        self.assertEqual(s3, 5.0, "间隔下限 5s：防时钟漂移导致的 0 间隔死转")

    def test_live_loop_once_cycle_gated_by_window(self):
        """非交易时段 _one_cycle 不触发巡检（守门在窗口判断，不在异常处理）。"""
        ll = importlib.import_module("tools.live_loop")
        intra = importlib.import_module("pipeline.intraday")
        bjt = _dt.timezone(_dt.timedelta(hours=8))
        noon = _dt.datetime(2026, 9, 24, 12, 10, tzinfo=bjt)   # 午休
        with mock.patch.object(intra, "run") as run_mock:
            self.assertFalse(ll._one_cycle("2026-09-24", noon))
            run_mock.assert_not_called()

    def test_sched_from_beijing_base(self):
        """am 存的是 hours=[9]/minutes=[45] ⇒ 存储口径=北京时间。"""
        sched = self.tl._sched_for(
            {"schedule": {"hours": [9], "minutes": [45],
                          "timezone": "Asia/Shanghai"}})
        self.assertEqual(sched["hours"], [9, 10, 11, 13, 14])
        self.assertEqual(sched["minutes"], [0, 10, 20, 30, 40, 50])
        self.assertEqual(sched["timezone"], "Asia/Shanghai")

    def test_sched_from_utc_base(self):
        """am 存的是 hours=[1]/minutes=[45] ⇒ 存储口径=UTC（09:45-8h）。"""
        sched = self.tl._sched_for(
            {"schedule": {"hours": [1], "minutes": [45],
                          "timezone": "Etc/UTC"}})
        self.assertEqual(sched["hours"], [1, 2, 3, 5, 6])

    def test_sched_unrecognized_refused(self):
        self.assertIsNone(self.tl._sched_for(
            {"schedule": {"hours": [7], "minutes": [13]}}),
            "无法反推口径时必须拒绝（宁可失败，不可建出错误时刻的定时器）")

    def test_retitle_body(self):
        body = '{"ref": "main", "inputs": {"task": "intraday", "slot": "am"}}'
        out = self.tl._retitle_body(body)
        self.assertIn('"slot": "live"', out)
        self.assertIn('"task": "intraday"', out)

    def test_retitle_body_without_slot_refused(self):
        self.assertIsNone(self.tl._retitle_body('{"ref": "main"}'),
                          "报文里没有 slot 不得静默放行")


class TestWiring(_LedgerIsolated):
    """入口与 workflow 接线。"""

    def test_build_cli_accepts_live(self):
        src = open(os.path.join(ROOT, "pipeline", "build.py"),
                   encoding="utf-8").read()
        self.assertRegex(
            src, r'add_argument\("--slot".*choices=\["am", "pm", "live"\]')

    def test_workflow_dispatches_live_slot(self):
        src = open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                   encoding="utf-8").read()
        self.assertIn("timer-live", src)
        self.assertIn("tools/timer_live.py", src)
        self.assertIn("live=买点巡检", src)
        # timer-live 不得触发站点重建/Pages 上传/推送验收
        # ⚠️ 必须用 "- name: 步骤名" 定位：步骤名会先以注释形式出现在
        # build 步骤的说明里，裸 find() 会命中注释段（09-26 实测踩坑）。
        for step in ("构建加密站点", "上传 Pages 构件", "推送验收（自检）"):
            i = src.find("- name: " + step)
            self.assertGreater(i, 0, f"找不到步骤 {step}")
            j = src.find("- name:", i + 10)
            seg = src[i:j if j > 0 else len(src)]
            self.assertIn("!= 'timer-live'", seg,
                          f"{step} 未排除 timer-live 任务")

    def test_executor_step_covers_live(self):
        """模拟盘自动建仓步骤必须覆盖 intraday 任务（live 搭 intraday 顺风车）：
        新买点出现时模拟盘同步按区间建仓。"""
        src = open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                   encoding="utf-8").read()
        i = src.find("- name: 模拟盘自动运行")
        self.assertGreater(i, 0, "找不到模拟盘步骤")
        seg = src[i:src.find("- name:", i + 10)]
        self.assertIn("== 'intraday'", seg)
        self.assertIn("--slot", seg)

    def test_every_case_isolates_ledger(self):
        for name, obj in list(globals().items()):
            if (isinstance(obj, type) and issubclass(obj, unittest.TestCase)
                    and obj.__module__ == __name__
                    and obj is not _LedgerIsolated):
                self.assertTrue(issubclass(obj, _LedgerIsolated),
                                f"{name} 必须继承 _LedgerIsolated")


if __name__ == "__main__":
    unittest.main()
