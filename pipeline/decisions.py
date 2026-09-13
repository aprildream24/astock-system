# -*- coding: utf-8 -*-
"""统一决策对象与信号生命周期（审计 N04/M11/M12/M13/M18 + 333-三）。

N04：推送、网页、模拟盘必须引用同一份决策结果——
  {signal_id, code, strategy, channel, status, reason, zone, invalid_if,
   valid_until, data_version, rule_version}。

M18 决策状态机（等待/触发/失效/数据不足，与执行状态分离）：
  等待确认 → 条件满足 / 超价取消 / 结构失效 / 数据不足 / 到期失效
  委托中/部分成交/已成交属于执行状态，只存在于 orders 表，不混入本状态机。

M11 评级与执行状态分离：research_grade 只参与排序与风险预算，
  不直接产生"现在买"；M12 必要字段缺失 → 数据不足，不进可执行名单。
M13 边界统一：所有门槛一律 ≥ / ≤（5.00% 属于"高开≥5%"档，边界测试锁定）。

333-三 日K口径纪律：日K无法知道盘中高低点先后顺序——
  同日"触及止损"与"触及买区"同时出现时，保守判结构失效并记录双触发，
  不伪造"先触发后止损"的执行过程。
"""
import hashlib
import json
from datetime import datetime, timedelta

RULE_VERSION = "v3-20260912"
VALID_DAYS = 5               # 观察计划默认有效期（交易日）
EXEC_GRADES = ("A", "B", "T", "C")   # 5.1 分级基线；X=不满足条件


def signal_id(strategy, code, date):
    return hashlib.sha1(
        f"{strategy}|{code}|{date}|{RULE_VERSION}".encode()).hexdigest()[:16]


def research_grade(cand, missing_fields=()):
    """5.1 分级基线（A/B/T/C/X）+ M12 缺失字段处理。

    必要字段（流通市值/交易状态/价格）缺失 → ("X", "数据不足")，
    不得按 B 级兜底。评级只参与排序，不承诺仓位（M14）。"""
    for f in missing_fields:
        if cand.get(f) is None:
            return "X", "数据不足"
    streak = cand.get("consecutive_limit_ups") or 0
    gap = cand.get("gap_pct")
    fmv = cand.get("fmv")
    fmv_ok = fmv is None or False   # 市值缺失时按 M12 由 missing_fields 处理
    hi_open = gap is not None and gap >= 5.0        # M13：≥5%（含 5.00%）
    big_fmv = fmv is not None and 60e8 <= fmv <= 150e8
    if hi_open and streak >= 3 and big_fmv:
        return "A", "条件满足"
    if streak >= 3 and big_fmv:
        return "B", "等待确认"
    if cand.get("pool") == "趋势":
        return "T", "等待确认"
    if hi_open and big_fmv:
        return "C", "等待确认"
    return "X", "等待确认"


def make_decision(cand, date, data_version=RULE_VERSION,
                  missing_fields=()):
    """N04 统一决策对象。exec_status 与 research_grade 分离（M11）。"""
    pool = cand.get("pool", "")
    strategy = {"连板": "ladder_auction", "趋势": "trend_close",
                "区间": "trend_close", "波段": "trend_close"}.get(pool, pool)
    zone_low, zone_high = cand.get("buy_low"), cand.get("buy_high")
    grade, _ = research_grade(cand, missing_fields)
    missing = bool(missing_fields)
    if missing:
        status = "数据不足"
    elif cand.get("action") == "现在买":
        status = "条件满足"
    elif cand.get("action") in ("等回踩", "次日竞价达标买", "小仓试"):
        status = "等待确认"
    else:
        status = "等待确认"
    valid_until = date     # 由调用方按交易日历推
    stop = cand.get("stop")
    invalid_if = []
    if stop:
        invalid_if.append(f"收盘跌破止损 {stop:.2f}")
    if zone_high:
        invalid_if.append(f"现价超出不追价上限 {zone_high * 1.03:.2f}")
    d = {"signal_id": signal_id(strategy, cand["code"], date),
         "code": cand["code"], "name": cand.get("name", ""),
         "strategy": strategy, "channel": strategy,
         "rule_version": RULE_VERSION, "data_version": data_version,
         "data_date": date, "status": status,
         "reason": cand.get("entry_hint") or cand.get("cycle_hint", ""),
         "zone": [zone_low, zone_high], "stop": stop,
         "invalid_if": "；".join(invalid_if) or "条件破坏即失效",
         "valid_until": valid_until,
         "research_grade": grade, "exec_status": status,
         "is_st": bool(cand.get("is_st")),
         "consecutive_limit_ups": cand.get("consecutive_limit_ups") or 0}
    return d


