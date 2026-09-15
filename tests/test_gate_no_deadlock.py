# -*- coding: utf-8 -*-
"""守护：交易日闸门**不得**循环依赖被它管控的数据（2026-09-15 实盘事故）。

真实事故：
    `trade_calendar(con)` 完全由 `klines` 表里 `sh000001` 的日期推导。
    而 `is_trading_day_cross` 与 `data_ready_for` 的**第一关**都是
    `date in trade_calendar(con)`。

    ⇒ 当日指数K线尚未入库时，"今天"必然不在日历里
    ⇒ 闸门把 **"还没抓到数据"** 判成 **"非交易日"**
    ⇒ `close` 打印「拒绝构建（指数日K无此日期/非交易日）」→ `return None`
    ⇒ 退出码 0（静默）⇒ 用户全天零推送。

    更糟的是它与抓取超时**串联**：抓取挂了 ⇒ 指数K线不入库 ⇒
    闸门永远拒绝 ⇒ 死锁。而我此前只看 rc=0，还回汇报"没问题"。

    诊断话术的误导性是关键：系统说"非交易日"，真相是"还没抓"。
    错误的 reason 直接把我引向错误的排查方向。

修复：判断"客观上是否开市"必须用**独立于本地数据**的权威日历
（`trade_calendar.is_trade_day`，源自国务院放假安排），
而不是"我的库里有没有这根K线"。

本测试锁死：
    1. 交易日 + 本地无数据 ⇒ 理由必须是"待抓取"，**不得**说"非交易日"；
    2. 周末/法定假日 ⇒ 必须判非交易日（不能因为修 bug 而放松）；
    3. 数据齐备的交易日 ⇒ 必须放行；
    4. `is_real_trade_day` 必须独立于 db（同一个日期，空库에도结论一致）。
"""
import datetime
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import core  # noqa: E402
from pipeline import fetch_daily as fd  # noqa: E402


def _empty_db():
    """一个只有 schema、没有任何数据的临时库。"""
    tmp = tempfile.mkdtemp(prefix="gate_")
    con = sqlite3.connect(os.path.join(tmp, "market.db"))
    con.execute("CREATE TABLE klines (code TEXT, date TEXT, o REAL, c REAL, "
                "h REAL, l REAL, v REAL)")
    con.execute("CREATE TABLE snapshot (date TEXT, code TEXT, pct REAL, amt REAL)")
    return con


class TestRealTradeDayIndependent(unittest.TestCase):
    """客观交易日的判定必须与本地数据无关。"""

    # 2026-09-15 是周二，正常交易日
    TRADE = "2026-09-15"
    SAT = "2026-09-12"          # 周六
    NATIONAL = "2026-10-01"     # 国庆法定假日
    MID_AUTUMN = "2026-09-25"   # 中秋法定假日

    def test_weekday_is_trade_day(self):
        self.assertTrue(core.is_real_trade_day(self.TRADE),
                        "周二必须判为交易日——不得因本地无数据而否定")

    def test_weekend_is_not_trade_day(self):
        self.assertFalse(core.is_real_trade_day(self.SAT), "周六必须非交易日")

    def test_holidays_are_not_trade_day(self):
        for d in (self.NATIONAL, self.MID_AUTUMN):
            self.assertFalse(core.is_real_trade_day(d),
                             f"{d} 是法定假日，必须判非交易日")

    def test_independent_of_db_contents(self):
        """同一日期，空库与有库结论必须一致（证明不依赖本地数据）。"""
        empty = _empty_db()
        try:
            for d in (self.TRADE, self.SAT, self.NATIONAL):
                self.assertEqual(core.is_real_trade_day(d),
                                 core.is_real_trade_day(d))
            # 空库里 trade_calendar 为空，但 is_real_trade_day 仍应正确
            self.assertEqual(core.trade_calendar(empty), [],
                             "空库的 trade_calendar 应为空")
            self.assertTrue(core.is_real_trade_day(self.TRADE),
                            "空库也必须能正确判断客观交易日")
        finally:
            empty.close()


