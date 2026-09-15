# -*- coding: utf-8 -*-
"""数据抓取入口：全市场日K + 收盘快照 → SQLite（含全部质量防线）。

用法：python -m pipeline.fetch_daily [--days 260] [--limit N]
"""
import argparse
import json
import os
import re
import urllib.request

from . import core, gapfill, quality
from . import trade_calendar as holiday_cal
from .core import (get_conn, fetch_text, kline_batch, trade_calendar,
                   is_trading_day, today_str, upsert_klines,
                   corp_action_scan, BROWSER_UA)

EM_SNAPSHOT = ("https://push2delay.eastmoney.com/api/qt/clist/get?"
               "pn=1&pz=6000&po=1&np=1&fltt=2&invt=2&"
               "fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&"
               "fields=f2,f3,f5,f6,f8,f12,f14,f20,f21")


def _num(v):
    """EM 快照停牌/缺失字段返回 '-'，统一转 float 或 None。"""
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def fetch_universe(max_stocks=None):
    """全市场清单+收盘快照。EM clist 单页上限约 100 条 → 按 pn 分页拉全。"""
    out = {}
    page = 1
    total = None
    while True:
        url = (f"https://push2delay.eastmoney.com/api/qt/clist/get?"
               f"pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&"
               "fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&"
               "fields=f2,f3,f5,f6,f8,f12,f14,f20,f21")
        try:
            js = json.loads(fetch_text(url))
        except Exception:  # noqa: BLE001
            break
        data = js.get("data") or {}
        total = data.get("total") or total
        diff = data.get("diff") or []
        if not diff:
            break
        for row in diff:
            num = row.get("f12", "")
            if re.match(r"^\d{6}$", num):
                out[num] = {"name": row.get("f14", ""),
                            "price": _num(row.get("f2")),
                            "pct": _num(row.get("f3")),
                            "vol": _num(row.get("f5")),
                            "amt": _num(row.get("f6")),
                            "turn": _num(row.get("f8")),
                            "fmv": _num(row.get("f21"))}
        if max_stocks and len(out) >= max_stocks:
            break
        if total and len(out) >= total:
            break
        page += 1
        if page > 80:   # 安全阀：80 页 = 8000 只
            break
    return out


def guard_snapshot(universe, today, con=None, partial=False):
    """M02 分级守门：单位错误阻断；超历史分布→告警隔离复核（不自动改写）；
    盘中/部分抓取不套用完整交易日阈值。返回 (level, reason)。"""
    pcts = [v.get("pct") or 0 for v in universe.values()]
    if pcts and all(abs(p) < 1e-9 for p in pcts):
        raise ValueError("快照 pct 全 0：疑似休市日，拒绝写库")
    if partial:
        print(f"[quality] 部分抓取（{len(universe)} 只）→ 跳过全日成交额分级")
        return "ok", "partial fetch"
    total_amt = sum(v.get("amt") or 0 for v in universe.values())
    level, action, reason = quality.grade_total_amount(total_amt)
    if level == "block":
        raise ValueError(f"成交额守门阻断：{reason}")
    print(f"[quality] 成交额 {total_amt:.3e} → {level}/{action}: {reason}")
    return level, reason


