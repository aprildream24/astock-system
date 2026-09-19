# -*- coding: utf-8 -*-
"""模拟盘执行器（审计 八~十 全面落地）。

RiskGate 参数基线（9.1【待验证 V05】——保留原值，非通用标准）：
  单票仓位硬上限 70% / 最大持仓 4 只 / 单日最大委托 6 笔 /
  单笔金额 1000~60000 元 / 日内组合亏损熔断 -3% / 初始模拟资金 100000 元。

纪律：
- M21 加仓按累计敞口检查（不是单笔）；
- M22 日内亏损基准=日初净值（现金流调整），熔断当日锁定不复位；
- M23 买入频率限制不得阻止必要退出（卖出不计额度）；
- M24 止损规则冲突：收集全部触发原因，固定优先级定动作；
- M25 日涨幅≠持仓收益（+5% 日涨幅不自动标止盈，按持仓盈亏判）；
- M26 T+1 按批次管理：昨日批次可卖、今日批次不可卖，同票共存；
- M27 风险触发≠实际成交：跌停卖不出记录"已触发未成交"；
- N06 下单前检查：涨跌停/资金/可卖数量/重复订单/金额范围/集中度；
- N07 ATR 保护线只允许收紧（max），滚动窗口变化不放松。
分账（M32）：决策(signals) / 委托(orders) / 成交(fills) /
持仓批次(position_batches) / 资金流水(cashflow) / 账户(account_state)。
净值 = 现金余额 + 持仓市值（可对账）。
"""
import json
import os
import uuid
from datetime import datetime

from . import core
from . import trade_calendar as tc
from .core import get_conn, today_str, trade_calendar

RISK = {"max_pos_pct": 0.70, "max_holdings": 4, "max_daily_orders": 6,
        "min_order_amt": 1000.0, "max_order_amt": 60000.0,
        "daily_loss_halt": -0.03, "init_cash": 100000.0}
# 分仓计划（用户 2026-09-21：「模拟盘购买要分成 3322 或者 3331」）。
# 第 N 笔建仓占用第 N 档仓位比例（按净值计），允许 config/sim.json
# {"position_plan": "3331"} 切换；缺省 3322。
POSITION_PLANS = {"3322": [0.30, 0.30, 0.20, 0.20],
                  "3331": [0.30, 0.30, 0.30, 0.10]}


def _position_plan():
    key = "3322"
    p = os.path.join(core.CONFIG_DIR, "sim.json")
    if os.path.exists(p):
        try:
            key = json.load(open(p, encoding="utf-8")).get(
                "position_plan", key)
        except Exception:  # noqa: BLE001
            pass
    return POSITION_PLANS.get(key, POSITION_PLANS["3322"])


FEE_RATE = 0.0003            # 佣金近似；卖出另计印花税 0.0005
STAMP_TAX = 0.0005

# 止损规则优先级（M24）：数字越小优先级越高
RULES = [
    ("hard_stop", "普通硬止损", lambda pnl, low, protect, ma20: pnl <= -3.0),
    ("swim_stop", "趋势/波段止损", lambda pnl, low, protect, ma20: pnl <= -6.0),
    ("atr_protect", "ATR保护线", lambda pnl, low, protect, ma20:
     protect is not None and low <= protect),
    ("ma20_break", "MA20破位", lambda pnl, low, protect, ma20:
     ma20 is not None and low < ma20 and pnl < 0),
]
TAKE_PROFIT_PNL = 15.0       # 持仓浮盈 ≥15% → 止盈（按持仓收益，M25）

# 「实质动作」= 值得单独发一条推送的判决（用户 2026-09-18：推送太多分不清）。
# 对照：SESSION（非交易时段）/ SKIP（未到买点）/ HOLD（持有）都不触发推送。
ACTIONABLE = ("BUY", "SELL", "REJECT", "RISK_BLOCKED", "RISK_FLAGGED")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def ensure_account(con, today):
    """账户初始化 + 日切：日初净值按现金流调整（M22）。"""
    row = con.execute("SELECT cash, day_start_equity, day_key, frozen "
                      "FROM account_state WHERE id=1").fetchone()
    if row is None:
        con.execute("INSERT INTO account_state VALUES(1,?,?,?,0)",
                    (RISK["init_cash"], RISK["init_cash"], today))
        con.execute("INSERT INTO cashflow VALUES(?,?,?,?,?)",
                    (_now(), "init", RISK["init_cash"], RISK["init_cash"],
                     "初始模拟资金"))
        con.commit()
        return con.execute("SELECT cash, day_start_equity, day_key, frozen "
                           "FROM account_state WHERE id=1").fetchone()
    cash, day_start, day_key, frozen = row
    if day_key != today:
        # 日切：熔断锁定自动复位（跨日），日初净值=昨日收盘净值（现金流入日切已含）
        eq = equity(con)
        con.execute("UPDATE account_state SET day_start_equity=?, day_key=?, "
                    "frozen=0 WHERE id=1", (eq, today))
        # M26 日切：昨日及更早批次解锁可卖
        con.execute("UPDATE position_batches SET available=qty WHERE buy_date<?",
                    (today,))
        con.commit()
    return con.execute("SELECT cash, day_start_equity, day_key, frozen "
                       "FROM account_state WHERE id=1").fetchone()


def equity(con, price_of=None):
    """净值 = 现金余额 + 持仓市值（M32 可对账表达）。"""
    cash = con.execute("SELECT cash FROM account_state WHERE id=1").fetchone()[0]
    mv = 0.0
    for code, qty in con.execute(
            "SELECT code, SUM(qty) FROM position_batches GROUP BY code"):
        p = price_of(code) if price_of else None
        if p is None:
            row = con.execute(
                "SELECT c FROM klines WHERE code=? ORDER BY date DESC LIMIT 1",
                (code,)).fetchone()
            p = row[0] if row else 0.0
        mv += qty * p
    return cash + mv


def day_pnl_pct(con, today):
    row = ensure_account(con, today)
    _, day_start, _, _ = row
    if not day_start:
        return 0.0
    return (equity(con) / day_start - 1) * 100


def available_qty(con, code, today):
    """M26 T+1 按批次：仅 buy_date < today 的批次可卖。"""
    row = con.execute(
        "SELECT COALESCE(SUM(available),0) FROM position_batches "
        "WHERE code=? AND buy_date<?", (code, today)).fetchone()
    return row[0] or 0.0


def _limit_prices(prev_close, limit=0.10):
    return prev_close * (1 - limit), prev_close * (1 + limit)


