# -*- coding: utf-8 -*-
"""模拟盘自动运行的回归锁（2026-09-18 新增，用户需求）。

用户原话：「是不是还有模拟盘没有运行？请全面修复，按照 100000 元起步
开始自动运行」。诊断出的两条根因，本套件各锁一条：

A. **功能缺失**（本套件主体）
   `pipeline/executor.py` 原来只有"退出裁决"、**没有任何买入路径** ⇒
   账户永远空仓 ⇒ 巡逻无对象 ⇒ 日志为空 ⇒ 一条推送都发不出去。
   现在 `auto_open()` 必须能从空账（¥100,000）按当日推荐自动建仓。

B. **通道缺失**（结构断言）
   executor 单跑时 runner 是空库、且状态无处持久化 ⇒ 已在 workflow 层
   把模拟盘挂到 close 主链之后，并保留 restore-only 的手动通道。
   这些都是 YAML 事实，用源码断言锁住。

纪律：全部在内存库上跑，**绝不碰生产 cache/market.db**，
并且 `notifier.push` 一律 mock（模拟盘跑测试不许真发推送）。
"""
import importlib
import os
import re
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ⚠️ 必须自带 sys.path 引导（前面的 e2e 套件会改 cwd）。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pipeline.core as core              # noqa: E402
import pipeline.executor as executor      # noqa: E402
import pipeline.notifier as notifier      # noqa: E402

DATE = "2026-09-18"
PREV = "2026-09-17"


def _mkcon():
    con = sqlite3.connect(":memory:")
    con.executescript(core._SCHEMA)
    return con


def _price(con, code, price, date=DATE, prev=None):
    con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, date, price, price, price, price, 1e6, 3e7, 0.0, 1.0))
    if prev:
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (code, PREV, prev, prev, prev, prev, 1e6, 3e7, 0.0, 1.0))
    con.commit()


def _plan(con, code, lo, hi, score=80.0, action="现在买", date=DATE):
    con.execute("INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (date, code, "票" + code[-2:], "趋势", action, lo, hi,
                 round(lo * 0.95, 2), None, None, score, "", None))
    con.commit()