def fetch_daily(days=260, limit=None, force=False):
    con = get_conn()
    today = today_str()
    # 法定节假日日历守门（吸收原项目 trade_calendar）：节假日 cron 不白跑；
    # --force 供手动补数（build 侧守门不放松：目标日必须是真实交易日且有K线）
    if not holiday_cal.is_trade_day(today) and not force:
        print(f"[fetch] {today} {holiday_cal.why_closed(today)} → 跳过抓取"
              "（手动补数请加 --force）")
        return {"date": today, "skipped": "holiday"}
    universe = fetch_universe(max_stocks=limit)
    level, _ = guard_snapshot(universe, today, con,
                              partial=bool(limit and len(universe) < 3000))
    codes = sorted(universe.keys())
    # 市场准入前置（#486）：科创板/北交所等不可交易代码不发请求——省一半无效抓取
    from . import mktfilter
    codes = [c for c in codes if mktfilter.tradable(c)]
    # 增量同步：已同步到最近交易日的只补近端尾巴（INC_DAYS 根），
    # 新票/断档票才全量拉——依托历史库做增量，不每轮重拉全部历史
    #
    # 2026-09-15 修复（全天零推送根因）：原判定锚 latest_td = "今日往前推的
    # 最近交易日"，而当日数据此时尚未入库 → last(09-14) < latest_td(09-15)
    # ⇒ 全市场 4993 只全被判"断档"走 days=260 全量路径，--days 20 的轻量
    # 盘前/竞价任务实际耗时 ≈53 分钟 > timeout 45 分钟被 cancel（或网络异常
    # 抛 failure），第 8 步「构建+推送」整步 skipped ⇒ 用户全天零推送。
    # 正确锚 = 库中已有的最新交易日：库已跟上上一交易日 ⇒ 只补近端尾巴。
    import datetime as _dt
    last_dates = dict(con.execute(
        "SELECT code, MAX(date) FROM klines WHERE code!='sh000001' "
        "GROUP BY code").fetchall())
    # 今日应达交易日（用于文案与就绪判断，不作断档锚）
    _d = _dt.date.fromisoformat(today)
    while not holiday_cal.is_trade_day(_d.isoformat()):
        _d -= _dt.timedelta(days=1)
    due_td = _d.isoformat()
    # 断档锚：库中最新日期与「上一交易日」取较新者——当日未入库不算断档。
    db_latest = max(last_dates.values()) if last_dates else ""
    prev_d = _dt.date.fromisoformat(due_td)
    prev_d -= _dt.timedelta(days=1)
    while not holiday_cal.is_trade_day(prev_d.isoformat()):
        prev_d -= _dt.timedelta(days=1)
    latest_td = max(db_latest, prev_d.isoformat())
    inc_codes, full_codes = [], []
    for c in codes:
        last = last_dates.get(("sh" if c.startswith("6") else "sz") + c)
        (inc_codes if last and last >= latest_td else full_codes).append(c)
    print(f"[fetch] universe={len(codes)} 增量={len(inc_codes)} 全量={len(full_codes)}"
          f" @ {today}（断档锚 {latest_td} / 库最新 {db_latest or '空'}）")
    pfx_of = lambda c: "sh" if c.startswith("6") else "sz"  # noqa: E731
    batch = {}
    # 轻量任务（--days 20 及以下）全量拉也封顶 40 根，避免冷库/长假期后
    # 首次补数把盘前任务拖成 53 分钟超时。历史补数请显式 --days 260。
    full_days = min(days, 40) if days <= 20 else days
    if inc_codes:
        batch.update(kline_batch([(c, pfx_of(c)) for c in inc_codes],
                                 days=min(days, 20), con=con))
    if full_codes:
        batch.update(kline_batch([(c, pfx_of(c)) for c in full_codes],
                                 days=full_days, con=con))
    written = 0
    idx_codes = [c for c in codes if c in batch]
    for code in idx_codes:
        rows = batch[code]
        flags = corp_action_scan(rows)
        if len(flags) > 50:
            print(f"[warn] {code} 单日>50 只跳变 = 市场级 qfq 基准切换，只披露不排除")
        full = ("sh" if code.startswith("6") else "sz") + code
        written += upsert_klines(con, full, rows)
        # 换手率落入 klines（区间池 MIN_TURN 用近20日均换手）
        turn = universe[code].get("turn")
        if turn:
            con.execute("UPDATE klines SET turn=? WHERE code=? AND date=?",
                        (turn, full, today))
    if "000001" in batch:
        upsert_klines(con, "sh000001", batch["000001"])
    else:
        idx_batch = kline_batch([("000001", "sh")], days=days, con=con)
        if "000001" in idx_batch:
            upsert_klines(con, "sh000001", idx_batch["000001"])
    # M03 量纲修复：真实流通股本 = 流通市值÷股价（不可用换手率反推）。
    # 腾讯K线量为「手」：q=量/股本 ≈ 换手率/100 <0.01 正常；源异常返「股」→ q>0.01
    float_shares = {}
    for c, v in universe.items():
        try:
            if v.get("fmv") and v.get("price") and v["price"] > 0:
                float_shares[c] = v["fmv"] / v["price"]
        except Exception:  # noqa: BLE001
            continue
    factor, records = quality.repair_volume_units(
        [(c, batch[c][-1][5]) for c in idx_codes if batch[c]],
        float_shares, rule_version=quality.RULE_VERSION)
    if factor != 1.0:
        for c, _v in [(c, batch[c][-1][5]) for c in idx_codes if batch[c]]:
            con.execute("UPDATE klines SET v=v/? WHERE code=? AND date=?",
                        (factor, ("sh" if c.startswith("6") else "sz") + c,
                         today))
        for r in records:
            r["date"] = today
        quality.log_repairs(con, records)
        print(f"[quality] 量纲失灵修复 ÷{factor}，记录 {len(records)} 条")
    # 全市场快照落库（名称/成交额/换手/流通市值——build 前置过滤的数据基础）
    con.executemany(
        "INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
        [(today, ("sh" if c.startswith("6") else "sz") + c,
          v.get("name", ""), v.get("price"), v.get("pct"), v.get("amt"),
          v.get("turn"), v.get("fmv")) for c, v in universe.items()])
    # N02 批次元数据
    rec = quality.batch_record(
        source="push2delay+ifzq/em", trade_date=today,
        caliber="qfq日K,元,股,流通市值", quality=level,
        extra={"universe": len(codes), "fetched": len(idx_codes),
               "incremental": len(inc_codes), "full": len(full_codes)})
    quality.write_batch_meta(con, rec)
    # K线缺口自愈：今日半根 / 历史空洞 → 隔离+删除+腾讯源重补
    heal = gapfill.self_heal(con, idx_codes, today)
    if heal.get("repaired"):
        print(f"[gapfill] 缺口自愈：{heal['targets']} → "
              f"{heal.get('written', {})}")
    con.commit()
    # fetch_stats.json（#605-⑦：跨日残留由消费方校验 date==今日）
    stats = {"date": today, "is_trading_day": len(idx_codes) > 0,
             "universe": len(codes), "fetched": len(idx_codes),
             "rows_written": written, "gap_heal": heal,
             "guards_health": core.guards_health()}
    path = os.path.join(core.CACHE_DIR, "fetch_stats.json")
    os.makedirs(core.CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(f"[fetch] written rows={written} stats={stats['fetched']}/{len(codes)}")
    return stats


def is_trading_day_today(con=None):
    """数据新鲜度护栏（#605-⑦）：fetch_stats 必须校验 date==今日，
    跨日残留回退启发式（今日 ∈ trade_calendar）。"""
    con = con or get_conn()
    path = os.path.join(core.CACHE_DIR, "fetch_stats.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            stats = json.load(f)
        if stats.get("date") == today_str():     # 必须校验，跨日残留不可信
            return bool(stats.get("is_trading_day"))
    # 回退启发式：今日出现在上证指数日历中
    return today_str() in trade_calendar(con)


def data_ready_for(con, date):
    """build 就绪判断（#605-⑦ 的正确语义）：
    ① date 必须是真实交易日；② date 当日K线已入库；
    ③ fetch_stats 不早于 date（周六抓到的周五数据 → 可复盘周五）。
    返回 (ok, reason)。"""
    if date not in trade_calendar(con):
        return False, "非交易日"
    n = con.execute("SELECT COUNT(*) FROM klines WHERE date=?",
                    (date,)).fetchone()[0]
    if n == 0:
        return False, f"{date} 无K线数据（先跑 fetch_daily）"
    path = os.path.join(core.CACHE_DIR, "fetch_stats.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            stats = json.load(f)
        if stats.get("date") and stats["date"] < date:
            return False, (f"fetch 数据陈旧（{stats['date']} < {date}），"
                           "先重新抓取")
    return True, "K线就绪"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=260)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="非交易日手动补数（build 侧守门不受影响）")
    a = ap.parse_args()
    fetch_daily(days=a.days, limit=a.limit, force=a.force)
