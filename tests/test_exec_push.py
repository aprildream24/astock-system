# -*- coding: utf-8 -*-
"""模拟盘推送与交易时段门控的回归锁（2026-09-18 第二轮，用户 5 条需求）。

用户原话：
  1. 「模拟盘推送的板式也需要参考前面的，现在板式非常混乱，到底买了什么，
     持有什么，完全不知道……买入前需要告诉我」
  2. 「无法买入的到达买点了也同样告诉我，比如资金不足等等」
  3. 「需要考虑周末和节假日，今天已经不在交易时间了又开始购买」
  4. 「测试所有功能无误后再推送」
  5. 「Pushplus 推送模拟盘信息是【模拟】【Astra】购入XX，竞价前是
     【竞价】【Astra】，依次类推，现在推送消息太多我根本分不清」

对应本套件的四组锁：
  A. 标题规范（#5）        —— `【{任务}】【{tag}】`，未知任务回退旧形态
  B. 时段门控（#3）        —— 周末/节假日/开盘前/午休/收盘后一律不建仓
  C. 未成交原因（#2）      —— **资金不足**必须被查出来并说清楚
  D. 版式与推送时机（#1/#4）—— 分组渲染 + 只在有实质动作时推送

纪律：全部内存库；`notifier.push` 一律 mock（测试绝不真发推送）；
      `notifier.DIST_LEDGER` 必须重定向到临时路径（否则会读仓库真实账本，
      这是本仓库最贵的假 FAIL，见 skill 的"测试自洽性"一节）。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pipeline.core as core              # noqa: E402
import pipeline.executor as executor      # noqa: E402
import pipeline.notifier as notifier      # noqa: E402
import pipeline.trade_calendar as tc      # noqa: E402

CST = timezone(timedelta(hours=8))
DATE = "2026-09-18"                       # 周五，交易日
SAT = "2026-09-19"                        # 周六
HOLIDAY = "2026-10-01"                    # 国庆，法定休市


def _at(date, hh, mm):
    """北京时间某日某刻。"""
    y, m, d = (int(x) for x in date.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=CST)


NOW = _at(DATE, 10, 0)                    # 盘中 10:00


def _mkcon():
    con = sqlite3.connect(":memory:")
    con.executescript(core._SCHEMA)
    return con


def _price(con, code, price, date=DATE, prev=None):
    con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, date, price, price, price, price, 1e6, 3e7, 0.0, 1.0))
    if prev:
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (code, "2026-09-17", prev, prev, prev, prev, 1e6, 3e7,
                     0.0, 1.0))
    con.commit()


def _plan(con, code, lo, hi, score=80.0, action="现在买", date=DATE):
    con.execute("INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (date, code, "票" + code[-2:], "趋势", action, lo, hi,
                 round(lo * 0.95, 2), None, None, score, "", None))
    con.commit()


class _Base(unittest.TestCase):
    """统一隔离：账本落临时目录（见文件头纪律）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._ledger = notifier.DIST_LEDGER
        notifier.DIST_LEDGER = os.path.join(self._td.name, "ledger.json")

    def tearDown(self):
        notifier.DIST_LEDGER = self._ledger
        self._td.cleanup()