class TestAutoOpen(unittest.TestCase):
    """A. 自动建仓必须真的能建成仓。"""

    def test_account_starts_at_100k(self):
        con = _mkcon()
        executor.ensure_account(con, DATE)
        cash, day_start, day_key, frozen = con.execute(
            "SELECT cash, day_start_equity, day_key, frozen FROM account_state"
        ).fetchone()
        self.assertEqual(cash, 100000.0, "起步资金必须是 10 万")
        self.assertEqual(day_start, 100000.0)
        self.assertEqual(frozen, 0)
        self.assertIn("起步 ¥100,000", executor.account_line(con, DATE))

    def test_no_plan_no_trade(self):
        con = _mkcon()
        self.assertEqual(executor.auto_open(con, DATE), [],
                         "没有推荐必须静默（不推空消息、不硬凑）")

    def test_buys_when_price_in_zone(self):
        con = _mkcon()
        _price(con, "sh600001", 10.0, prev=9.9)
        _plan(con, "sh600001", 9.8, 10.2)
        log = executor.auto_open(con, DATE)
        actions = [a for _, a, _ in log]
        self.assertIn("BUY", actions, f"现价在买区内必须建仓，实得 {log}")
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM position_batches").fetchone()[0], 1)
        cash = con.execute("SELECT cash FROM account_state").fetchone()[0]
        self.assertLess(cash, 100000.0, "买入后现金必须减少")
        self.assertAlmostEqual(executor.equity(con), 100000.0, delta=300,
                              msg="建仓后净值应仍约等于 10 万（费用极小）")
        # 单笔金额落在风控区间内
        amt = con.execute("SELECT SUM(qty*cost) FROM position_batches").fetchone()[0]
        self.assertGreaterEqual(amt, executor.RISK["min_order_amt"])

    def test_qty_is_board_lot(self):
        con = _mkcon()
        _price(con, "sh600001", 10.0)
        _plan(con, "sh600001", 9.8, 10.2)
        executor.auto_open(con, DATE)
        qty = con.execute("SELECT qty FROM position_batches").fetchone()[0]
        self.assertEqual(qty % 100, 0, "A 股必须整手（100 股）下单")

    def test_t1_locks_today_batch(self):
        con = _mkcon()
        _price(con, "sh600001", 10.0)
        _plan(con, "sh600001", 9.8, 10.2)
        executor.auto_open(con, DATE)
        self.assertEqual(executor.available_qty(con, "sh600001", DATE), 0,
                         "当日买入批次 T+1 不可卖（M26）")
        # 解锁发生在**日切**（ensure_account 跨日）——必须走真实路径验证，
        # 直接查 available_qty 不会解锁（那才是漏测）。
        executor.ensure_account(con, "2026-09-21")
        self.assertGreater(executor.available_qty(con, "sh600001", "2026-09-21"), 0,
                           "跨日后昨日批次必须解锁可卖")

    def test_skips_price_above_zone(self):
        """现价跳出买区 → 不许追（与推送 buyable_now 同一把尺子）。"""
        con = _mkcon()
        _price(con, "sh600001", 12.0)
        _plan(con, "sh600001", 9.8, 10.2)
        log = executor.auto_open(con, DATE)
        self.assertEqual([a for _, a, _ in log], ["SKIP"])
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM position_batches").fetchone()[0], 0)

    def test_skips_when_no_price(self):
        con = _mkcon()
        _plan(con, "sh600001", 9.8, 10.2)      # 没有任何行情
        log = executor.auto_open(con, DATE)
        self.assertEqual([a for _, a, _ in log], ["SKIP"])
        self.assertIn("无当日价格", log[0][2])

    def test_skips_when_less_than_one_lot(self):
        """高价股买不到 1 手 → 跳过，不许放松风控去凑单。"""
        con = _mkcon()
        _price(con, "sh600001", 300.0)
        _plan(con, "sh600001", 290.0, 310.0)
        log = executor.auto_open(con, DATE)
        self.assertEqual([a for _, a, _ in log], ["SKIP"])
        self.assertIn("1 手", log[0][2])

    def test_max_holdings_respected(self):
        con = _mkcon()
        for i in range(6):
            _price(con, f"sh60000{i}", 10.0)
            _plan(con, f"sh60000{i}", 9.8, 10.2)
        executor.auto_open(con, DATE)
        n = con.execute("SELECT COUNT(DISTINCT code) FROM position_batches").fetchone()[0]
        self.assertEqual(n, executor.RISK["max_holdings"],
                         f"最多持有 {executor.RISK['max_holdings']} 只")

    def test_no_duplicate_buy_of_held(self):
        con = _mkcon()
        _price(con, "sh600001", 10.0)
        _plan(con, "sh600001", 9.8, 10.2)
        executor.auto_open(con, DATE)
        second = executor.auto_open(con, DATE)
        self.assertNotIn("BUY", [a for _, a, _ in second],
                         "已持仓的票不得重复建仓")
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM position_batches").fetchone()[0], 1)

    def test_frozen_blocks_new_positions(self):
        """M22 日内熔断锁定 → 不开新仓。"""
        con = _mkcon()
        _price(con, "sh600001", 10.0)
        _plan(con, "sh600001", 9.8, 10.2)
        executor.ensure_account(con, DATE)
        con.execute("UPDATE account_state SET frozen=1 WHERE id=1")
        con.commit()
        log = executor.auto_open(con, DATE)
        self.assertEqual([a for _, a, _ in log], ["HOLD"])
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM position_batches").fetchone()[0], 0)

    def test_cashflow_reconciles(self):
        """M32 分账可对账：现金余额 == 初始资金 + 全部流水。"""
        con = _mkcon()
        _price(con, "sh600001", 10.0)
        _plan(con, "sh600001", 9.8, 10.2)
        executor.auto_open(con, DATE)
        cash = con.execute("SELECT cash FROM account_state").fetchone()[0]
        flow = con.execute("SELECT SUM(amount) FROM cashflow").fetchone()[0]
        self.assertAlmostEqual(cash, flow, delta=0.01,
                               msg="现金必须等于初始资金加全部流水（M32）")


