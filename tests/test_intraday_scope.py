# -*- coding: utf-8 -*-
"""M41 盘中计划校验的回归锁（2026-09-16 新增）。

锁两组**不可回退**的不变量：

A. 零污染（数据安全）
   盘中模块只能用实时快照、只写独立表 `snapshot_live`；
   **绝不允许**调用 fetch_daily()、**绝不允许**写 klines / snapshot 主表。
   理由：盘中价不是收盘价。写进主表 → 次日全部引擎基于假收盘价出信号
   （与 09-16「盘前候选 0」血案同源：用不该用的数据做判断）。

B. 打扰纪律（用户口径）
   没有实质内容就静默，不占推送额度；且 mode 必须与收盘通道隔离，
   不能撞上 build_close 的日级保险丝。
"""
import datetime as _dt
import importlib
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ⚠️ 必须自带 sys.path 引导：run_regression.py 顺序跑多个套件，前面的套件会
# 改变 cwd（e2e 类测试要重定向工作根目录），此时 `import pipeline` 会
# ModuleNotFoundError —— 表现为「单独跑全绿、进回归 15 个 ERROR」。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATE = "2026-09-16"          # 真实交易日（周三）
BJT = _dt.timezone(_dt.timedelta(hours=8))


def strip_comments(src):
    """剥掉注释与字符串字面量外的注释，避免命中注释里的历史写法（血案：踩过两次）。"""
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    src = re.sub(r"^\s*#.*$", "", src, flags=re.M)
    return src


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
    """pairs: [(裸码, 现价, 涨幅)] → fetch_universe 的返回结构。

    默认补齐到 520 只：模块内置「快照严重不足 ⇒ 判源异常不推」的保护，
    真实盘中应有 4500+ 只；测试要用 pad=False 才能验那条保护。
    """
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
    """跑一次盘中校验：mock 掉网络与凭据，push 走真实逻辑（含账本/去重）。"""
    intra = importlib.import_module("pipeline.intraday")
    fetch_daily = importlib.import_module("pipeline.fetch_daily")
    notifier = importlib.import_module("pipeline.notifier")
    build = importlib.import_module("pipeline.build")
    con = con or _mkcon()
    # 账本重定向到系统临时目录（绝不写仓库：CI checkout 的仓库账本会污染断言）
    tmp_ledger = os.path.join(tempfile.gettempdir(),
                              "_intraday_test_ledger.json")
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
                        now=now or _dt.datetime(2026, 9, 16, 14, 40,
                                                tzinfo=BJT))
    if os.path.exists(tmp_ledger):
        os.remove(tmp_ledger)
    return res, con, notifier


class _LedgerIsolated(unittest.TestCase):
    """账本隔离基类：**mock 作用域必须覆盖断言本身**。

    `notifier._daily_sent()` 的文件分支无条件读模块级 `DIST_LEDGER`
    （仓库 `dist/push_ledger.json`）。CI 的 `actions/checkout` 会把**真实账本**
    拉到工作区，所以任何"在 with 块之外"的 `_daily_sent` 断言都会命中真实数据。

    血案（2026-09-16，CI run 35067193010）：`_run()` 只在 `with` 块内把
    `DIST_LEDGER` 重定向到临时路径，断言写在块外 ⇒ 真实账本被读到。
    当时之所以"本地全绿"，是因为**当天真实的 `intraday_pm` 记录还没回写进
    仓库**；14:40 那次真实推送把该记录提交回来之后，CI 上的 checkout 里就
    有了它 ⇒ `assertFalse` 立刻变红。**这是一个按"现实世界发生的事件"定时
    引爆的测试**，比普通 flaky 更隐蔽（本文件 DATE 写死，触发后不会自愈）。

    修法：类级 setUpClass 打一次 patch，生命周期覆盖整个测试类（含断言）。
    """

    @classmethod
    def setUpClass(cls):
        notifier = importlib.import_module("pipeline.notifier")
        cls._tmp_dist = os.path.join(
            tempfile.gettempdir(), "_intraday_dist_%s.json" % id(cls))
        if os.path.exists(cls._tmp_dist):
            os.remove(cls._tmp_dist)
        cls._patch = mock.patch.object(notifier, "DIST_LEDGER", cls._tmp_dist)
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()
        if os.path.exists(getattr(cls, "_tmp_dist", "")):
            os.remove(cls._tmp_dist)