# ---------------------------------------------------------------------------
# A. 标题规范（用户需求 5）
# ---------------------------------------------------------------------------
class TestTitleScheme(_Base):
    """★ 必须继承 `_Base`（账本重定向到临时目录）。

    血案（2026-09-18 本套件第一次运行就踩到）：本类调用的是**真实**
    `notifier.push`，`DIST_LEDGER` 未隔离 ⇒ 第一轮运行把 `<exec_auto,
    2026-09-18>` 写进了**仓库的 dist/push_ledger.json**；第二轮运行
    `_daily_sent` 立刻命中该条 ⇒ `push` 提前 return ⇒ 用例自爆。
    这就是 skill 里记的"按现实世界事件定时引爆的测试"：
    它**第一遍绿、第二遍红**，而且污染会随 push_ledger_sync 上云。
    """

    def test_known_modes_have_labels(self):
        """用户点名的两个形态必须逐字一致。"""
        self.assertEqual(notifier.title_prefix("exec_auto", "Astra"),
                         "【模拟】【Astra】")
        self.assertEqual(notifier.title_prefix("build_auction", "Astra"),
                         "【竞价】【Astra】")

    def test_all_scheduled_modes_labelled(self):
        """依次类推：每个会推送的任务都要有中文标签，不能有裸标题。"""
        want = {"build_pre": "盘前", "build_auction": "竞价",
                "build_close": "收盘", "narrative": "复盘",
                "watch_advice": "自选", "intraday_am": "盘中",
                "intraday_pm": "尾盘", "exec_auto": "模拟",
                "data_holiday": "休市"}
        for mode, label in want.items():
            self.assertEqual(notifier.title_prefix(mode, "Astra"),
                             f"【{label}】【Astra】", f"{mode} 标签不对")

    def test_data_blocked_is_warning(self):
        self.assertEqual(notifier.title_prefix("data_blocked_close", "Astra"),
                         "【告警】【Astra】")

    def test_unknown_mode_falls_back(self):
        """★ 未登记任务必须回退旧形态 —— 否则新加的任务会变裸标题。"""
        self.assertEqual(
            notifier.title_prefix("brand_new_mode", "Astra", "PushPlus"),
            "【Astra·PushPlus】")
        self.assertEqual(notifier.title_prefix(None, "Astra", "SC"),
                         "【Astra·SC】")

    def test_push_uses_new_title(self):
        """端到端：exec_auto 走 PushPlus 通道时标题应为 【模拟】【Astra】…"""
        con = _mkcon()
        orig_cfg, orig_pp = notifier.load_config, notifier._send_pushplus
        cap = {}
        try:
            notifier.load_config = lambda: {
                "push_dry_run": False, "push_tag": "Astra",
                "primary_channel": "pushplus", "pushplus_token": "T",
                "serverchan_key": "", "wxpusher_accounts": []}

            def fake_pp(token, title, content):
                cap["title"] = title
                return "sent", "ok"
            notifier._send_pushplus = fake_pp
            notifier.push("exec_auto", "建仓 1 只 · 持仓 2 只", "<p>x</p>",
                          date=DATE, con=con)
            self.assertTrue(cap["title"].startswith("【模拟】【Astra】"),
                            f"标题应为【模拟】【Astra】开头: {cap['title']}")
        finally:
            notifier.load_config, notifier._send_pushplus = orig_cfg, orig_pp
            con.close()


# ---------------------------------------------------------------------------
# B. 交易时段门控（用户需求 3）
# ---------------------------------------------------------------------------
class TestSessionGate(_Base):

    def _blocked(self, date, hh, mm):
        con = _mkcon()
        try:
            _price(con, "sh600001", 10.0)
            _plan(con, "sh600001", 9.8, 10.2)
            log = executor.auto_open(con, date, now=_at(date, hh, mm))
            n = con.execute(
                "SELECT COUNT(*) FROM position_batches").fetchone()[0]
            return log, n
        finally:
            con.close()

    def test_weekend_never_buys(self):
        log, n = self._blocked(SAT, 10, 0)
        self.assertEqual([a for _, a, _ in log], ["SESSION"])
        self.assertEqual(n, 0, "周六绝对不能建仓")
        self.assertIn("周末", log[0][2])

    def test_holiday_never_buys(self):
        log, n = self._blocked(HOLIDAY, 10, 0)
        self.assertEqual(n, 0, "国庆节绝对不能建仓")
        self.assertIn("节假日", log[0][2])

    def test_after_close_never_buys(self):
        """★ 用户实测现象：「今天已经不在交易时间了又开始购买」。

        close 班定时器是 15:22 —— 日期是交易日，但早已收盘。
        只看日期不看时刻就会在这里建仓，正是用户报的 bug。
        """
        log, n = self._blocked(DATE, 15, 22)
        self.assertEqual(n, 0, "收盘后不得建仓")
        self.assertIn("已收盘", log[0][2])

    def test_pre_market_never_buys(self):
        log, n = self._blocked(DATE, 8, 50)     # pre 班
        self.assertEqual(n, 0)
        self.assertIn("开盘前", log[0][2])

    def test_lunch_break_never_buys(self):
        log, n = self._blocked(DATE, 12, 0)
        self.assertEqual(n, 0)
        self.assertIn("午间休市", log[0][2])

    def test_utc_clock_is_converted(self):
        """★ CI runner 是 UTC：北京 15:22 == UTC 07:22。

        门控必须换算到 UTC+8 再判——若直接用 now()，CI 上会把"收盘后"
        当成"凌晨"，闸门形同虚设（这是最容易在本地测不出来的坑）。
        """
        utc_now = datetime(2026, 9, 18, 7, 22, tzinfo=timezone.utc)
        self.assertFalse(tc.in_trading_session(utc_now),
                         "北京 15:22 必须判为不可下单")
        # 同一个绝对时刻换成北京时间表示，结论必须一致
        self.assertFalse(tc.in_trading_session(
            utc_now.astimezone(CST)))
        # 北京 10:00 == UTC 02:00
        self.assertTrue(tc.in_trading_session(
            datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)))

    def test_session_boundaries(self):
        for hh, mm, ok in ((9, 14, False), (9, 15, True), (11, 30, True),
                           (11, 31, False), (12, 59, False), (13, 0, True),
                           (15, 0, True), (15, 1, False)):
            self.assertEqual(
                tc.in_trading_session(_at(DATE, hh, mm)), ok,
                f"{hh:02d}:{mm:02d} 判定错误")

    def test_in_session_actually_buys(self):
        """闸门不能把正常路径也挡了（反向红线）。"""
        con = _mkcon()
        try:
            _price(con, "sh600001", 10.0)
            _plan(con, "sh600001", 9.8, 10.2)
            log = executor.auto_open(con, DATE, now=_at(DATE, 10, 0))
            self.assertIn("BUY", [a for _, a, _ in log])
        finally:
            con.close()

    def test_session_gate_returns_reason(self):
        ok, why = executor.session_gate(DATE, now=_at(DATE, 16, 47))
        self.assertFalse(ok)
        self.assertIn("16:47", why, "原因里要带上时刻，读者才知道为什么没买")
        ok2, why2 = executor.session_gate(DATE, now=NOW)
        self.assertTrue(ok2)
        self.assertEqual(why2, "")


