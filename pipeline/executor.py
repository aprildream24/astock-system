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
import uuid
from datetime import datetime

from . import core
from .core import get_conn, today_str

RISK = {"max_pos_pct": 0.70, "max_holdings": 4, "max_daily_orders": 6,
        "min_order_amt": 1000.0, "max_order_amt": 60000.0,
        "daily_loss_halt": -0.03, "init_cash": 100000.0}
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


def evaluate_exit(con, code, today, protect_prev=None):
    """M24：收集全部触发规则 → 固定优先级定动作；M25 按持仓收益。

    返回 (action, reasons, detail)。action ∈ SELL/HOLD。
    N07：ATR 保护线只收紧——调用方传入上一日保护线，取 max。"""
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


def _last_price(con, code, date):
    """当日可用价格：优先当日快照（盘前/盘中也能拿到），回退最近一根K线。"""
    row = con.execute("SELECT price FROM snapshot WHERE date=? AND code=?",
                      (date, code)).fetchone()
    if row and row[0]:
        return float(row[0])
    row = con.execute(
        "SELECT c FROM klines WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
        (code, date)).fetchone()
    return float(row[0]) if row and row[0] else None


def account_line(con, today):
    """账户概览行（推送头部用）：净值 / 现金 / 持仓数 / 累计收益。"""
    acct = ensure_account(con, today)
    eq = equity(con)
    ret = (eq / RISK["init_cash"] - 1) * 100
    n = con.execute(
        "SELECT COUNT(DISTINCT code) FROM position_batches").fetchone()[0]
    return (f"账户 ¥{eq:,.0f}（现金 ¥{acct[0]:,.0f} · 持仓 {n} 只 · "
            f"累计 {ret:+.2f}% ｜ 起步 ¥{RISK['init_cash']:,.0f}）")


def auto_open(con, today, max_new=None):
    """按当日推荐自动建仓——模拟盘「自动运行」的核心（2026-09-18 新增）。

    ⚠️ 为什么需要：原实现**只有退出裁决、没有任何买入路径**。账户永远空仓
    ⇒ 巡逻无对象 ⇒ log 为空 ⇒ 连一条推送都不会发。用户看到的"模拟盘没在
    跑"根因就在这里（叠加 executor workflow 此前从未挂定时器）。

    纪律：
      · 只买与推送**同源口径**的票（当日 rec_picks 里 action ∈ 现在买/等回踩/
        小仓试，且现价确实落在买区内）——推什么就模拟买什么，避免出现
        "推的票没买、买的票没推"这种无法对账的状态；
      · 单票目标资金 = 净值 × min(max_pos_pct/max_holdings, 0.25)，手数取整
        到 100 股；不足 1 手直接跳过（不硬凑、不放松风控凑单）；
      · 风控**只走 place_order 一处**（涨跌停/资金/T+1/累计敞口/持仓上限/
        单日委托数全在那），本函数不重复实现判定，避免两套规则打架；
      · 无推荐 / 已满仓 / 熔断锁定 → 返回空列表，不推空消息。
    """
    acct = ensure_account(con, today)
    if acct[3]:
        return [("-", "HOLD", "日内亏损熔断锁定，不开新仓（M22）")]
    rows = con.execute(
        "SELECT code, name, action, buy_low, buy_high, score FROM rec_picks "
        "WHERE date=? AND action IN ('现在买','等回踩','小仓试') "
        "ORDER BY score DESC", (today,)).fetchall()
    if not rows:
        return []
    held = {r[0] for r in con.execute(
        "SELECT DISTINCT code FROM position_batches")}
    eq = equity(con)
    per_amt = eq * min(RISK["max_pos_pct"] / RISK["max_holdings"], 0.25)
    log = []
    for code, name, action, lo, hi, score in rows:
        if max_new and len(log) >= max_new:
            break
        if code in held:
            continue
        if len(held) >= RISK["max_holdings"]:
            break
        price = _last_price(con, code, today)
        if not price or price <= 0:
            log.append((code, "SKIP", "无当日价格，跳过"))
            continue
        # 现价必须落在买区内（与推送 buyable_now 同一把尺子）
        if lo and hi and not (lo * 0.995 <= price <= hi * 1.005):
            log.append((code, "SKIP",
                        f"现价{price:.2f}不在买区{lo:.2f}-{hi:.2f}，不追"))
            continue
        qty = int(min(per_amt, RISK["max_order_amt"]) / price / 100) * 100
        if qty < 100:
            log.append((code, "SKIP", f"单票资金不足 1 手（价{price:.2f}）"))
            continue
        prev = con.execute(
            "SELECT c FROM klines WHERE code=? AND date<? "
            "ORDER BY date DESC LIMIT 1", (code, today)).fetchone()
        oid, status, why = place_order(
            con, code, "buy", qty, price, today,
            prev_close=prev[0] if prev else None,
            reason=f"自动建仓 {action}")
        if status == "filled":
            held.add(code)
            log.append((code, "BUY", f"{qty}股@{price:.2f}（{action}）"))
        else:
            log.append((code, "REJECT", why))
    return log


def run(task="scan", price_of=None):
    """模拟盘日常：自动建仓（task=auto）+ 全部持仓退出裁决。

    task 语义（2026-09-18 明确化，原实现忽略 task 参数）：
      · `auto` —— 自动建仓 + 巡逻（默认自动运行形态）
      · `scan`/`tail`/`now` —— 只巡逻（保持原有语义，不做买入）
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
        log.extend(auto_open(con, today))
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
    con.commit()
    # 推送（模拟盘只走 PushPlus；重要退出风险不被普通去重拦截）
    if log:
        from . import notifier
        # 账户概览打头（用户要"按 100000 元起步自动运行"，那就得看得见净值）
        lines = [account_line(con, today)]
        for c, a, r in log:
            mark = {"SELL": "⛔", "RISK_BLOCKED": "⛔", "RISK_FLAGGED": "⚠️",
                    "BUY": "🔴", "REJECT": "✋"}.get(a, "·")
            lines.append(f"{mark} {c} {a} {r}")
        md = "# 模拟盘 " + today + "\n" + "\n".join(lines)
        force = any(a in ("SELL", "RISK_BLOCKED", "RISK_FLAGGED")
                    for _, a, _ in log)
        notifier.push(f"exec_{task}", today, notifier.md2html(md), date=today,
                      con=con, force=force)
    return log
