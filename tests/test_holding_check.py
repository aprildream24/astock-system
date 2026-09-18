# -*- coding: utf-8 -*-
"""真实持仓体检 + 换股建议（holding_check）回归锁。

覆盖：
  A. 退出裁决 cost_override 口径（真实持仓不在模拟盘批次 → 必须外部传成本）
  B. evaluate_real_holdings 结构化体检（浮亏/裁决/板块热冷）
  C. render_holding_advice 三段式渲染（概要/体检/候选）
  D. 接线纪律：build.py 在 review 挂了 holding_check；MODE_LABEL 登记
  E. 账本隔离纪律：本套件所有用例继承 _Base，绝不污染仓库 push_ledger

⚠️ 账本隔离是血泪教训：notifier.push 无条件读 DIST_LEDGER，仓库账本被 checkout
进工作区 → 第一遍绿第二遍红自爆。所以每个用例类都隔离账本到临时目录。
"""
import os
import sys
import sqlite3
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import executor as EX
from pipeline import notifier as N


# ---------------------------------------------------------------------------
# 账本隔离基类（与 test_exec_push 同源）
# ---------------------------------------------------------------------------
class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hc_ledger_")
        # 把账本重定向到临时目录，绝不动摇仓库 dist/push_ledger.json
        self._orig_ledger = getattr(N, "DIST_LEDGER", None)
        N.DIST_LEDGER = os.path.join(self._tmp, "push_ledger.json")
        open(N.DIST_LEDGER, "w", encoding="utf-8").close()

    def tearDown(self):
        if self._orig_ledger is not None:
            N.DIST_LEDGER = self._orig_ledger
        else:
            N.DIST_LEDGER = self._orig_ledger