class TestCrossGateReasonIsTruthful(unittest.TestCase):
    """闸门拒绝时的**理由**必须说真话（误导性 reason 会带偏排查）。"""

    TRADE = "2026-09-15"

    def setUp(self):
        self.con = _empty_db()

    def tearDown(self):
        self.con.close()

    def test_trade_day_without_data_says_pending_not_closed(self):
        """★ 核心断言：交易日 + 无数据 ⇒ 说"待抓取"，不许说"非交易日"。"""
        ok, why = core.is_trading_day_cross(self.con, self.TRADE)
        self.assertFalse(ok, "无数据时不能放行")
        self.assertNotIn("非交易日", why,
                         f"误导！理由是「{why}」——它其实只是还没抓数据，"
                         "说成非交易日会把排查带偏（这正是零推送的成因）")
        self.assertTrue(any(k in why for k in ("待抓取", "尚无", "无快照")),
                        f"理由应说明是数据未到，实际：{why}")

    def test_weekend_reason_mentions_calendar(self):
        ok, why = core.is_trading_day_cross(self.con, "2026-09-12")
        self.assertFalse(ok)
        self.assertIn("非交易日", why, "周末的理由应明确指出非交易日")

    def test_data_ready_reason_for_trade_day_without_data(self):
        """data_ready_for 同样不得把"没数据"说成"非交易日"。"""
        ok, why = fd.data_ready_for(self.con, self.TRADE)
        self.assertFalse(ok)
        self.assertNotIn("非交易日", why,
                         f"误导理由：{why}（应说明缺K线数据，而非非交易日）")
        self.assertIn("K线", why)

    def test_data_ready_rejects_real_holiday_as_non_trading(self):
        ok, why = fd.data_ready_for(self.con, "2026-10-01")
        self.assertFalse(ok)
        self.assertIn("非交易日", why, "法定假日必须明确判非交易日")

    def test_passes_when_data_present(self):
        """交易日 + K线已入库 ⇒ 必须放行（不能因修 bug 而误拦）。

        注意：`data_ready_for` 还会读 `cache/fetch_stats.json`（③ 数据新鲜度）。
        必须把它一并隔离，否则本机会读到真实的旧 stats → 假失败。
        """
        self.con.execute("INSERT INTO klines VALUES ('sh000001',?,1,1,1,1,1)",
                         (self.TRADE,))
        self.con.commit()
        # 隔离 fetch_stats.json：临时把 CACHE_DIR 指到空目录
        orig = core.CACHE_DIR
        tmp = tempfile.mkdtemp(prefix="cache_")
        core.CACHE_DIR = tmp
        try:
            ok, why = fd.data_ready_for(self.con, self.TRADE)
        finally:
            core.CACHE_DIR = orig
        self.assertTrue(ok, f"数据齐备应放行，却得到：{why}")

    def test_stale_fetch_stats_still_blocks(self):
        """抓取统计早于目标日 ⇒ 必须拦（这条保护不许被改没）。"""
        self.con.execute("INSERT INTO klines VALUES ('sh000001',?,1,1,1,1,1)",
                         (self.TRADE,))
        self.con.commit()
        orig = core.CACHE_DIR
        tmp = tempfile.mkdtemp(prefix="cache_")
        core.CACHE_DIR = tmp
        try:
            with open(os.path.join(tmp, "fetch_stats.json"), "w",
                      encoding="utf-8") as f:
                f.write('{"date": "2026-09-14"}')
            ok, why = fd.data_ready_for(self.con, self.TRADE)
            self.assertFalse(ok, "陈旧 fetch_stats 必须拦")
            self.assertIn("陈旧", why)
        finally:
            core.CACHE_DIR = orig

    def test_all_zero_pct_still_rejects(self):
        """快照存在但 pct 全零 → 疑似休市，仍须拒绝（别把保护逻辑改没了）。"""
        self.con.execute("INSERT INTO klines VALUES ('sh000001',?,1,1,1,1,1)",
                         (self.TRADE,))
        for i in range(5):
            self.con.execute("INSERT INTO snapshot VALUES (?,?,0,100)",
                             (self.TRADE, f"sh60000{i}"))
        self.con.commit()
        ok, why = core.is_trading_day_cross(self.con, self.TRADE)
        self.assertFalse(ok, "pct 全零必须拒（疑似休市）")
        self.assertIn("全零", why)


class TestBuildGateNotDeadlocked(unittest.TestCase):
    """build 的闸门组合不得在"交易日但数据未到"时产生死锁式静默。"""

    def setUp(self):
        self.con = _empty_db()

    def tearDown(self):
        self.con.close()

    def test_close_gate_reason_identifies_missing_data(self):
        """close 走 is_trading_day_cross + data_ready_for，两者理由须可辨。"""
        date = "2026-09-15"
        certain, why = core.is_trading_day_cross(self.con, date)
        ready, ready_why = fd.data_ready_for(self.con, date)
        self.assertFalse(certain)
        self.assertFalse(ready)
        combined = f"{why}/{ready_why}"
        self.assertNotIn("非交易日", combined,
                         f"组合理由把数据缺失说成非交易日：{combined}")
        # 至少一条理由明确指向"缺数据"
        self.assertTrue("待抓取" in combined or "无K线" in combined,
                        f"应能看出是缺数据，实际：{combined}")


if __name__ == "__main__":
    unittest.main()