class TestZeroPollution(_LedgerIsolated):
    """A. 盘中绝不能污染收盘口径的数据。"""

    def setUp(self):
        self.src = strip_comments(
            open(os.path.join(ROOT, "pipeline", "intraday.py"),
                 encoding="utf-8").read())

    def test_never_calls_full_fetch(self):
        """只能调 fetch_universe()（纯 HTTP），不得调 fetch_daily() 写库版。"""
        self.assertIn("fetch_universe()", self.src)
        self.assertNotIn("fetch_daily.fetch_daily", self.src)
        self.assertNotRegex(self.src, r"fetch_daily\s*\.\s*fetch_daily\s*\(")
        # 更严：任何 fetch_daily( 调用形式都不许出现（那是写主表的入口）
        self.assertNotRegex(self.src, r"(?<!fetch_)\bfetch_daily\(")

    def test_never_writes_main_tables(self):
        """不得写 klines / snapshot 主表；只能写 snapshot_live。"""
        self.assertNotRegex(self.src, r"INSERT[^;]*INTO\s+klines")
        self.assertNotRegex(self.src, r"INSERT[^;]*INTO\s+snapshot\s*[( ]")
        self.assertNotRegex(self.src, r"\bupsert_klines\s*\(")
        self.assertIn("snapshot_live", self.src)

    def test_snapshot_live_table_isolated(self):
        """独立表必须存在，且主表结构未被改动（回归护栏）。"""
        core = importlib.import_module("pipeline.core")
        self.assertIn("snapshot_live", core._SCHEMA)
        con = _mkcon()
        cols = [r[1] for r in con.execute("PRAGMA table_info(snapshot)")]
        self.assertEqual(cols, ["date", "code", "name", "price", "pct",
                                "amt", "turn", "fmv"], "snapshot 主表结构变了")
        live = [r[1] for r in con.execute("PRAGMA table_info(snapshot_live)")]
        self.assertEqual(live, ["date", "slot", "code", "name", "price",
                                "pct", "amt"])
        # 两表必须**物理分离**：列结构不同，不存在「同表加 slot 列」的降级实现
        self.assertNotEqual(cols, live)

    def test_run_leaves_main_tables_untouched(self):
        """端到端：跑完一次盘中校验，主表行数必须仍为 0。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        res, con, _ = _run("pm", _snap([("600519", 20.5, 1.2)]), con=con)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM klines"
                                     ).fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM snapshot"
                                     ).fetchone()[0], 0)
        self.assertGreaterEqual(
            con.execute("SELECT COUNT(*) FROM snapshot_live").fetchone()[0], 1,
            "实时价必须落独立表")
        self.assertIn("universe", res)


class TestClassify(_LedgerIsolated):
    """状态判定只用「价 vs 区间」比较，不引用任何引擎阈值。"""

    def setUp(self):
        self.intra = importlib.import_module("pipeline.intraday")

    def test_states(self):
        f = self.intra.classify
        self.assertEqual(f(20.5, 1.0, 20.0, 21.0, None)[0], "in_zone")
        self.assertEqual(f(21.5, 5.0, 20.0, 21.0, None)[0], "above")
        self.assertEqual(f(19.0, -3.0, 20.0, 21.0, None)[0], "below")
        self.assertEqual(f(18.0, -8.0, 20.0, 21.0, 18.5)[0], "broke_stop")
        self.assertEqual(f(22.0, 10.0, 20.0, 21.0, None)[0], "limit_up")
        self.assertEqual(f(None, None, 20.0, 21.0, None)[0], "no_data")
        self.assertEqual(f(0, 0, 20.0, 21.0, None)[0], "no_data")

    def test_stop_beats_zone(self):
        """破止损的优先级必须高于「在买区内」——绝不能让破位票显示成可买。"""
        self.assertEqual(
            self.intra.classify(20.5, -5.0, 20.0, 21.0, 20.6)[0], "broke_stop")


class TestWindowGuard(_LedgerIsolated):
    """防误触发：非盘中时段一律跳过，不抓不推。"""

    def test_pm_outside_window(self):
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        res, _, _ = _run("pm", _snap([("600519", 20.5, 1.0)]), con=con,
                         now=_dt.datetime(2026, 9, 16, 10, 0, tzinfo=BJT))
        self.assertFalse(res["pushed"])
        self.assertIn("非pm时段", res["reason"])

    def test_am_outside_window(self):
        con = _mkcon()
        res, _, _ = _run("am", _snap([("600519", 20.5, 1.0)]), con=con,
                         now=_dt.datetime(2026, 9, 16, 20, 0, tzinfo=BJT))
        self.assertFalse(res["pushed"])
        self.assertIn("非am时段", res["reason"])

    def test_non_trading_day_skipped(self):
        con = _mkcon()
        intra = importlib.import_module("pipeline.intraday")
        fetch_daily = importlib.import_module("pipeline.fetch_daily")
        with mock.patch.object(fetch_daily, "fetch_universe",
                               lambda *a, **k: self.fail("非交易日不得抓取")):
            res = intra.run(slot="pm", date="2026-09-13", con=con,   # 周日
                            now=_dt.datetime(2026, 9, 13, 14, 40, tzinfo=BJT))
        self.assertFalse(res["pushed"])
        self.assertIn("非交易日", res["reason"])


class TestPushDiscipline(_LedgerIsolated):
    """B. 有实质内容才推；无内容静默；通道隔离。"""

    def test_pm_pushes_when_price_in_zone(self):
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        _plan(con, "sz000001", "平安银行", 9.0, 9.5)
        res, con, notifier = _run(
            "pm", _snap([("600519", 20.5, 1.0), ("000001", 12.0, 3.0)]),
            con=con)
        self.assertTrue(res["pushed"], f"尾盘进买区必须推，实际 {res}")
        self.assertEqual(res["in_zone"], 1)
        self.assertTrue(notifier._daily_sent(con, "intraday_pm", DATE))

    def test_pm_silent_when_nothing_actionable(self):
        """全部涨出买区 → 没有机会就不凑数，一条都不发。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        _plan(con, "sz000001", "平安银行", 9.0, 9.5)
        res, con, notifier = _run(
            "pm", _snap([("600519", 25.0, 6.0), ("000001", 11.0, 5.0)]),
            con=con)
        self.assertFalse(res["pushed"])
        self.assertIn("静默", res["reason"])
        self.assertFalse(notifier._daily_sent(con, "intraday_pm", DATE),
                         "静默时不得占用当日额度（否则真实机会被自家保险丝拦掉）")

    def test_am_pushes_on_deterioration(self):
        con = _mkcon()
        for i, c in enumerate(("sh600519", "sz000001", "sh601318", "sz000002")):
            _plan(con, c, "票%d" % i, 20.0, 21.0)
        # 3/4 跌破下沿 → ≥50% ⇒ 计划转差警示
        res, _, _ = _run("am",
                         _snap([("600519", 19.0, -2.0), ("000001", 19.0, -2.0),
                                ("601318", 19.0, -2.0), ("000002", 20.5, 0.5)]),
                         con=con, now=_dt.datetime(2026, 9, 16, 9, 45,
                                                   tzinfo=BJT))
        self.assertTrue(res["pushed"], f"计划多数跌破必须警示，实际 {res}")
        self.assertEqual(res["broken"], 3)

    def test_am_silent_when_plan_holds(self):
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        res, _, _ = _run("am", _snap([("600519", 20.5, 0.5)]), con=con,
                         now=_dt.datetime(2026, 9, 16, 9, 45, tzinfo=BJT))
        self.assertFalse(res["pushed"])

    def test_holding_stop_always_pushes(self):
        """持仓破止损是最高优先：即使 pm 无票进买区也必须推。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)          # 涨飞，不推
        held = [{"code": "sz000001", "name": "平安银行", "stop": 9.5}]
        res, _, _ = _run("pm", _snap([("600519", 25.0, 6.0),
                                      ("000001", 9.1, -4.0)]),
                         held=held, con=con)
        self.assertTrue(res["pushed"], f"持仓破止损必须推，实际 {res}")
        self.assertEqual(res["stops"], 1)

    def test_channel_isolated_from_close_slot(self):
        """盘中 mode 必须与收盘通道隔离，不得撞 build_close 的日级保险丝。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        _, con, notifier = _run("pm", _snap([("600519", 20.5, 1.0)]), con=con)
        self.assertFalse(notifier._daily_sent(con, "build_close", DATE),
                         "盘中推送不得占用收盘额度")
        modes = {r[0] for r in con.execute(
            "SELECT mode FROM push_ledger").fetchall()}
        self.assertIn("intraday_pm", modes)
        self.assertNotIn("build_close", modes)

    def test_abnormal_snapshot_blocks_push(self):
        """源异常（快照严重不足）→ 宁可不推，也不推基于残缺数据的判断。"""
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        res, _, _ = _run("pm", _snap([("600519", 20.5, 1.0)], pad=False),
                         con=con)
        self.assertFalse(res["pushed"])
        self.assertIn("快照异常", res["reason"])

    def test_dry_run_never_sends(self):
        con = _mkcon()
        _plan(con, "sh600519", "贵州茅台", 20.0, 21.0)
        res, con, notifier = _run("pm", _snap([("600519", 20.5, 1.0)]),
                                  con=con, dry=True)
        self.assertFalse(res["pushed"])
        self.assertIn("dry-run", res["reason"])
        self.assertFalse(notifier._daily_sent(con, "intraday_pm", DATE))