def place_order(con, code, side, qty, price, today, prev_close=None,
                risk_sell=False, reason=""):
    """N06 下单前检查 → orders 落账 → 即时模拟撮合（fills/现金流/持仓）。

    risk_sell=True 的退出订单不受买入频率额度与熔断冻结限制（M23）。
    返回 (order_id, status, detail)。status ∈ filled/rejected/pending。"""
    oid = uuid.uuid4().hex[:12]
    ts = _now()

    def reject(why):
        con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
                    (oid, ts, code, side, qty, price, "rejected", why))
        con.commit()
        return oid, "rejected", why

    # N06 检查（卖出风险单跳过熔断与频率闸）
    if side == "buy":
        acct = ensure_account(con, today)
        frozen = acct[3]
        if frozen and not risk_sell:
            return reject("日内亏损熔断锁定，不开新仓（M22）")
        buys_today = con.execute(
            "SELECT COUNT(*) FROM orders WHERE side='buy' AND status!='rejected' "
            "AND ts LIKE ?", (today + "%",)).fetchone()[0]
        if buys_today >= RISK["max_daily_orders"] and not risk_sell:
            return reject("达到单日最大买入委托数（M23：不影响卖出）")
    amt = qty * price
    if amt < RISK["min_order_amt"]:
        return reject(f"单笔金额 {amt:.0f} 低于下限 {RISK['min_order_amt']:.0f}")
    if amt > RISK["max_order_amt"]:
        return reject(f"单笔金额 {amt:.0f} 超上限 {RISK['max_order_amt']:.0f}")
    if prev_close:
        low_l, up_l = _limit_prices(prev_close)
        if side == "sell" and price <= low_l * 1.001:
            return reject("跌停价无法卖出：风险已触发，未成交（M27）")
        if side == "buy" and price >= up_l * 0.999:
            return reject("涨停价无法买入")
    if side == "sell":
        avail = available_qty(con, code, today)
        if qty > avail:
            return reject(f"T+1 可卖数量不足：请求 {qty}，可卖 {avail}（M26 按批次）")
    if side == "buy":
        n_hold = con.execute(
            "SELECT COUNT(DISTINCT code) FROM position_batches").fetchone()[0]
        if code not in [r[0] for r in con.execute(
                "SELECT DISTINCT code FROM position_batches")] \
                and n_hold >= RISK["max_holdings"]:
            return reject("达到最大持仓数量（N06 集中度）")
        # M21 加仓按累计敞口
        eq = equity(con)
        cur = con.execute("SELECT COALESCE(SUM(qty*cost),0) FROM position_batches "
                          "WHERE code=?", (code,)).fetchone()[0]
        if (cur + amt) / eq > RISK["max_pos_pct"]:
            return reject(f"单票累计敞口超 {RISK['max_pos_pct']:.0%}（M21）")
        # ★ 2026-09-18 补：**资金充足性**。原实现查了金额上下限、持仓数、
        # 集中度，**唯独没查现金够不够** ⇒ `cash -= (amt+fee)` 会把余额买成
        # 负数。这不是理论风险：auto_open 的单票预算 = 净值×min(70%/4, 25%)
        # ≈ 17.5%，上限 6 万；4 只满仓理论上要 24 万，而初始资金只有 10 万
        # ⇒ 走到第 3、4 只时必然触达（用户原话："比如资金不足等等"）。
        need = amt * (1 + FEE_RATE)
        cash_avail = ensure_account(con, today)[0]
        if need > cash_avail:
            return reject(f"资金不足：需 ¥{need:,.0f}，可用 ¥{cash_avail:,.0f}")
        if con.execute("SELECT 1 FROM orders WHERE code=? AND side='buy' "
                       "AND status='pending'", (code,)).fetchone():
            return reject("存在同标的未成交买单（N06 重复订单）")
    # 落账并模拟成交
    fee = amt * FEE_RATE + (amt * STAMP_TAX if side == "sell" else 0.0)
    con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
                (oid, ts, code, side, qty, price, "filled", reason))
    con.execute("INSERT INTO fills VALUES(NULL,?,?,?,?,?,?,?)",
                (oid, ts, code, side, qty, price, round(fee, 2)))
    acct = ensure_account(con, today)
    cash = acct[0]
    if side == "buy":
        cash -= (amt + fee)
        con.execute("INSERT INTO position_batches(code,buy_date,qty,cost,"
                    "available,strategy) VALUES(?,?,?,?,0,'sim')",
                    (code, today, qty, price))
        con.execute("UPDATE position_batches SET available=qty WHERE code=? "
                    "AND buy_date<?", (code, today))
    else:
        cash += (amt - fee)
        remain = qty
        for bid, bqty in con.execute(
                "SELECT batch_id, available FROM position_batches "
                "WHERE code=? AND buy_date<? ORDER BY buy_date",
                (code, today)).fetchall():
            take = min(remain, bqty)
            con.execute("UPDATE position_batches SET available=available-? "
                        "WHERE batch_id=?", (take, bid))
            remain -= take
            if remain <= 0:
                break
        # 仅清理"已解锁且卖光"的批次；当日新批次 available=0 但明日解锁
        con.execute("DELETE FROM position_batches WHERE available<=0 "
                    "AND buy_date<?", (today,))
    con.execute("UPDATE account_state SET cash=? WHERE id=1", (round(cash, 2),))
    con.execute("INSERT INTO cashflow VALUES(?,?,?,?,?)",
                (ts, side, (-amt - fee) if side == "buy" else (amt - fee),
                 round(cash, 2), code))
    con.commit()
    return oid, "filled", reason or "模拟成交"