def _build_db(path):
    """构造最小行情库：荣盛石化(sz002493) 一段下行趋势，触发 ATR 保护线。"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE klines(date TEXT, code TEXT, o REAL, c REAL, "
                "h REAL, l REAL, v REAL)")
    con.execute("CREATE TABLE snapshot(date TEXT, code TEXT, name TEXT, price REAL)")
    con.execute("CREATE TABLE stock_industry(code TEXT, sector TEXT, date TEXT)")
    con.execute("CREATE TABLE sector_heat(date TEXT, sector TEXT, pct REAL, "
                "net_yi REAL, up INTEGER, down INTEGER)")
    # evaluate_exit / _name_of 会查这两张表；测试库里建空表即可（无批次 → cost None）
    con.execute("CREATE TABLE position_batches(batch_id TEXT, code TEXT, "
                "cost REAL, buy_date TEXT, available REAL)")
    con.execute("CREATE TABLE rec_picks(date TEXT, code TEXT, name TEXT, "
                "tag TEXT, action TEXT, buy_low REAL, buy_high REAL, stop REAL, "
                "sell_low REAL, sell_high REAL, score REAL, outcome TEXT, "
                "outcome_ret REAL)")
    # 收盘价从高到低下行；09-09 最高点 15.17 → hh10 高，protect 线远高于现价
    rows = [
        ("2026-09-07", 14.0, 14.0, 14.5, 13.9),
        ("2026-09-08", 14.07, 14.49, 14.52, 13.91),
        ("2026-09-09", 14.35, 15.14, 15.17, 14.18),
        ("2026-09-10", 15.28, 14.37, 15.28, 14.29),
        ("2026-09-11", 14.3, 13.6, 14.37, 13.51),
        ("2026-09-12", 13.7, 13.5, 13.72, 13.4),
        ("2026-09-13", 13.6, 13.4, 13.62, 13.3),
        ("2026-09-14", 13.52, 13.2, 13.68, 13.14),
        ("2026-09-15", 13.21, 13.06, 13.3, 13.0),
        ("2026-09-16", 13.05, 13.03, 13.14, 12.74),
        ("2026-09-17", 12.93, 12.93, 13.12, 12.65),
        ("2026-09-18", 12.94, 12.85, 13.16, 12.73),
    ]
    for d, o, c, h, l in rows:
        con.execute("INSERT INTO klines VALUES(?,?,?,?,?,?,1000000)",
                    (d, "sz002493", o, c, h, l))
    con.execute("INSERT INTO snapshot VALUES('2026-09-18','sz002493','荣盛石化',12.85)")
    con.execute("INSERT INTO stock_industry VALUES('sz002493','炼化及贸易','2026-09-18')")
    # 当日热门板块（荣盛石化的板块不在其中 → 冷）
    for i, s in enumerate(["电子", "半导体", "通信", "电网设备", "机械设备"]):
        con.execute("INSERT INTO sector_heat VALUES('2026-09-18',?,2.0,100.0,50,5)", (s,))
    con.commit()
    return con


# ---------------------------------------------------------------------------
# A. cost_override 口径
# ---------------------------------------------------------------------------
class TestCostOverride(_Base):
    def test_no_cost_returns_zero_pnl(self):
        con = _build_db(os.path.join(self._tmp, "m.db"))
        act, reasons, detail = EX.evaluate_exit(con, "sz002493", "2026-09-18")
        # 无外部成本 → 模拟盘批次里没有这只 → cost=None → pnl 恒 0
        self.assertIn("+0.0%", detail)

    def test_cost_override_gives_real_pnl(self):
        con = _build_db(os.path.join(self._tmp, "m.db"))
        act, reasons, detail = EX.evaluate_exit(
            con, "sz002493", "2026-09-18", cost_override=13.06)
        # 13.06 → 12.85 实亏约 -1.6%
        self.assertIn("-1.6", detail)
        self.assertEqual(act, "SELL")  # ATR 保护线触发


# ---------------------------------------------------------------------------
# B. evaluate_real_holdings 结构化体检
# ---------------------------------------------------------------------------
class TestRealHoldingsEval(_Base):
    def _eval(self):
        con = _build_db(os.path.join(self._tmp, "m.db"))
        holdings = [{"code": "sz002493", "name": "荣盛石化",
                     "buy_price": 13.06, "buy_date": "2026-09-18"}]
        return EX.evaluate_real_holdings(con, "2026-09-18", holdings)

    def test_pnl_and_verdict(self):
        he = self._eval()
        self.assertEqual(len(he), 1)
        h = he[0]
        self.assertEqual(h["code"], "sz002493")
        self.assertAlmostEqual(h["pnl_pct"], (12.85 / 13.06 - 1) * 100, places=1)
        self.assertEqual(h["exit_action"], "SELL")
        self.assertIn("减仓", h["verdict"])

    def test_sector_cold_flag(self):
        he = self._eval()
        self.assertEqual(he[0]["sector"], "炼化及贸易")
        self.assertFalse(he[0]["sector_hot"])  # 不在当日热门前 20

    def test_missing_data_is_safe(self):
        con = _build_db(os.path.join(self._tmp, "m.db"))
        he = EX.evaluate_real_holdings(con, "2026-09-18",
                                      [{"code": "sz999999"}])
        self.assertEqual(he[0]["verdict"], "数据不足")


# ---------------------------------------------------------------------------
# C. 渲染三段式
# ---------------------------------------------------------------------------
class TestRenderHoldingAdvice(_Base):
    def test_contains_sections(self):
        he = [{"code": "sz002493", "name": "荣盛石化", "buy_price": 13.06,
               "close": 12.85, "pnl_pct": -1.61, "verdict": "建议减仓/离场",
               "exit_action": "SELL", "exit_reasons": ["ATR保护线"],
               "stop": 12.08, "zone": [12.65, 13.08], "state": "可买",
               "sector": "炼化及贸易", "sector_hot": False}]
        cands = [{"code": "sh600343", "name": "航天动力", "action": "次日竞价达标买",
                  "score": 70.3, "buy_low": 21.56, "buy_high": 22.32,
                  "stop": 19.94, "sector": "通用设备"}]
        html = N.render_holding_advice(he, cands, "2026-09-18")
        self.assertIn("持仓体检", html)
        self.assertIn("荣盛石化", html)
        self.assertIn("换股候选", html)
        self.assertIn("航天动力", html)
        self.assertIn("-1.61%", html)

    def test_empty_candidates(self):
        html = N.render_holding_advice([], [], "2026-09-18")
        self.assertIn("持仓体检", html)
        self.assertIn("今日无换股候选", html)


# ---------------------------------------------------------------------------
# D. 接线纪律（结构性）
# ---------------------------------------------------------------------------
class TestWiring(unittest.TestCase):
    def test_mode_label_registered(self):
        self.assertEqual(N.MODE_LABEL.get("holding_check"), "持仓")

    def test_build_wires_holding_check(self):
        """2026-09-19 更新：持仓体检已并入「晚间综合」一条推送（需求⑤降噪），
        不再单独 push。接线契约改为：review 分支渲染 holding_html 并传入
        render_evening_digest；digest 为空时跳过推送。"""
        with open(os.path.join(ROOT, "pipeline", "build.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("holding_html = notifier.render_holding_advice(", src)
        self.assertIn("notifier.render_evening_digest(", src)
        self.assertIn("holding_html", src.split("render_evening_digest")[1][:200])
        # 降噪：digest 为空时跳过，不硬凑一条
        self.assertIn("evening_digest empty", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