class TestRunWiring(unittest.TestCase):
    """run() 的接线：auto 必须真的建仓并推送，其他 task 不许偷偷买入。"""

    def _con(self):
        con = _mkcon()
        _price(con, "sh600001", 10.0)
        _plan(con, "sh600001", 9.8, 10.2)
        return con

    def _run(self, task, con):
        with mock.patch.object(executor, "get_conn", return_value=con), \
                mock.patch.object(executor, "today_str", return_value=DATE), \
                mock.patch.object(notifier, "push",
                                  return_value={"status": "sent"}) as push:
            log = executor.run(task)
        return log, push

    def test_run_auto_opens_and_pushes(self):
        log, push = self._run("auto", self._con())
        self.assertIn("BUY", [a for _, a, _ in log])
        self.assertTrue(push.called, "建仓必须有推送（否则用户看不到模拟盘在跑）")
        mode = push.call_args[0][0]
        self.assertEqual(mode, "exec_auto", "mode 必须按 task 分流，避免撞去重保险丝")
        body = push.call_args[0][2]
        self.assertIn("起步 ¥100,000", body)

    def test_run_scan_does_not_buy(self):
        """scan/tail/now 保持原语义：只巡逻，不买入（不许静默改变行为）。"""
        con = self._con()
        log, _ = self._run("scan", con)
        self.assertNotIn("BUY", [a for _, a, _ in log])
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM position_batches").fetchone()[0], 0)


class TestWorkflowWiring(unittest.TestCase):
    """B. 通道事实：模拟盘必须挂在有数据、能持久化状态的位置。"""

    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_main_chain_runs_executor_after_close(self):
        src = self._read(".github", "workflows", "stock.yml")
        self.assertIn("tools.executor --task auto", src,
                      "close 主链必须跑模拟盘（否则 runner 是空库、状态也无处持久化）")
        self.assertIn("continue-on-error", src,
                      "模拟盘是旁路，必须 continue-on-error，不得连坐主链推送")

    def test_manual_channel_restores_cache(self):
        src = self._read(".github", "workflows", "executor.yml")
        self.assertIn("actions/cache/restore@v4", src,
                      "手动通道必须恢复行情库缓存（否则永远是空库）")
        self.assertNotIn("actions/cache/save@v4", src,
                         "手动通道不得 save，避免覆盖线上模拟盘账户状态")
        self.assertIn("restore-keys", src)

    def test_no_ternary_in_workflows(self):
        """Actions 表达式不支持三元 `?:` —— 出现即整份 workflow 解析失败（血案）。"""
        for name in ("stock.yml", "executor.yml", "watchdog.yml"):
            src = self._read(".github", "workflows", name)
            raw = "\n".join(l for l in src.splitlines()
                            if not l.lstrip().startswith("#"))
            self.assertNotRegex(raw, r"\?\s*[^:\n]+\s*:\s*[^\s]",
                                f"{name} 疑似三元表达式（会让 workflow 解析失败）")

    def test_executor_module_has_no_private_fallback(self):
        """模拟盘不得悄悄回退到"只巡逻"——auto 是默认形态。"""
        src = ""
        with open(os.path.join(ROOT, "tools", "executor.py"), encoding="utf-8") as f:
            src = f.read()
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        self.assertIn('default="auto"', src,
                      "CLI 默认 task 必须是 auto（否则手动跑永远不建仓）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