def evaluate_exit(con, code, today, protect_prev=None, cost_override=None):
    """M24：收集全部触发规则 → 固定优先级定动作；M25 按持仓收益。

    返回 (action, reasons, detail)。action ∈ SELL/HOLD。
    N07：ATR 保护线只收紧——调用方传入上一日保护线，取 max。

    ★ 2026-09-18 新增 `cost_override`（真实持仓体检用）：成本原本只从
    `position_batches`（模拟盘）取。真实持仓（config/holdings.json）**不在**
    那张表里 ⇒ cost 为 None ⇒ pnl 恒 0.0% ⇒ **所有按收益率的规则（-3%/-6%/
    +15%）全部失效**，只剩与收益无关的 ATR/MA20 会触发。
    实测表现：对用户真实持仓返回「持仓收益 +0.0%」——这个数字是假的，
    拿它当"没亏"会把已经亏钱的持仓判成完好。
    ⇒ 真实持仓必须把 `buy_price` 传进来，口径才成立。
    默认 None = 完全保持原行为（模拟盘不受影响）。"""
    row = con.execute(
        "SELECT c FROM klines WHERE code=? ORDER BY date DESC LIMIT 1",
        (code,)).fetchone()
    if not row:
        return "HOLD", [], "无行情"
    close = row[0]
    bars = con.execute(
        "SELECT h, l, c FROM klines WHERE code=? ORDER BY date DESC LIMIT 11",
        (code,)).fetchall()
    low_today = bars[0][1]
    trs = [max(bars[i][0] - bars[i][1],
               abs(bars[i][0] - bars[i + 1][2]),
               abs(bars[i][1] - bars[i + 1][2]))
           for i in range(len(bars) - 1)]
    atr = (sum(trs) / len(trs)) if trs else 0.0
    hh10 = max(b[0] for b in bars[:10])
    protect = hh10 - 2 * atr
    if protect_prev is not None:          # N07 只收紧
        protect = max(protect, protect_prev)
    ma20_row = con.execute(
        "SELECT AVG(c) FROM (SELECT c FROM klines WHERE code=? "
        "ORDER BY date DESC LIMIT 20)", (code,)).fetchone()
    ma20 = ma20_row[0] if ma20_row else None
    cost = con.execute(
        "SELECT AVG(cost) FROM position_batches WHERE code=?", (code,)).fetchone()[0]
    if not cost and cost_override:
        cost = cost_override                           # 真实持仓外部成本
    pnl = (close / cost - 1) * 100 if cost else 0.0    # 持仓收益（M25）
    triggered = [(rid, name) for rid, name, cond in RULES
                 if cond(pnl, low_today, protect, ma20)]
    if pnl >= TAKE_PROFIT_PNL:
        triggered.append(("take_profit", "持仓浮盈止盈"))
    if not triggered:
        return "HOLD", [], f"持仓收益 {pnl:+.1f}%，保护线 {protect:.2f}"
    priority = {r[0]: i for i, r in enumerate(RULES)}
    triggered.sort(key=lambda t: priority.get(t[0], 99))
    return "SELL", [n for _, n in triggered], f"持仓收益 {pnl:+.1f}%"


def _sector_hot(con, date, industry):
    """板块是否处于当日主力净流入第一梯队（前 20）。"""
    rows = con.execute(
        "SELECT sector, net_yi FROM sector_heat WHERE date=? "
        "ORDER BY net_yi DESC", (date,)).fetchall()
    if not rows:
        return False
    return industry in {r[0] for r in rows[:20]}


def hold_limit_for(con, code, buy_date, default=20):
    """该持仓的持有周期上限（交易日）：优先取买入日前后 ±4 天内系统推荐的
    hold_days（快箱体 8 / N字二波 12 / 常规 20），找不到退回默认 20。"""
    try:
        rows = con.execute(
            "SELECT date FROM rec_picks WHERE code=? "
            "AND date BETWEEN date(?, '-4 day') AND date(?, '+4 day')",
            (code, buy_date, buy_date)).fetchall()
    except Exception:  # noqa: BLE001
        return default
    for (d,) in rows:
        extra = con.execute(
            "SELECT extra FROM candidate_snapshots WHERE code=? AND date=?",
            (code, d)).fetchone()
        if extra and extra[0]:
            try:
                hd = json.loads(extra[0]).get("hold_days")
                if hd:
                    return int(hd)
            except Exception:  # noqa: BLE001
                continue
    return default


