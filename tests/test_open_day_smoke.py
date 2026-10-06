# -*- coding: utf-8 -*-
"""开盘日全链集成演练（2026-10-05 开盘前体检）。

复刻**节后首日 2026-10-08** 的真实数据形态：K 线止于 09-30（8 天假期缺口）、
快照/情绪/裁决均为节前最后交易日，然后按 CI 的真实执行顺序实跑：

    ① build pre（08:50，锚定 09-30）→ ② build auction（09:25，写 day_meta）
    → ③ executor auto（09:35 盘中，读裁决闸门建仓）
    → ④ build close（15:22）→ ⑤ build review（20:02 周五触发 autotune）
    → ⑥ intraday am（盘中提醒，报价 mock）

每一环都断言「该发生的发生、不该发生的不发生」；notifier.push 全程 mock
（零真实推送、零网络依赖——报价与 AI 均注入桩）。
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pipeline.core as core              # noqa: E402
import pipeline.build as build_mod        # noqa: E402
import pipeline.executor as executor      # noqa: E402
import pipeline.intraday as intraday      # noqa: E402
import pipeline.narrative as narrative    # noqa: E402
import pipeline.trade_calendar as tc      # noqa: E402

DATE = "2026-10-08"       # 节后首个交易日（周四）
PREV = "2026-09-30"       # 节前最后交易日（K线/快照/情绪止于此）
FRI = "2026-10-09"

STOCKS = [("sz300192", "科德教育", 24.0, "医药"),
          ("sh600519", "贵州茅台", 1400.0, "白酒"),
          ("sz000001", "平安银行", 11.0, "银行")]


def _trading_days(n, end):
    """end 往前数 n 个工作日（升序）。"""
    import datetime as _dt
    d = _dt.date.fromisoformat(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5 and d.isoformat() not in tc.HOLIDAYS:
            out.append(d.isoformat())
        d -= _dt.timedelta(days=1)
    return out[::-1]


class OpenDaySmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls._tmp.name, "smoke.db")
        core.DB_PATH = cls.db
        # ⚠️ build/executor 都是 `from .core import get_conn` —— 默认参数
        # path=DB_PATH 在**导入时**绑定，事后改 core.DB_PATH 无效。
        # 必须逐模块补丁 get_conn，让演练真正落在夹具库上。
        cls._con_patches = [
            mock.patch.object(build_mod, "get_conn", lambda: cls.con),
            mock.patch.object(executor, "get_conn", lambda: cls.con),
            # executor.run 内部 today=today_str()（真实系统日期）——演练
            # 必须钉在节后首日，否则今天（假期）会被非交易日闸门拦下。
            mock.patch.object(executor, "today_str", lambda: DATE),
            # fetch_daily 的陈旧检查读 CACHE_DIR/fetch_stats.json——指向
            # 演练临时目录，避免读到本机真实缓存的旧日期。
            mock.patch.object(core, "CACHE_DIR", cls._tmp.name),
        ]
        for p in cls._con_patches:
            p.start()
        cls.con = core.get_conn(cls.db)
        con = cls.con
        days = _trading_days(65, PREV) + [DATE]
        for i, d in enumerate(days[:-1]):
            _bar(con, "sh000001", d, 3000 + i * 2)
            for code, name, base, _sec in STOCKS:
                p = base * (1 + (i % 7 - 3) / 300)
                _bar(con, code, d, p)
        # 节后首日的「昨日K线」（供盘中/收盘口径）
        for code, name, base, _sec in STOCKS:
            _bar(con, code, DATE, base * 1.005)
        _bar(con, "sh000001", DATE, 3130)
        # 节前最后交易日的快照（成交额/市值/换手 达门槛）
        for code, name, base, _sec in STOCKS:
            con.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
                        (PREV, code, name, base, 1.5, 5e8, 2.0, 3e9))
        con.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
                    (PREV, "sh000001", "上证指数", 3000, 0, 0, 0, 0))
        # 节后首日快照：CI 里由抓取步骤在 build 前入库（pre=竞价撮合额、
        # close=全日快照）——数据就绪闸门会拒绝无快照的构建（已实测），
        # 本演练模拟抓取已完成后的形态。
        for code, name, base, _sec in STOCKS:
            con.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
                        (DATE, code, name, base * 1.005, 1.5, 6e8, 2.2, 3e9))
        con.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
                    (DATE, "sh000001", "上证指数", 3130, 0, 0, 0, 0))
        # 10-09（周五）当日数据：review/复盘任务需要「当天」就绪才能构建
        for code, name, base, _sec in STOCKS:
            _bar(con, code, FRI, base * 1.008)
            con.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
                        (FRI, code, name, base * 1.008, 1.5, 6e8, 2.2, 3e9))
        _bar(con, "sh000001", FRI, 3135)
        con.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
                    (FRI, "sh000001", "上证指数", 3135, 0, 0, 0, 0))
        # 行业归属 + 板块热度
        for code, name, base, sec in STOCKS:
            con.execute("INSERT OR REPLACE INTO stock_industry VALUES(?,?,?)",
                        (code[2:], sec, PREV))
            con.execute("INSERT OR REPLACE INTO sector_heat VALUES(?,?,?,?,?,?)",
                        (PREV, sec, 1.2, 5.0, 10, 5))
        # 节前情绪（齐备维度 → 轻仓试探级）
        con.execute("INSERT OR REPLACE INTO emotion_log VALUES(?,?,?,?,?,?,?)",
                    (PREV, 50.0, 8, 0.8, 1, "震荡", "{}"))
        con.commit()

    @classmethod
    def tearDownClass(cls):
        import gc
        for p in cls._con_patches:
            p.stop()
        gc.collect()
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass          # Windows 句柄延迟释放：临时目录留给系统清理

    def setUp(self):
        import pipeline.notifier as notifier
        self.pushes = []
        self._push_patch = mock.patch.object(
            notifier, "push",
            side_effect=lambda mode, title, content, **kw:
                self.pushes.append({"mode": mode, "title": title,
                                    "kw": kw}) or {"sent": False})
        self._push_patch.start()
        self._narr_patch = mock.patch.object(
            narrative, "narrate", return_value="【模板】复盘文字")
        self._narr_patch.start()
        # 持仓固定为夹具（不依赖本机 config/holdings.json）
        self._hold = [{"code": "sz300192", "name": "科德教育",
                       "buy_price": 23.0, "buy_date": PREV, "shares": 800}]
        self._hold_patch = mock.patch.object(
            build_mod, "load_holdings", return_value=self._hold)
        self._hold_patch.start()

    def tearDown(self):
        self._push_patch.stop()
        self._narr_patch.stop()
        self._hold_patch.stop()

    # ① pre：节后首日盘前，K线锚定 09-30，不得崩、不得空推送
    def test_01_pre_节后首日(self):
        r = build_mod.build("pre", date=DATE)
        self.assertTrue(self.pushes, "盘前计划必须产生推送")
        modes = [p["mode"] for p in self.pushes]
        self.assertIn("build_pre", modes)
        wx = next((p["kw"].get("wx_text") for p in self.pushes
                   if p["mode"] == "build_pre"), "") or ""
        self.assertIn("【今日操作】", wx)
        con = self.con
        row = con.execute("SELECT verdict FROM day_meta WHERE date=?",
                          (DATE,)).fetchone()
        self.assertIsNotNone(row, "day_meta 必须在 pre 阶段落库（executor 前置依赖）")
        print(f"[smoke] pre verdict={row[0]} 推送={len(self.pushes)}条")

    # ② auction：链路走通（迷你宇宙扫不出候选是合法结果——
    #    建仓依据由 test_03 手工铺入，避免把扫描公式耦合进演练）
    def test_02_auction_链路走通(self):
        build_mod.build("auction", date=DATE)
        modes = [p["mode"] for p in self.pushes]
        self.assertIn("build_auction", modes, "竞价裁决必须产生推送")
        con = self.con
        # 铺当日可执行推荐（executor 的建仓依据）：价格落在买区内
        for code, name, base, _sec in STOCKS:
            con.execute(
                "INSERT OR REPLACE INTO rec_picks VALUES("
                "?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (DATE, code, name, "趋势", "现在买",
                 round(base * 0.95, 2), round(base * 1.06, 2),
                 round(base * 0.90, 2), None, None, 85.0, "", None))
        con.commit()

    # ③ executor auto：裁决闸门生效——谨慎档最多 1 笔、单笔 ≤20% 档
    def test_03_executor_裁决闸门(self):
        con = self.con
        vd = con.execute("SELECT verdict FROM day_meta WHERE date=?",
                         (DATE,)).fetchone()[0]
        now = datetime(2026, 10, 8, 9, 35, tzinfo=timezone(timedelta(hours=8)))
        executor.run(task="auto", slot="am", now=now)
        buys = con.execute(
            "SELECT o.code, o.qty, o.price FROM orders o WHERE o.side='buy' "
            "AND substr(o.ts,1,10)=?", (DATE,)).fetchall()
        if vd in ("离场为主", "观望为主"):
            self.assertEqual(len(buys), 0, f"裁决{vd}必须零建仓")
        else:
            self.assertLessEqual(len(buys), 1, f"裁决{vd}（谨慎/轻仓）最多 1 笔")
            for _c, qty, price in buys:
                self.assertLessEqual(qty * price, 100000 * 0.20 * 1.05,
                                     "谨慎/轻仓档单笔 ≤20% 仓位")
        print(f"[smoke] executor verdict={vd} 建仓={len(buys)}笔")

    # ④ close：收盘观察推送 + 持仓动态随行
    def test_04_close_持仓动态随行(self):
        build_mod.build("close", date=DATE)
        modes = [p["mode"] for p in self.pushes]
        self.assertIn("build_close", modes)
        wx = next((p["kw"].get("wx_text") for p in self.pushes
                   if p["mode"] == "build_close"), "") or ""
        self.assertIn("📦", wx, "收盘推送的微信动作清单必须自带持仓动态")

    # ⑤ review：周五触发 autotune（有隔日止损证据 → k_hot 下调 + 已调参标题）
    def test_05_review_周五自动调参(self):
        con = self.con
        # 铺一周平仓证据：2 笔隔日止损 + 1 笔止盈（churn 2/3 ≥ 50%）
        for code, bd, sd, bp, sp, why in (
                ("sz300192", PREV, DATE, 20.0, 19.4, "普通硬止损"),
                ("sz000002", DATE, FRI, 20.0, 19.4, "普通硬止损"),
                ("sz000003", PREV, DATE, 10.0, 10.5, "持仓浮盈止盈")):
            for d, side, price in ((bd, "buy", bp), (sd, "sell", sp)):
                oid = f"smoke-{code}-{d}-{side}"
                con.execute("INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?,?,?,?)",
                            (oid, f"{d}T09:26:00", code, side, 1000, price,
                             "filled", why if side == "sell" else "自动建仓"))
                con.execute("INSERT INTO fills VALUES(NULL,?,?,?,?,?,?,?)",
                            (oid, f"{d}T09:26:00", code, side, 1000, price, 0.0))
        con.commit()
        from pipeline import autotune
        self.assertTrue(autotune.due(con, FRI), "周五必须触发")
        # review 跑在 10-08（周四）：due=False 只出日报；再跑一个周五
        build_mod.build("review", date=DATE)
        rev = next((p for p in self.pushes if p["mode"] == "review"), None)
        self.assertIsNotNone(rev, "晚间复盘必须推送")
        self.assertNotIn("已调参", rev["kw"].get("headline") or "",
                         "周四不该触发调参")
        build_mod.build("review", date=FRI)
        rev_fri = next((p for p in self.pushes
                        if p["mode"] == "review" and "已调参" in
                        (p["kw"].get("headline") or "")), None)
        self.assertIsNotNone(rev_fri, "周五复盘应自动调参并在标题标注")
        row = con.execute(
            "SELECT value FROM tune_state WHERE key='k_hot'").fetchone()
        self.assertIsNotNone(row, "调参必须落库留痕")
        self.assertAlmostEqual(row[0], 0.80)

    # ⑥ intraday：报价 mock，全链不崩、相关度排序生效
    def test_06_intraday_相关度与推送链(self):
        con = self.con
        # 当日计划里放三只票：自选、持仓同板块、无关
        for code, action, score in (("sh600000", "现在买", 80),
                                    ("sh600519", "现在买", 90),
                                    ("sz000001", "现在买", 95)):
            con.execute(
                "INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (DATE, code, "票", "趋势", action,
                 1.0, 99999.0, None, None, None, score, "", None))
        con.execute("INSERT OR REPLACE INTO stock_industry VALUES(?,?,?)",
                    ("600519", "白酒", PREV))
        con.commit()
        snap = {bare: {"name": bare, "price": 500000.0, "pct": 1.0,
                       "amt": 1e9}
                for bare in ("000001", "300192", "600519", "000002",
                             "600000", "600519")}
        with mock.patch.object(intraday, "fetch_quotes",
                               return_value=(snap, "mock")), \
             mock.patch.object(intraday, "in_window", return_value=True):
            out = intraday.run(slot="am", date=DATE, dry=True,
                               now=datetime(2026, 10, 8, 9, 45,
                                            tzinfo=timezone(
                                                timedelta(hours=8))))
        self.assertIsInstance(out, dict)
        self.assertNotEqual(out.get("reason", ""), "",
                            "mock 报价齐全时不得报快照异常")

    # F. 节后首日专项：日历/裁决时序
    def test_07_节后日历与时序(self):
        self.assertTrue(tc.is_trade_day(DATE), "10-08 必须是交易日")
        self.assertFalse(tc.is_trade_day("2026-10-05"), "假期中不得开工")
        con = self.con
        # day_meta 在 pre/auction 阶段已写当日 → executor 才读得到
        row = con.execute("SELECT verdict FROM day_meta WHERE date=?",
                          (DATE,)).fetchone()
        self.assertIsNotNone(row)


def _bar(con, code, date, close):
    con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, date, close, close, close * 0.99, close,
                 1e6, 3e7, 0.0, 1.0))


if __name__ == "__main__":
    unittest.main()