# ---------------------------------------------------------------------------
# C. 到价未成交必须说清楚（用户需求 2）
# ---------------------------------------------------------------------------
class TestBlockedBuyReasons(_Base):

    def test_insufficient_cash_is_checked(self):
        """★ 原实现**根本没有这道检查** ⇒ 现金能被买成负数。

        构造：持仓市值撑高净值到 10 万，现金只剩 1000 元。
        单笔 6 万在敞口(60%)与持仓数上都合法，只有现金不够。
        """
        con = _mkcon()
        try:
            executor.ensure_account(con, DATE)
            _price(con, "sh600002", 10.0)
            con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                        "available,strategy) VALUES('sh600002',?,9900,10.0,"
                        "9900,'sim')", ("2026-09-17",))
            con.execute("UPDATE account_state SET cash=1000 WHERE id=1")
            con.commit()
            self.assertAlmostEqual(executor.equity(con), 100000.0, delta=1)
            oid, st, why = executor.place_order(
                con, "sh600001", "buy", 6000, 10.0, DATE)
            self.assertEqual(st, "rejected", f"必须拒单，实得 {st}")
            self.assertIn("资金不足", why)
            self.assertIn("可用", why)
        finally:
            con.close()

    def test_cash_never_goes_negative(self):
        """拒单后现金不得变化（原实现会直接减成负数）。"""
        con = _mkcon()
        try:
            executor.ensure_account(con, DATE)
            _price(con, "sh600002", 10.0)
            con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                        "available,strategy) VALUES('sh600002',?,9900,10.0,"
                        "9900,'sim')", ("2026-09-17",))
            con.execute("UPDATE account_state SET cash=1000 WHERE id=1")
            con.commit()
            executor.place_order(con, "sh600001", "buy", 6000, 10.0, DATE)
            cash = con.execute(
                "SELECT cash FROM account_state").fetchone()[0]
            self.assertGreaterEqual(cash, 0, "现金被买成负数（原缺陷）")
        finally:
            con.close()

    def test_auto_open_reports_blocked_with_reason(self):
        """端到端：到价但买不进 → 推送里必须出现「资金不足」。"""
        con = _mkcon()
        try:
            executor.ensure_account(con, DATE)
            _price(con, "sh600002", 10.0)
            con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                        "available,strategy) VALUES('sh600002',?,9900,10.0,"
                        "9900,'sim')", ("2026-09-17",))
            con.execute("UPDATE account_state SET cash=1000 WHERE id=1")
            con.commit()
            _price(con, "sh600001", 10.0)
            _plan(con, "sh600001", 9.8, 10.2)
            with mock.patch.object(executor, "get_conn", return_value=con), \
                    mock.patch.object(executor, "today_str",
                                      return_value=DATE), \
                    mock.patch.object(notifier, "push",
                                      return_value={"status": "sent"}) as push:
                log = executor.run("auto", now=NOW)
            self.assertIn("REJECT", [a for _, a, _ in log])
            self.assertTrue(push.called, "到价未成交也必须通知用户")
            body = push.call_args[0][2]
            self.assertIn("到价未成交", body)
            self.assertIn("资金不足", body)
        finally:
            con.close()

    def test_all_reject_reasons_surface(self):
        """各类拒单原因都要能出现在推送里（资金不足只是一例）。"""
        con = _mkcon()
        try:
            log = [("sh600001", "REJECT",
                    "达到最大持仓数量（N06 集中度）"),
                   ("sh600002", "REJECT", "涨停价无法买入"),
                   ("sh600003", "REJECT", "资金不足：需 ¥17,005，可用 ¥1,000")]
            body = notifier.render_exec_report(
                DATE, {"equity": 100000, "cash": 1000, "init": 100000,
                       "n_hold": 4, "ret_pct": 0.0, "day_pct": 0.0},
                blocked=[{"code": c, "name": "票", "reason": r,
                          "buy_low": 9.0, "buy_high": 10.0, "price": 10.5}
                         for c, _a, r in log])
            for kw in ("资金不足", "涨停价无法买入", "最大持仓数量"):
                self.assertIn(kw, body, f"拒绝原因 {kw} 未出现在推送里")
        finally:
            con.close()