def evaluate_real_holdings(con, date, holdings):
    """★ 用户需求（2026-09-18）：真实持仓体检 + 换股依据。

    与模拟盘 `position_batches` 完全隔离——这里是用户**实盘**自建仓，不在那张
    表里，所以成本必须靠 `buy_price` 外部传入（evaluate_exit 的 cost_override）。

    对每只持仓：外部成本跑退出裁决（ATR/MA20/-3%/-6%/+15%）→ 取四态买区/止损
    → 板块热度 → 产出结构化体检。单只数据缺失 → 标「数据不足」，不阻断整份。

    返回 list[dict]，字段：code,name,buy_price,shares,buy_date,close,pnl_pct,
    verdict,exit_action,exit_reasons,stop,zone,state,sector,sector_hot,tradable。
    verdict 颜色语义：SELL→红（建议减仓/离场）、含「止损/警惕」→黄、其余→蓝（持有观察）。"""
    from . import engines
    from . import mktfilter
    out = []
    for h in holdings or []:
        code = h.get("code")
        if not code:
            continue
        num = code[2:] if code[:2] in ("sh", "sz") else code
        bp = h.get("buy_price")
        item = {"code": code, "name": h.get("name") or _name_of(con, code),
                "buy_price": bp, "shares": h.get("shares"),
                "buy_date": h.get("buy_date"),
                "close": None, "pnl_pct": None, "verdict": "数据不足",
                "exit_action": None, "exit_reasons": [], "stop": None,
                "zone": None, "state": None, "sector": None,
                "sector_hot": False,
                "tradable": mktfilter.tradable(num)}
        out.append(item)
        row = con.execute(
            "SELECT c FROM klines WHERE code=? AND date<=? "
            "ORDER BY date DESC LIMIT 1", (code, date)).fetchone()
        if not row or not row[0]:
            continue
        close = float(row[0])
        item["close"] = close
        if bp:
            item["pnl_pct"] = round((close / bp - 1) * 100, 2)
        act, reasons, _ = evaluate_exit(con, code, date, cost_override=bp)
        item["exit_action"] = act
        item["exit_reasons"] = reasons
        # 四态买区 / 止损
        krows = con.execute(
            "SELECT date,o,c,h,l,v FROM klines WHERE code=? AND date<=? "
            "ORDER BY date DESC LIMIT 60", (code, date)).fetchall()
        krows = [[d, o, c, h, l, v] for d, o, c, h, l, v in reversed(krows)]
        if len(krows) >= 5:
            plan = engines.entry_plan(krows)
            item["stop"] = round(plan["stop"], 2)
            item["zone"] = [round(plan["pull_zone"][0], 2),
                            round(plan["pull_zone"][1], 2)]
            item["state"] = plan["state"]
        # ★ 去弱留强·持续弱量化（用户 2026-09-21：「如果一直持续弱，我拿着
        # 也没什么意义」）：近 10 个交易日里收盘低于 MA20 的天数 + 近 10 日
        # 跌幅。两者同时恶化 → 明确给「换股」建议（去弱留强）。
        if len(krows) >= 21:
            closes = [r[2] for r in krows]
            ma20 = [sum(closes[i - 19:i + 1]) / 20
                    for i in range(19, len(closes))]
            tail_c, tail_m = closes[-10:], ma20[-10:]
            weak_days = sum(1 for c, m in zip(tail_c, tail_m) if c < m)
            drop10 = (closes[-1] / closes[-11] - 1) * 100                 if len(closes) >= 11 else 0.0
            item["weak_days"] = weak_days
            item["drop10"] = round(drop10, 2)
            if weak_days >= 7 and drop10 <= -5:
                item["swap_hint"] = (
                    f"持续走弱：10 日中 {weak_days} 日收在 MA20 下方，"
                    f"近 10 日 {drop10:+.1f}%——建议换股（去弱留强）")
                item["verdict"] = "建议换股"
                if act != "SELL":
                    item["exit_reasons"] = (item["exit_reasons"] or []) +                         [f"持续走弱（{weak_days}/10 日 < MA20）"]
        # 板块热度
        ind = con.execute(
            "SELECT sector FROM stock_industry WHERE code=?", (code,)).fetchone()
        if ind and ind[0]:
            item["sector"] = ind[0]
            item["sector_hot"] = _sector_hot(con, date, ind[0])
        # ★ 持有生命周期（用户 2026-09-21：「告诉我买入后持有几天」）：
        # hold_days = 已持有交易日数；hold_limit = 推荐携带的周期上限
        # （快箱体 8 / N字二波 12 / 常规 20）；phase = 持有中/接近到期/已到期。
        from .trade_calendar import is_trade_day as _itd
        bd = h.get("buy_date")
        if bd and _itd(bd):
            days = trade_calendar(con)
            try:
                i0, i1 = days.index(bd), -1
                later = [d for d in days if d >= date]
                if later:
                    i1 = days.index(later[0])
                hd = max(0, i1 - i0)
                limit = hold_limit_for(con, code, bd)
                item["hold_days"] = hd
                item["hold_limit"] = limit
                item["phase"] = ("已到期" if hd >= limit else
                                 "接近到期" if hd >= limit * 0.8 else "持有中")
            except ValueError:  # noqa: BLE001 — buy_date 不在日历（停牌穿越等）
                pass
        # ★ 持仓板块退潮预警（同日买入就暴跌的预防针）
        if item.get("sector"):
            try:
                from .sector import retreat_signal
                ret = retreat_signal(con, date, item["sector"])
                if ret and ret.get("retreat"):
                    item["sector_retreat"] = ret["detail"]
            except Exception:  # noqa: BLE001
                pass
        # 裁决文本
        if act == "SELL":
            item["verdict"] = ("建议减仓/离场" if (item["pnl_pct"] is not None
                                                  and item["pnl_pct"] < 0)
                               else "触发退出")
        else:
            if item["stop"] and close <= item["stop"]:
                item["verdict"] = "已破止损·警惕"
                item["exit_action"] = "SELL"
                item["exit_reasons"] = (item["exit_reasons"] or []) + \
                    ["现价跌破止损线"]
            else:
                item["verdict"] = "持有观察"
        # 生命周期/板块退潮 → 升级裁决与建议（放最后，优先级低于 SELL）：
        #   · 已到期：红/黄提示按纪律了结（弱市=红）；
        #   · 接近到期：黄灯预告，让用户有心理准备；
        #   · 板块退潮：黄灯预警补跌风险（去弱留强的前置信号）。
        # 生命周期/退潮原因**无条件追加**（SELL 行同样要显示到期/退潮提示）；
        # verdict 升级仅在未触发 SELL 时（SELL 本身已是最高级处置）。
        phase = item.get("phase")
        pnl = item.get("pnl_pct")
        if phase == "已到期":
            item["exit_reasons"] = (item["exit_reasons"] or []) +                 [f"持有周期已到（{item.get('hold_days')}/"
                 f"{item.get('hold_limit')} 个交易日），按纪律了结"]
            if act != "SELL":
                item["verdict"] = ("到期·建议了结" if (pnl is not None
                                                     and pnl < 0)
                                   else "到期·兑现观察")
        elif phase == "接近到期":
            item["exit_reasons"] = (item["exit_reasons"] or []) +                 [f"接近持有周期上限（{item.get('hold_days')}/"
                 f"{item.get('hold_limit')} 日）"]
            if act != "SELL" and item["verdict"] == "持有观察":
                item["verdict"] = "接近到期·警惕"
        if item.get("sector_retreat"):
            item["exit_reasons"] = (item["exit_reasons"] or []) +                 [f"板块退潮：{item['sector_retreat']}（注意补跌）"]
            if act != "SELL" and item["verdict"] == "持有观察":
                item["verdict"] = "板块退潮·警惕"
    return out


def _last_price(con, code, date, slot=None):
    """当日可用价格。

    优先级（★ 顺序不能乱，否则盘中会拿盘前价当实时价）：
      ① `snapshot_live[date, slot]` —— 盘中任务**真实抓到的那一刻**的价格。
         `slot` 只在 intraday 任务里传（am/pm），是唯一"当下价"来源。
      ② `snapshot[date]` —— 盘前/竞价抓的快照（09:25 是撮合价）。收盘任务
         在收盘抓取后也写这里，所以 close 班拿到的是当日收盘价。
      ③ 最近一根K线 —— 兜底（停牌/新股）。
    """
    if slot:
        row = con.execute(
            "SELECT price FROM snapshot_live WHERE date=? AND slot=? AND code=?",
            (date, slot, code)).fetchone()
        if row and row[0]:
            return float(row[0])
    row = con.execute("SELECT price FROM snapshot WHERE date=? AND code=?",
                      (date, code)).fetchone()
    if row and row[0]:
        return float(row[0])
    row = con.execute(
        "SELECT c FROM klines WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
        (code, date)).fetchone()
    return float(row[0]) if row and row[0] else None


def account_line(con, today):
    """账户概览行（纯文本，供日志/测试复用）：净值/现金/持仓/累计。"""
    acct = ensure_account(con, today)
    eq = equity(con)
    ret = (eq / RISK["init_cash"] - 1) * 100
    n = con.execute(
        "SELECT COUNT(DISTINCT code) FROM position_batches").fetchone()[0]
    return (f"账户 ¥{eq:,.0f}（现金 ¥{acct[0]:,.0f} · 持仓 {n} 只 · "
            f"累计 {ret:+.2f}% ｜ 起步 ¥{RISK['init_cash']:,.0f}）")


def account_snapshot(con, today):
    """账户结构化快照（卡片渲染用）。与 account_line 同源同口径。"""
    acct = ensure_account(con, today)
    eq = equity(con)
    return {
        "equity": eq, "cash": acct[0], "init": RISK["init_cash"],
        "n_hold": con.execute(
            "SELECT COUNT(DISTINCT code) FROM position_batches").fetchone()[0],
        "ret_pct": (eq / RISK["init_cash"] - 1) * 100,
        "day_pct": day_pnl_pct(con, today),
        "frozen": bool(acct[3]),
    }