class TestWiring(_LedgerIsolated):
    """接线：入口、workflow、无三元表达式。"""

    def test_every_case_class_isolates_ledger(self):
        """★ 结构性护栏：本文件**所有** TestCase 子类都必须继承
        `_LedgerIsolated`，否则将来新增的类又会在断言里读到仓库真实账本，
        重现 09-16 那次"随真实推送引爆"的 CI 假 FAIL。"""
        for name, obj in list(globals().items()):
            if (isinstance(obj, type) and issubclass(obj, unittest.TestCase)
                    and obj.__module__ == __name__
                    and obj is not _LedgerIsolated):
                self.assertTrue(
                    issubclass(obj, _LedgerIsolated),
                    f"{name} 必须继承 _LedgerIsolated（账本隔离要覆盖断言）")

    def test_ledger_patch_is_active_during_assertions(self):
        """隔离必须真的生效：模块常量此刻应指向临时路径，而非仓库 dist/。"""
        notifier = importlib.import_module("pipeline.notifier")
        self.assertNotEqual(
            os.path.normcase(os.path.abspath(notifier.DIST_LEDGER)),
            os.path.normcase(os.path.abspath(
                os.path.join(ROOT, "dist", "push_ledger.json"))),
            "DIST_LEDGER 未被隔离 —— 断言会读到真实账本")

    def test_build_accepts_intraday_task(self):
        src = open(os.path.join(ROOT, "pipeline", "build.py"),
                   encoding="utf-8").read()
        self.assertIn('"intraday"', src)
        self.assertIn("--slot", src)
        # 盘中必须走独立模块，不得进收盘 build() 主链
        self.assertIn('elif a.task == "intraday":', src)

    def test_workflow_supports_intraday(self):
        src = open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                   encoding="utf-8").read()
        self.assertIn("slot:", src, "workflow_dispatch 需暴露 slot 参数")
        self.assertIn("--task intraday", src)
        # ⚠️ 必须查**原文**，不能剥注释——这里与 Python 源码的规则**方向相反**：
        # `run: |` 块标量里的 `#` 只是 shell 注释，但 GitHub 的表达式扫描器
        # **照样会解析其中的花括号表达式**。2026-09-16 就是因为把三元的字面
        # 形式写进了 run 块内的注释，导致整个 workflow 解析失败
        # （dispatch 422、"failed to parse workflow"、run 零 job 零日志）。
        # 纪律：本文件任何位置（含注释）都不得出现三元表达式字面量。
        self.assertIsNone(
            re.search(r"\$\{\{[^}]*\?[^}]*:", src),
            "workflow 任何位置（含 run 块内注释）都不得出现三元表达式")

    def test_intraday_not_in_close_path(self):
        """盘中模块不得被 build() 主链引用（否则会污染收盘口径）。"""
        src = strip_comments(open(os.path.join(ROOT, "pipeline", "build.py"),
                                  encoding="utf-8").read())
        i = src.find("def build(")
        j = src.find("def main(")
        self.assertTrue(0 < i < j)
        self.assertNotIn("intraday", src[i:j],
                         "build() 主链内不得出现 intraday（必须物理隔离）")


if __name__ == "__main__":
    unittest.main()