# ---------------------------------------------------------------------------
# D. 版式与推送时机（用户需求 1 / 4）
# ---------------------------------------------------------------------------
class TestExecLayout(_Base):

    def _acct(self):
        return {"equity": 101234, "cash": 40000, "init": 100000,
                "n_hold": 2, "ret_pct": 1.23, "day_pct": -0.45}

    def test_report_has_all_groups(self):
        body = notifier.render_exec_report(
            DATE, self._acct(),
            opened=[{"code": "sh600610", "name": "大众交通", "qty": 2300,
                     "price": 7.59, "amount": 17457.0, "action": "现在买",
                     "buy_low": 7.4, "buy_high": 7.7, "score": 82,
                     "sector": "半导体", "sector_pct": 3.2,
                     "sector_temp": "🔥强"}],
            blocked=[{"code": "sz002137", "name": "实益达", "buy_low": 8.5,
                      "buy_high": 8.9, "price": 8.67,
                      "reason": "资金不足：需 ¥17,340，可用 ¥1,000"}],
            holdings=[{"code": "sh603266", "name": "天龙股份", "qty": 1300,
                       "cost": 12.85, "price": 13.10, "pnl_pct": 1.95,
                       "days": 3, "status": "持有"}])
        for kw in ("模拟盘", "本次建仓", "当前持仓", "到价未成交",
                   "账户净值", "可用现金", "起步资金", "大众交通",
                   "天龙股份", "7.59", "12.85", "资金不足"):
            self.assertIn(kw, body, f"新版式缺少「{kw}」")
        # 板块标签必须带上（与主推送同源）
        self.assertIn("半导体", body)

    def test_sold_group_rendered(self):
        body = notifier.render_exec_report(
            DATE, self._acct(),
            sold=[{"code": "sh600001", "name": "票01", "action": "SELL",
                   "detail": "ATR保护线"},
                  {"code": "sh600002", "name": "票02",
                   "action": "RISK_BLOCKED",
                   "detail": "跌停价无法卖出：风险已触发，未成交（M27）"}])
        self.assertIn("本次卖出", body)
        self.assertIn("已卖出", body)
        self.assertIn("触发退出但未成交", body)
        self.assertIn("跌停价无法卖出", body)

    def test_no_placeholder_leaks(self):
        """缺数据时不得渲染出 None/空档位（干净降级）。"""
        body = notifier.render_exec_report(
            DATE, self._acct(),
            holdings=[{"code": "sh600001", "name": "票01", "qty": 100,
                       "cost": 10.0, "price": 10.0, "pnl_pct": 0.0,
                       "days": 0, "status": "持有"}])
        self.assertNotIn("None", body)
        self.assertNotIn("—%", body)

    def test_never_exceeds_pushplus_cap(self):
        """压力锁：30 只持仓 + 20 只未成交也不得超 PushPlus 上限。"""
        acct = self._acct()
        acct["n_hold"] = 30
        body = notifier.render_exec_report(
            DATE, acct,
            opened=[{"code": f"sh60001{i}", "name": f"票{i}", "qty": 1000,
                     "price": 10.0, "amount": 10000.0, "action": "现在买",
                     "buy_low": 9.8, "buy_high": 10.2, "score": 80}
                    for i in range(10)],
            blocked=[{"code": f"sz00000{i}", "name": f"未成交{i}",
                      "buy_low": 9.0, "buy_high": 10.0, "price": 10.5,
                      "reason": "资金不足：需 ¥17,005，可用 ¥1,000"}
                     for i in range(20)],
            holdings=[{"code": f"sh6001{i:02d}", "name": f"持仓{i}",
                       "qty": 1000, "cost": 10.0, "price": 10.5,
                       "pnl_pct": 5.0, "days": i, "status": "持有"}
                      for i in range(30)])
        self.assertLessEqual(len(body), notifier.PP_HTML_CAP,
                             "超长会被 PushPlus 硬截断（切在半张卡中间）")

    def test_quiet_run_does_not_push(self):
        """★ 只有 HOLD/SKIP/SESSION 时不推送 —— 否则每天 4 个 run 全是噪音。"""
        con = _mkcon()
        try:
            log = [("sh600001", "HOLD", "持仓收益 +1.0%"),
                   ("sh600002", "SKIP", "现价11.0不在买区9.8-10.2，不追"),
                   ("-", "SESSION", "北京时间 15:22，已收盘（15:00 后不再撮合），不建仓")]
            with mock.patch.object(notifier, "push") as push:
                r = executor._exec_push(con, "auto", DATE, log)
            self.assertIsNone(r)
            self.assertFalse(push.called, "无实质动作不得推送")
        finally:
            con.close()

    def test_reject_alone_triggers_push(self):
        con = _mkcon()
        try:
            _plan(con, "sh600001", 9.8, 10.2)
            log = [("sh600001", "REJECT", "资金不足：需 ¥17,005，可用 ¥1,000")]
            with mock.patch.object(notifier, "push",
                                   return_value={"status": "sent"}) as push:
                executor._exec_push(con, "auto", DATE, log)
            self.assertTrue(push.called)
            self.assertIn("未成交", push.call_args[0][1],
                          "标题摘要要带上未成交数")
        finally:
            con.close()

    def test_sell_forces_push_past_dedup(self):
        """退出风险必须 force（不能被普通去重吃掉）。"""
        con = _mkcon()
        try:
            log = [("sh600001", "RISK_BLOCKED", "跌停价无法卖出")]
            with mock.patch.object(notifier, "push",
                                   return_value={"status": "sent"}) as push:
                executor._exec_push(con, "auto", DATE, log)
            self.assertTrue(push.call_args[1].get("force"),
                            "风险退出必须 force=True")
        finally:
            con.close()

    def test_session_note_is_shown_not_pushed(self):
        """非交易时段的说明进正文注释，而不是单独成条消息。"""
        con = _mkcon()
        try:
            _plan(con, "sh600001", 9.8, 10.2)
            log = [("-", "SESSION", "北京时间 07:00，开盘前（09:15 才开始撮合），不建仓"),
                   ("sh600001", "BUY", "1000股@10.00（现在买）")]
            with mock.patch.object(notifier, "push",
                                   return_value={"status": "sent"}) as push:
                executor._exec_push(con, "auto", DATE, log)
            body = push.call_args[0][2]
            self.assertIn("开盘前", body, "时段说明要在正文里出现")
        finally:
            con.close()

    def test_once_per_day_opens_only_one_batch(self):
        """★ 每天最多一批：否则下午再买的那笔永远没人告诉用户
        （日级保险丝按 mode+date 只放行一条 exec_auto 推送）。"""
        con = _mkcon()
        try:
            for i in range(4):
                _price(con, f"sh60000{i}", 10.0)
                _plan(con, f"sh60000{i}", 9.8, 10.2)
            first = executor.auto_open(con, DATE, now=NOW)
            self.assertIn("BUY", [a for _, a, _ in first])
            n1 = con.execute(
                "SELECT COUNT(*) FROM position_batches").fetchone()[0]
            second = executor.auto_open(con, DATE, now=_at(DATE, 14, 40))
            self.assertNotIn("BUY", [a for _, a, _ in second],
                             "同一天不得开第二批仓")
            n2 = con.execute(
                "SELECT COUNT(*) FROM position_batches").fetchone()[0]
            self.assertEqual(n1, n2)
        finally:
            con.close()

    def test_no_fill_today_allows_later_attempt(self):
        """反向：当天**一笔都没成交**时不得封板 ——
        上午没票到价、下午到价了仍然应该买（否则等于漏单）。"""
        con = _mkcon()
        try:
            _price(con, "sh600001", 12.0)          # 高出买区
            _plan(con, "sh600001", 9.8, 10.2)
            executor.auto_open(con, DATE, now=_at(DATE, 9, 25))
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM position_batches").fetchone()[0], 0)
            _price(con, "sh600001", 10.0)          # 下午回落到买区
            log = executor.auto_open(con, DATE, now=_at(DATE, 14, 40))
            self.assertIn("BUY", [a for _, a, _ in log],
                          "上午没成交不得封板")
        finally:
            con.close()

    def test_holdings_rows_show_pnl(self):
        con = _mkcon()
        try:
            executor.ensure_account(con, DATE)
            _price(con, "sh600001", 11.0)
            con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                        "available,strategy) VALUES('sh600001',?,1000,10.0,"
                        "1000,'sim')", ("2026-09-15",))
            con.commit()
            rows = executor.holdings_rows(con, DATE)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertAlmostEqual(r["pnl_pct"], 10.0, places=1)
            self.assertEqual(r["days"], 3)
            self.assertEqual(r["qty"], 1000)
        finally:
            con.close()