def report_daily_pnl(con, today, slot=None):
    """★ 用户需求①：模拟盘每日盈亏总结 + 亏因归因。

    返回 dict：净值/现金/累计、当日盈亏额与%、持仓逐只当日涨跌幅、
    与所属板块/沪指对比、归因结论（选股弱 / 板块弱 / 系统性）。

    亏因归因逻辑（用户原话「亏了要给我亏的理由，是板块没选对还是
    个股没选对」）：逐只比 个股当日涨跌幅 vs 所属板块涨跌幅 vs 沪指；
    个股弱于板块 → 选股层面问题；板块弱于大盘 → 板块选择偏弱；
    整体随大盘走弱且无个股/板块明显失误 → 系统性波动。"""
    from .engines import pct
    a = ensure_account(con, today)
    # account_snapshot 不返回 day_start，这里从 ensure_account 元组直接取
    # day_start_equity（日初净值，日切时由上一日收盘净值写入）。
    day_start_eq = a[1] if a[1] else RISK["init_cash"]
    acct = account_snapshot(con, today)
    day_pct = day_pnl_pct(con, today)
    day_amt = acct["equity"] - day_start_eq

    # 沪指当日涨跌幅
    idx = con.execute(
        "SELECT c FROM klines WHERE code='sh000001' AND date<=? "
        "ORDER BY date DESC LIMIT 2", (today,)).fetchall()
    idx_chg = (pct(idx[0][0] - idx[1][0], idx[1][0])
               if len(idx) == 2 and idx[1][0] else None)

    # 板块当日涨跌幅（sector_heat.pct）
    sec_chg = {r[0]: r[1] for r in con.execute(
        "SELECT sector, pct FROM sector_heat WHERE date=?", (today,)).fetchall()}

    rows = holdings_rows(con, today, slot)
    stock_weak = sector_weak = 0
    for h in rows:
        code = h["code"]
        pr = con.execute(
            "SELECT c FROM klines WHERE code=? AND date<=? "
            "ORDER BY date DESC LIMIT 2", (code, today)).fetchall()
        schg = (pct(pr[0][0] - pr[1][0], pr[1][0])
                if len(pr) == 2 and pr[1][0] else None)
        ind = con.execute(
            "SELECT sector FROM stock_industry WHERE code=?", (code,)).fetchone()
        sec = ind[0] if ind else None
        sechg = sec_chg.get(sec) if sec else None
        h["day_chg"] = round(schg, 2) if schg is not None else None
        h["sector"] = sec
        h["sector_chg"] = round(sechg, 2) if sechg is not None else None
        if schg is not None and sechg is not None:
            if schg < sechg - 0.5:
                h["weak"] = "个股弱于板块"; stock_weak += 1
            elif sechg < (idx_chg or 0) - 0.5:
                h["weak"] = "板块弱于大盘"; sector_weak += 1
            else:
                h["weak"] = ""
        else:
            h["weak"] = ""
    reasons = []
    if day_amt < 0:
        if idx_chg is not None and day_pct < idx_chg - 0.3:
            reasons.append(f"跑输大盘（组合 {day_pct:+.2f}% vs 沪指 "
                           f"{idx_chg:+.2f}%）")
        if stock_weak:
            reasons.append(f"{stock_weak} 只个股当日弱于所属板块 —— "
                           f"选股层面需优化（挑了板块里偏弱的标的）")
        if sector_weak:
            reasons.append(f"{sector_weak} 只所属板块弱于大盘 —— "
                           f"板块选择偏弱")
        if not reasons:
            reasons.append("持仓整体随大盘/板块走弱，属系统性波动，"
                          f"非个股或板块明显失误（沪指 {idx_chg:+.2f}%）")
    else:
        reasons.append("当日盈利，保持节奏")
    return {"equity": acct["equity"], "cash": acct["cash"], "init": acct["init"],
            "day_pct": day_pct, "day_amt": day_amt, "ret_pct": acct["ret_pct"],
            "n_hold": acct["n_hold"], "rows": rows, "idx_chg": idx_chg,
            "stock_weak": stock_weak, "sector_weak": sector_weak,
            "reasons": reasons}


