# -*- coding: utf-8 -*-
"""数据质量分级守门（审计 M02/M03/N02 + 333-二 异常分级）。

核心原则（222.docx 二）：
- 数据异常不得伪装成"无机会"，也不得全停/全过二选一；
- 单位错误 → 阻断入库；超出历史分布 → 告警+多源复核；
- 多源确认的极端真实行情 → 允许入库；无法确认 → 隔离原始记录，不自动改写；
- 盘中累计成交额不套用完整交易日的下限；
- 修复必须记录原值、修复值、依据与版本。

异常分级（333-二）：
  STOP_ALL   交易日不确定/全市场明显过期 → 停止整个选股流程
  DROP_STOCK 单只数据缺失 → 排除该股票并记录原因
  NO_FIRST   覆盖不足可能影响排序 → 不发布"首选"结论
  LIMITED    少量孤立异常 → 发布受限结果并注明覆盖情况
  KEEP       推送失败 → 保留报告单独处理发送状态
覆盖阈值是运维配置，不是金融规律。
"""
import hashlib
import json
import os
import time
from datetime import datetime

from .core import CONFIG_DIR, DIST_DIR, today_str

STOP_ALL = "stop_all"
DROP_STOCK = "drop_stock"
NO_FIRST = "no_first"
LIMITED = "limited"
KEEP = "keep"

# 运维配置：候选覆盖率低于此值 → 不发布"首选"结论（M：阈值需实测校准）
MIN_COVERAGE_RATIO = 0.90

RULE_VERSION = "v3-20260912"


# ---------------------------------------------------------------------------
# M02 成交额守门分级（替代旧的绝对区间硬阻断）
# ---------------------------------------------------------------------------

HIST_DIST = (0.8e12, 8e12)      # 近年历史分布参考区间（非"物理不可能"边界）
UNIT_SUSPECT = (1e9, 5e12)      # 若数值落在此带且明显差 1e4 倍 → 疑似单位错误


def grade_total_amount(amount, intraday=False, multi_source_confirmed=False):
    """M02：返回 (level, action, reason)。
    level ∈ {"ok","warn","block"}；action ∈ {"allow","review","quarantine","block"}。
    """
    if amount is None or amount <= 0:
        return "block", "quarantine", "成交额缺失或非正"
    # 单位错误：典型为"元→万元/亿元"差 1e4/1e8 倍
    if amount < 1e11 and UNIT_SUSPECT[1] / amount >= 1e4:
        return "block", "block", f"疑似单位错误（{amount:.3e} 元）"
    if intraday:
        # 盘中累计成交额：只查单位错误，不套用完整交易日下限
        if amount > HIST_DIST[1]:
            return "warn", "review", "盘中累计额超出历史分布上限"
        return "ok", "allow", "盘中口径"
    lo, hi = HIST_DIST
    if lo <= amount <= hi:
        return "ok", "allow", "历史分布内"
    # 超出历史分布：告警 + 多源复核；多源确认的极端真实行情允许入库
    if multi_source_confirmed:
        return "warn", "allow", f"超出历史分布 [{lo:.1e},{hi:.1e}]，但多源确认为真实极端行情"
    return "warn", "review", f"超出历史分布 [{lo:.1e},{hi:.1e}]，需多源复核"


# ---------------------------------------------------------------------------
# M03 量纲修复：估计流通股数 = 成交股数 ÷ 换手率小数
# ---------------------------------------------------------------------------

def est_float_shares(volume_shares, turnover_rate):
    """换手率必须为小数（百分数先 ÷100）；为零/缺失/口径不明 → 返回 None 不计算。"""
    if volume_shares is None or turnover_rate is None:
        return None
    if turnover_rate <= 0:
        return None
    return volume_shares / turnover_rate