# ---------------------------------------------------------------------------
# E. 接线（需求 3 的"到哪里去下单"）
# ---------------------------------------------------------------------------
class TestWorkflowWiring(_Base):

    def _stock(self):
        with open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                  encoding="utf-8") as f:
            return f.read()

    def test_executor_runs_on_tradeable_slots(self):
        """可下单时段是 auction(09:25) 与 intraday(09:45/14:40)——
        步骤条件里必须包含它们，否则永远没有"能买"的时刻。"""
        src = self._stock()
        i = src.find("模拟盘自动运行")
        self.assertGreater(i, 0)
        blk = src[i:i + 2600]
        for t in ("'close'", "'auction'", "'intraday'"):
            self.assertIn(t, blk, f"executor 步骤条件缺少 {t}")

    def test_intraday_slot_is_passed(self):
        """盘中必须传 --slot，否则会拿盘前快照价当盘中价成交。"""
        src = self._stock()
        i = src.find("模拟盘自动运行")
        blk = src[i:i + 2600]
        self.assertIn("--slot", blk)
        self.assertIn("github.event.inputs.slot", blk)

    def test_cli_has_dry_run(self):
        """用户需求 4「测试所有功能无误后再推送」—— CLI 必须有不推送的口子。"""
        with open(os.path.join(ROOT, "tools", "executor.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"--dry"', src)
        self.assertIn("dry=True", src)
        self.assertIn('"--slot"', src)


# ---------------------------------------------------------------------------
# F. 结构锁：本文件所有用例类都必须隔离账本
# ---------------------------------------------------------------------------
class TestLedgerIsolationDiscipline(_Base):
    """★ 防止再犯"测试写脏仓库账本 → 第二轮自爆"的血案。

    只靠自觉不够：`notifier._daily_sent()` 无条件读 `DIST_LEDGER`，而 CI 的
    `actions/checkout` 会把仓库账本拉进工作区 ⇒ 任何没隔离的用例都会
    "第一遍绿、第二遍红"，且红得毫无线索。这里用**结构断言**把它焊死。
    """

    def test_every_case_class_inherits_isolated_base(self):
        with open(os.path.join(ROOT, "tests", "test_exec_push.py"),
                  encoding="utf-8") as f:
            src = f.read()
        import re as _re
        bases = _re.findall(r"^class\s+(\w+)\(([^)]*)\):", src, _re.M)
        self.assertTrue(bases, "没解析到任何用例类，断言本身失效了")
        for name, base in bases:
            if name == "_Base":
                continue
            self.assertIn("_Base", base,
                          f"{name} 未继承 _Base ⇒ 会写脏仓库账本（血案重演）")

    def test_isolated_base_redirects_ledger(self):
        """_Base 必须真的重定向 DIST_LEDGER —— 只继承不重定向等于没隔离。"""
        with open(os.path.join(ROOT, "tests", "test_exec_push.py"),
                  encoding="utf-8") as f:
            src = f.read()
        i = src.find("class _Base")
        self.assertGreater(i, 0, "找不到 _Base")
        blk = src[i:i + 700]
        self.assertIn("notifier.DIST_LEDGER", blk,
                      "_Base 未重定向 DIST_LEDGER（血案会重演）")


if __name__ == "__main__":
    unittest.main(verbosity=1)