def report_period(con, today, days=30):
    """★ 用户需求①：半月/月度复盘汇总（盈利最大化 + 系统改进建议）。

    聚合 `days` 窗口内的：
      · 已实现盈亏（FIFO 匹配买/卖 fill，仅窗口内的平仓计入本期）
      · 未实现盈亏（当前持仓按市价 mark-to-market）
      · 平仓胜率（窗口内卖出事件，盈利笔数/总平仓笔数）
      · 最佳/最差板块（按净盈亏聚合到行业）
    并据上述指标生成「盈利最大化」与「系统改进建议」文本。

    设计取舍：历史买仓用**全部** fills 构建 FIFO 队列（保证窗口内卖出的成本
    基准确），但只把窗口内发生的平仓计入本期已实现盈亏与胜率——避免把旧周期的
    战果重复算进本期。"""
    from .engines import pct
    try:
        from datetime import date as _d, timedelta as _td
        end = _d.fromisoformat(today)
        start = (end - _td(days=days)).isoformat()
    except Exception:  # noqa: BLE001
        start = None

    snap = account_snapshot(con, today)
    eq, init = snap["equity"], snap["init"]
    ret_total = (eq / init - 1) * 100 if init else 0.0

    # —— FIFO：历史买仓进队列，窗口内平仓计本期已实现 ——
    buyq = {}
    realized_by_code = {}
    closed_trades = win_trades = 0
    realized_total = 0.0
    for ts, code, side, qty, price, fee in con.execute(
            "SELECT ts, code, side, qty, price, fee FROM fills "
            "ORDER BY ts").fetchall():
        if side == "buy":
            buyq.setdefault(code, []).append([qty, price])
            continue
        # sell：按 FIFO 取成本基
        remaining = qty
        cost_basis = 0.0
        for lot in buyq.get(code, []):
            if remaining <= 0:
                break
            take = min(remaining, lot[0])
            cost_basis += take * lot[1]
            lot[0] -= take
            remaining -= take
        proceeds = qty * price - (fee or 0)
        realized = proceeds - cost_basis
        if start and ts[:10] >= start:
            closed_trades += 1
            realized_total += realized
            realized_by_code[code] = realized_by_code.get(code, 0.0) + realized
            if realized > 0:
                win_trades += 1

    # —— 未实现盈亏（当前持仓按市价）——
    unreal_by_code = {}
    for code, qty, cost in con.execute(
            "SELECT code, SUM(qty), CASE WHEN SUM(qty)>0 "
            "THEN SUM(qty*cost)/SUM(qty) ELSE 0 END "
            "FROM position_batches GROUP BY code").fetchall():
        price = _last_price(con, code, today) or cost
        unreal_by_code[code] = qty * (price - cost)
    unreal_total = sum(unreal_by_code.values())

    # —— 板块聚合（最佳/最差）——
    def sector_of(code):
        r = con.execute(
            "SELECT sector FROM stock_industry WHERE code=?", (code,)).fetchone()
        return r[0] if r and r[0] else "未分类"
    sec_pnl = {}
    all_codes = set(realized_by_code) | set(unreal_by_code)
    for code in all_codes:
        s = sector_of(code)
        sec_pnl[s] = sec_pnl.get(s, 0.0) + realized_by_code.get(code, 0.0) \
            + unreal_by_code.get(code, 0.0)
    ranked = sorted(sec_pnl.items(), key=lambda kv: kv[1], reverse=True)
    best_sec = ranked[0] if ranked else None
    worst_sec = ranked[-1] if ranked and len(ranked) > 1 else None
    win_rate = (win_trades / closed_trades * 100) if closed_trades else None

    # —— 盈利最大化 + 系统改进建议 ——
    net = realized_total + unreal_total
    maxi = []
    if best_sec and best_sec[1] > 0:
        maxi.append(f"盈利高度集中在【{best_sec[0]}】板块（窗口净 "
                     f"{best_sec[1]:+,.0f} 元）——下一周期在强势板块里优先加仓、"
                     f"放宽到价条件，把盈利做厚。")
    else:
        maxi.append("窗口内无明显盈利板块，建议把仓位收敛到少数‘已确认买区’的票，"
                    "不做广撒网。")
    if unreal_total > 0:
        maxi.append(f"当前浮盈 {unreal_total:+,.0f} 元未落袋：对已达 +15% 止盈线或"
                    f"触发退出的持仓，果断了结，把纸上富贵变成现金。")
    elif unreal_total < 0:
        maxi.append(f"当前浮亏 {unreal_total:+,.0f} 元：按退出信号纪律减仓，"
                    f"不摊平亏损票。")
    improve = []
    if closed_trades == 0:
        improve.append("窗口内无平仓记录（尚处建仓/持有阶段），暂无胜率数据；"
                       "保持‘只在条件满足且现价落买区下沿才下单’的纪律。")
    elif win_rate is not None and win_rate < 50:
        improve.append(f"平仓胜率 {win_rate:.0f}% 偏低（<50%）：买点选择或止损需收紧——"
                       f"只在‘四态=条件满足’且现价贴近买区下沿时建仓，"
                       f"减少‘追高买在半山腰’。")
    if worst_sec and best_sec and worst_sec[0] != best_sec[0] and worst_sec[1] < 0:
        improve.append(f"亏损集中在【{worst_sec[0]}】板块：弱势板块的票优先按退出信号"
                       f"减仓，仓位向强势板块收敛。")
    if snap["n_hold"] >= RISK["max_holdings"]:
        improve.append(f"持仓已达上限 {snap['n_hold']} 只：严守单票"
                       f"≤{RISK['max_pos_pct']:.0%} 敞口上限，避免赌单一票。")
    improve.append("系统层面：若连续多周期跑输沪指，建议上调决断门控"
                   f"（DECISIVE_NET_MIN={DECISIVE_NET_MIN}/DECISIVE_EFF_MIN="
                   f"{DECISIVE_EFF_MIN}）进一步剔除磨叽票，并提升板块热度权重，"
                   "只在领涨行业的票上出手。")
    return {"today": today, "days": days, "equity": eq, "init": init,
            "ret_total": ret_total, "realized": realized_total,
            "unrealized": unreal_total, "net": net,
            "closed_trades": closed_trades, "win_trades": win_trades,
            "win_rate": win_rate, "best_sec": best_sec, "worst_sec": worst_sec,
            "by_code": {c: realized_by_code.get(c, 0.0)
                        + unreal_by_code.get(c, 0.0) for c in all_codes},
            "sector_pnl": sec_pnl,
            "maximize": maxi, "improve": improve}


def session_gate(today, now=None):
    """★ 用户需求（2026-09-18）：「需要考虑周末和节假日，今天已经不在交易
    时间了又开始购买」。返回 (可下单?, 原因)。

    为什么必须查**时刻**、不能只查日期：`close` 班定时器是 15:22、`pre` 班是
    08:50——两天都是不折不扣的交易日，却都不在撮合时段。只看日期 ⇒ 15:22 会
    拿当日收盘价建仓，用户看到的就是"收盘了还在买"（实测就是这么发生的）。
    """
    t = now or tc.now_cst()
    if tc.in_trading_session(t):
        return True, ""
    return False, tc.session_note(t, today=today)


def _name_of(con, code):
    """取股票名（rec_picks → snapshot）。取不到就退化成代码，绝不让整行消失。"""
    for sql in ("SELECT name FROM rec_picks WHERE code=? AND name<>'' "
                "ORDER BY date DESC LIMIT 1",
                "SELECT name FROM snapshot WHERE code=? AND name<>'' "
                "ORDER BY date DESC LIMIT 1"):
        row = con.execute(sql, (code,)).fetchone()
        if row and row[0]:
            return row[0]
    return code


def _pick_of(con, today, code):
    """当日推荐里的这只票（名称/动作/买区/评分）。供推送渲染回溯。"""
    return con.execute(
        "SELECT name, action, buy_low, buy_high, score FROM rec_picks "
        "WHERE date=? AND code=? ORDER BY score DESC LIMIT 1",
        (today, code)).fetchone()


def opened_today(con, today):
    """今日是否已经建过仓（★ 每天最多一批，见 auto_open 的说明）。"""
    return con.execute("SELECT 1 FROM exec_log WHERE action='auto_open' "
                       "AND code=?", (today,)).fetchone() is not None


def holdings_rows(con, today, slot=None):
    """当前持仓明细（推送「持有什么」区块）。"""
    out = []
    for code, qty, cost, buy_date in con.execute(
            "SELECT code, SUM(qty), "
            "CASE WHEN SUM(qty)>0 THEN SUM(qty*cost)/SUM(qty) ELSE 0 END, "
            "MIN(buy_date) FROM position_batches GROUP BY code "
            "ORDER BY MIN(buy_date)").fetchall():
        price = _last_price(con, code, today, slot) or cost
        status, why = "持有", ""
        try:
            act, reasons, _d = evaluate_exit(con, code, today)
            if act == "SELL":
                status, why = "待卖出", "；".join(reasons)
        except Exception:  # noqa: BLE001 —— 一根K线缺失不该让整段持仓消失
            pass
        try:
            days = (datetime.fromisoformat(today)
                    - datetime.fromisoformat(buy_date)).days
        except Exception:  # noqa: BLE001
            days = 0
        out.append({"code": code, "name": _name_of(con, code), "qty": qty,
                    "cost": cost, "price": price,
                    "pnl_pct": (price / cost - 1) * 100 if cost else 0.0,
                    "days": days, "status": status, "status_reason": why,
                    "amount": qty * price})
    return out