def persist_decision(con, d):
    """信号落账本（333-三 信号账本）。已存在同 signal_id → 不重复建。"""
    row = con.execute("SELECT 1 FROM signals WHERE signal_id=?",
                      (d["signal_id"],)).fetchone()
    if row:
        return False
    con.execute(
        "INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d["signal_id"], d["code"], d["strategy"], d["rule_version"],
         datetime.now().isoformat(timespec="seconds"), d["data_date"],
         d["status"], d["zone"][0], d["zone"][1], d["stop"],
         d["invalid_if"], d["valid_until"], d["reason"], "", ""))
    con.commit()
    return True


def advance_signals(con, today, close_of, valid_days=VALID_DAYS):
    """用日K收盘推进昨日及更早的未决信号（333-三 生命周期）。

    close_of(code) → (close, low, high) 或 None（数据不足）。
    判定优先级：到期失效 > 结构失效（含双触发保守失效）> 超价取消
    > 条件满足（区间内）> 保持等待。
    返回 changes: [{code, old, new, reason}]。"""
    changes = []
    rows = con.execute(
        "SELECT signal_id, code, status, zone_low, zone_high, stop, "
        "data_date FROM signals WHERE status IN ('等待确认','条件满足')").fetchall()
    for sid, code, st, lo, hi, stop, d0 in rows:
        bar = close_of(code)
        new, why = st, ""
        if bar is None:
            new, why = "数据不足", "当日行情缺失"
        else:
            close, low, high = bar
            if d0 == today:
                continue                      # 当日新建信号今日不推进
            if stop and low <= stop and lo <= close <= hi:
                # 日K无法分辨盘中先后 → 保守失效，不伪造执行顺序
                new = "结构失效"
                why = "同日触及止损与买区（先后顺序不可知，保守失效）"
            elif stop and close <= stop:
                new, why = "结构失效", f"收盘跌破止损 {stop:.2f}"
            elif hi is not None and close > hi * 1.03:
                new, why = "超价取消", f"现价 {close:.2f} 超出不追价上限 {hi * 1.03:.2f}"
            elif lo is not None and lo <= close <= hi:
                new, why = "条件满足", "收盘回到关注区间"
            # 其余：保持等待确认
        if new != st:
            con.execute(
                "UPDATE signals SET status=?, status_reason=?, changed_at=? "
                "WHERE signal_id=?", (new, why,
                                      datetime.now().isoformat(timespec="seconds"),
                                      sid))
            changes.append({"code": code, "old": st, "new": new, "reason": why})
    # 到期失效
    con.execute(
        "UPDATE signals SET status='到期失效', status_reason='超过有效期', "
        "changed_at=? WHERE status IN ('等待确认','条件满足') AND data_date<=?",
        (datetime.now().isoformat(timespec="seconds"),
         (datetime.fromisoformat(today)
          - timedelta(days=valid_days)).isoformat()))
    con.commit()
    return changes


def active_signals(con):
    rows = con.execute(
        "SELECT code, strategy, status, zone_low, zone_high, stop, reason "
        "FROM signals WHERE status IN ('等待确认','条件满足')").fetchall()
    return [{"code": r[0], "strategy": r[1], "status": r[2],
             "zone": [r[3], r[4]], "stop": r[5], "reason": r[6]}
            for r in rows]