def repair_volume_units(rows_today, float_shares, rule_version=RULE_VERSION):
    """检测全市场量纲失灵（M03）。

    量纲锚：q = 成交量 / 流通股本。流通股本 = 流通市值 ÷ 股价（真实股本，
    **不可用换手率反推**——那会使 q ≡ 换手率，恒为常数，失去判别力）。
    腾讯K线量纲为「手」：q 正常 ≈ 换手率/100（<0.01）；
    源异常返回「股」时 q ≈ 换手率（常 >0.01）→ 据此判失灵。
    只返回修复建议与记录，不直接改写；执行方按记录执行并写 repair_log。
    返回 (repair_factor or 1.0, records[])
    """
    records = []
    q_bad = q_ok = 0
    for code, vol in rows_today:
        fs = (float_shares or {}).get(code)
        if not fs or fs <= 0 or not vol or vol <= 0:
            records.append({"code": code, "field": "volume", "old": vol,
                            "new": None, "basis": "流通股本缺失，跳过量纲校验",
                            "rule_version": rule_version})
            continue
        q = vol / fs
        if q > 0.01:
            q_bad += 1
        else:
            q_ok += 1
    total = q_bad + q_ok
    if total >= 10 and q_bad / total > 0.5:
        # 全市场失灵：典型差 100 倍（股→手）
        factor = 100.0
        for code, vol in rows_today:
            records.append({"code": code, "field": "volume", "old": vol,
                            "new": (vol / factor) if vol else None,
                            "basis": f"量纲失灵 {q_bad}/{total} 只 q>0.01，"
                                     f"按 股→手 ÷100 幂等修复",
                            "rule_version": rule_version})
        return factor, records
    return 1.0, records


def log_repairs(con, records):
    ts = datetime.now().isoformat(timespec="seconds")
    for r in records:
        con.execute(
            "INSERT INTO repair_log(ts,code,date,field,old_val,new_val,basis,"
            "rule_version) VALUES(?,?,?,?,?,?,?,?)",
            (ts, r["code"], r.get("date", ""), r["field"], r["old"],
             r["new"], r["basis"], r["rule_version"]))
    con.commit()


# ---------------------------------------------------------------------------
# N02 批次元数据
# ---------------------------------------------------------------------------

def batch_record(source, trade_date, source_time=None, caliber="", quality="ok",
                 extra=None):
    """每个数据批次记录：来源/交易日期/源端时间/抓取时间/口径/质量/版本。"""
    fetched = datetime.now().isoformat(timespec="seconds")
    raw = f"{source}|{trade_date}|{fetched}|{caliber}|{quality}|{RULE_VERSION}"
    return {"batch_id": hashlib.sha1(raw.encode()).hexdigest()[:16],
            "source": source, "trade_date": trade_date,
            "source_time": source_time or "", "fetched_at": fetched,
            "field_caliber": caliber, "quality": quality,
            "data_version": RULE_VERSION, "extra": json.dumps(extra or {},
                                                              ensure_ascii=False)}


def write_batch_meta(con, rec):
    con.execute(
        "INSERT OR REPLACE INTO batch_meta VALUES(?,?,?,?,?,?,?,?,?)",
        (rec["batch_id"], rec["source"], rec["trade_date"], rec["source_time"],
         rec["fetched_at"], rec["field_caliber"], rec["quality"],
         rec["data_version"], rec["extra"]))
    con.commit()


# ---------------------------------------------------------------------------
# 覆盖率决策（333-二 分级处理）
# ---------------------------------------------------------------------------

def coverage_decision(universe_n, fetched_n, fail_codes, trade_day_certain=True):
    """按异常分级返回 (action, detail)。action ∈ STOP_ALL/DROP_STOCK/NO_FIRST/LIMITED。"""
    if not trade_day_certain:
        return STOP_ALL, "交易日不确定 → 停止整个选股流程"
    if universe_n and fetched_n / universe_n < 0.5:
        return STOP_ALL, f"抓取覆盖 {fetched_n}/{universe_n} 过低 → 全停"
    if fail_codes:
        ratio = 1 - len(fail_codes) / max(1, universe_n)
        if ratio < MIN_COVERAGE_RATIO:
            return NO_FIRST, (f"覆盖 {fetched_n}/{universe_n} 不足 "
                              f"{MIN_COVERAGE_RATIO:.0%} → 不发布首选结论")
        return LIMITED, (f"孤立异常 {len(fail_codes)} 只已排除"
                         f"（{','.join(fail_codes[:5])}）→ 注明覆盖后发布")
    return LIMITED, "覆盖完整 → 正常发布"


def quarantine_records(con, dates, out_dir=None):
    """M04：异常日期清洗前先隔离备份（JSON），再交由调用方删除。"""
    out_dir = out_dir or os.path.join(DIST_DIR, "quarantine")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {}
    for d in dates:
        rows = {}
        for tbl in ("klines", "zt_pool", "snapshot", "rec_picks",
                    "candidate_snapshots"):
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})")]
            if "date" in cols:
                rows[tbl] = con.execute(
                    f"SELECT * FROM {tbl} WHERE date=?", (d,)).fetchall()
        payload[d] = rows
    path = os.path.join(out_dir, f"quarantine_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str, indent=1)
    return path