def auto_open(con, today, max_new=None, slot=None, now=None, quiet=False):
    """按当日推荐自动建仓——模拟盘「自动运行」的核心（2026-09-18 新增）。

    ⚠️ 为什么需要：原实现**只有退出裁决、没有任何买入路径**。账户永远空仓
    ⇒ 巡逻无对象 ⇒ log 为空 ⇒ 连一条推送都不会发。用户看到的"模拟盘没在
    跑"根因就在这里（叠加 executor workflow 此前从未挂定时器）。

    纪律：
      · **先过交易时段闸门**（用户需求）：周末/法定节假日、开盘前、午休、
        收盘后一律不建仓。日期对不等于时段对，两件事都要查。
      · 只买与推送**同源口径**的票（当日 rec_picks 里 action ∈ 现在买/等回踩/
        小仓试，且现价确实落在买区内）——推什么就模拟买什么，避免出现
        "推的票没买、买的票没推"这种无法对账的状态；
      · 单票目标资金 = 净值 × min(max_pos_pct/max_holdings, 0.25)，手数取整
        到 100 股；不足 1 手直接跳过（不硬凑、不放松风控凑单）；
      · 风控**只走 place_order 一处**（涨跌停/资金/T+1/累计敞口/持仓上限/
        单日委托数全在那），本函数不重复实现判定，避免两套规则打架；
      · ★ **每天最多建一批**：一旦有成交就落 `exec_log` 标记，当天后续的
        盘中 run 不再开新仓。理由不是省事——是**推送已经发过了**：日级保险
        丝按 mode+date 只放行一条 `exec_auto`，如果下午再偷偷买，那笔成交
        就永远没人告诉用户，正是"模拟盘很乱"的来源。
      · 无推荐 / 已满仓 / 熔断锁定 → 返回空列表，不推空消息。
    """
    ok, why = session_gate(today, now)
    if not ok:
        return [("-", "SESSION", why)]
    acct = ensure_account(con, today)
    if acct[3]:
        return [("-", "HOLD", "日内亏损熔断锁定，不开新仓（M22）")]
    # 2026-09-21 用户需求：盘中出现更合适的买点 → **直接按区间买入推荐**。
    # 原「每天最多一批」硬闸取消，改为三层护栏兜底：最大持仓只数、单日委托
    # 数上限、现价必须落在买区（place_order 内还有资金/涨跌停/T+1/敞口）。
    # 推送纪律不变：换仓/追加批次由 run() 以 force 推送，成交必被告知。
    rows = con.execute(
        "SELECT code, name, action, buy_low, buy_high, score FROM rec_picks "
        "WHERE date=? AND action IN ('现在买','等回踩','小仓试') "
        "ORDER BY score DESC", (today,)).fetchall()
    if not rows:
        return []
    held = {r[0] for r in con.execute(
        "SELECT DISTINCT code FROM position_batches")}
    eq = equity(con)
    plan = _position_plan()
    log = []
    filled = 0
    for code, name, action, lo, hi, score in rows:
        if max_new and filled >= max_new:
            break
        if code in held:
            continue
        if len(held) >= RISK["max_holdings"]:
            break
        price = _last_price(con, code, today, slot)
        if not price or price <= 0:
            log.append((code, "SKIP", "无当日价格，跳过"))
            continue
        # 现价必须落在买区内（与推送 buyable_now 同一把尺子）
        if lo and hi and not (lo * 0.995 <= price <= hi * 1.005):
            if not quiet:
                log.append((code, "SKIP",
                            f"现价{price:.2f}不在买区{lo:.2f}-{hi:.2f}，不追"))
            continue
        # 分仓档位：第 len(held)+1 笔占用 plan 对应档（3322/3331）
        slot_i = min(len(held), len(plan) - 1)
        slot_pct = plan[slot_i]
        per_amt = eq * slot_pct
        # 现金预算钳制：3322 四档总和 = 100% 净值，最后一档必须给手续费
        # 留缓冲，否则差几块钱被拒单 → 永远建不满 4 仓
        _cash = con.execute(
            "SELECT cash FROM account_state WHERE id=1").fetchone()
        budget = min(per_amt, RISK["max_order_amt"],
                     max((_cash[0] if _cash else 0) * 0.985, 0))
        qty = int(budget / price / 100) * 100
        if qty < 100:
            # 资金不足 1 手 = 到价买不进 → REJECT（进「到价未成交」组，
            # 用户必须被告知；用户需求：「到价了却买不进，必须说清楚原因」）
            log.append((code, "REJECT",
                        f"资金不足 1 手（价{price:.2f}，"
                        f"档位目标 {budget:,.0f} 元）"))
            continue
        prev = con.execute(
            "SELECT c FROM klines WHERE code=? AND date<? "
            "ORDER BY date DESC LIMIT 1", (code, today)).fetchone()
        oid, status, why = place_order(
            con, code, "buy", qty, price, today,
            prev_close=prev[0] if prev else None,
            reason=(f"自动建仓 {action}（第{slot_i + 1}档 "
                    f"{slot_pct:.0%}·目标{per_amt:,.0f}元）"))
        if status == "filled":
            held.add(code)
            filled += 1
            log.append((code, "BUY",
                        f"{qty}股@{price:.2f}（{action}·"
                        f"第{slot_i + 1}档{slot_pct:.0%}≈{per_amt:,.0f}元）"))
        else:
            # ★ 用户需求：**到价了却买不进，必须说清楚原因**（资金不足/
            # 满仓/涨停/单日额度…）。原实现也记 REJECT，但推送里混在流水里
            # 没人看得见，等于没告诉。
            log.append((code, "REJECT", why))
    # 建成即封板（见 docstring：下午再偷偷买会让成交无人知晓）
    if filled:
        con.execute("INSERT INTO exec_log VALUES(?,?,?,?,?)",
                    (_now(), today, "auto_open", None,
                     f"slot={slot or '-'} 成交 {filled} 只"))
        con.commit()
    return log



def _annotate_sector(con, today, items):
    """给推送条目打板块标签（best-effort：拿不到就不标注，绝不阻断推送）。"""
    if not items:
        return
    try:
        from . import sector
        sector.annotate(con, today, items)
    except Exception:  # noqa: BLE001
        pass


def _exec_push(con, task, today, log, slot=None):
    """组装并发送模拟盘报告。返回推送结果；无实质动作 → None（不推送）。

    ★ 只在「有实质动作」时推送 —— 用户原话「推送消息太多我根本分不清」。
      实质动作 = 建仓 / 卖出·退出 / 到价未成交（含资金不足）。非交易时段的
      说明、未到买点、纯持有，都不单独成条消息：每天有 09:25 / 09:45 / 14:40 /
      15:22 四个 run，若每个都推一条"无事发生"，模拟盘自己就是最大噪音源。
    """
    from . import notifier
    if not any(a in ACTIONABLE for _, a, _ in log):
        print("[executor] 无实质动作，不推送（避免噪音）")
        return None
    opened, blocked, sold, skipped = [], [], [], []
    for code, action, detail in log:
        pk = _pick_of(con, today, code) if code != "-" else None
        name = (pk[0] if pk and pk[0] else None) or _name_of(con, code)
        if action == "BUY":
            b = con.execute(
                "SELECT qty, cost FROM position_batches WHERE code=? "
                "AND buy_date=? LIMIT 1", (code, today)).fetchone()
            item = {"code": code, "name": name,
                    "qty": b[0] if b else 0, "price": b[1] if b else 0}
            item["amount"] = item["qty"] * item["price"]
            if pk:
                item.update({"action": pk[1], "buy_low": pk[2] or 0,
                             "buy_high": pk[3] or 0, "score": pk[4]})
            opened.append(item)
        elif action == "REJECT":
            # ★ 用户需求：「无法买入的到达买点了也同样告诉我，比如资金不足」
            blocked.append({"code": code, "name": name,
                            "buy_low": (pk[2] if pk else 0) or 0,
                            "buy_high": (pk[3] if pk else 0) or 0,
                            "price": _last_price(con, code, today, slot),
                            "reason": detail})
        elif action in ("SELL", "RISK_BLOCKED", "RISK_FLAGGED"):
            sold.append({"code": code, "name": name, "action": action,
                         "detail": detail})
        else:
            skipped.append((code, detail))
    _annotate_sector(con, today, opened)
    acct = account_snapshot(con, today)
    holdings = holdings_rows(con, today, slot)
    note = next((d for _c, a, d in log if a == "SESSION"), "")
    html = notifier.render_exec_report(
        today, acct, opened=opened, blocked=blocked, sold=sold,
        holdings=holdings, skipped=skipped, note=note,
        slot_label={"am": " · 早盘", "pm": " · 尾盘"}.get(slot, ""))
    # 标题摘要（配合 notifier 的【模拟】【Astra】前缀，一眼知行情）
    head = f"建仓 {len(opened)} 只 · 持仓 {len(holdings)} 只"
    if blocked:
        head += f" · 未成交 {len(blocked)} 只"
    if sold:
        head += f" · 退出 {len(sold)} 只"
    r = notifier.push(
        f"exec_{task}", head, html, date=today, con=con,
        force=any(a in ("SELL", "RISK_BLOCKED", "RISK_FLAGGED")
                  for _, a, _ in log))
    # 打印推送三态（记忆纪律：日志必须能回答"到底发出去了没"——
    # 只看"步骤 success"会漏掉 uncertain 这类静默失败）。
    print(f"[executor] push={r}")
    return r


def run(task="scan", price_of=None, slot=None, now=None):
    """模拟盘日常：自动建仓（task=auto）+ 持仓退出裁决 + 结构化推送。

    task 语义（2026-09-18 明确化，原实现忽略 task 参数）：
      · `auto` —— 自动建仓 + 巡逻（默认自动运行形态）
      · `scan`/`tail`/`now` —— 只巡逻（保持原有语义，不做买入）

    slot：盘中任务传 "am"/"pm" ⇒ 取 `snapshot_live` 的**当下价**（唯一实时来源）。
    now ：可注入的"现在"（测试用）。None ⇒ 取北京时间当前时刻。
    """
    con = get_conn()
    today = today_str()
    ensure_account(con, today)
    # M22 日内熔断：触发后当日锁定（不因盘中反弹自动解除），卖出不受限
    if day_pnl_pct(con, today) <= RISK["daily_loss_halt"] * 100:
        con.execute("UPDATE account_state SET frozen=1 WHERE id=1")
        con.commit()
    log = []
    if task in ("auto", "open"):
        log.extend(auto_open(con, today, slot=slot, now=now))
    # ⚠️ 2026-09-18 修（实测噪音）：巡逻范围 = **存在已解锁批次（buy_date<today）**
    # 的持仓。原实现取 `SELECT DISTINCT code FROM position_batches`（含当日新建仓），
    # 于是 auto 建仓当天会把刚买的票全部判一遍 —— 而 T+1 决定它们今天无论如何都
    # 卖不出去，判决必然落成 `RISK_FLAGGED`（09-18 CI 实测：3 只新仓全部报
    # "触发退出但 T+1 受限：ATR保护线"）。这不是风险提示，是纯噪音，而且时序
    # 本身错位：用**当日收盘价**买入，却拿**当日盘中最低价**当"持有期触及止损"
    # 来判 —— 买入之前那段行情并不属于持有期。
    # M27 的语义（"触发但卖不出"要留痕）针对的是**跌停卖不出**，由已解锁批次
    # 正常覆盖，不受本改动影响。
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM position_batches WHERE buy_date<?",
        (today,))]
    for code in codes:
        action, reasons, detail = evaluate_exit(con, code, today)
        if action == "SELL":
            row = con.execute(
                "SELECT c FROM klines WHERE code=? ORDER BY date DESC LIMIT 1",
                (code,)).fetchone()
            prev = con.execute(
                "SELECT c FROM klines WHERE code=? ORDER BY date DESC LIMIT 1 "
                "OFFSET 1", (code,)).fetchone()
            qty = available_qty(con, code, today)
            if qty <= 0:
                # 当日买入不可卖，但风险必须留痕（M27）
                log.append((code, "RISK_FLAGGED", "触发退出但 T+1 受限：" +
                            "；".join(reasons)))
                continue
            oid, status, why = place_order(
                con, code, "sell", qty, row[0], today,
                prev_close=prev[0] if prev else None, risk_sell=True,
                reason="；".join(reasons))
            log.append((code, "SELL" if status == "filled" else "RISK_BLOCKED",
                        why))
        else:
            log.append((code, "HOLD", detail))
    # ★ 去弱留强（用户 2026-09-21「持续弱拿着没意义，本质是去弱留强」）：
    # 本轮 patrol 卖出成交腾出名额后，同一次 run 内直接按买区回补更优候选
    # （quiet=True：未到买点的 SKIP 不重复进推送，未到买点组已有同信息）。
    if any(a == "SELL" for _, a, _ in log):
        log.extend(auto_open(con, today, slot=slot, now=now, quiet=True))
    con.commit()
    _exec_push(con, task, today, log, slot)
    return log
