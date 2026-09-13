# -*- coding: utf-8 -*-
"""K线缺口自愈（吸收自原项目 backfill.py / repair_gap.py 的缺口检测思想）。

背景：盘后 fetch 依赖快照+K线接口，若某日未运行或被限流，会留下整日空洞
或「半根K线」（仅少量股票有行）。空洞会让连板数跨日误算、晋级率被机械
压低，进而污染情绪分与 MA。

检测（每轮 fetch 后调用）：
  今日行数 < 60% × 快照总数 → 判定「盘中半根」，删除当日全部行并重拉；
  近 N 日存在「行数 < 60% × 当日中位数」的历史空洞 → 列入修复清单。
修复：用腾讯源（不与东财争令牌）统一重拉近端日K，只写目标日期。
"""
import json
import os

from . import core
from .core import get_conn, today_str

GAP_RATIO = 0.60       # 行数 < 60% × 中位数 → 残缺
LOOKBACK_DAYS = 12     # 近 12 个交易日扫描窗口
SLOW_PATH_DAYS = 12    # 慢路径回补窗口（自然日）


def detect_gaps(con, today=None):
    """返回残缺日期清单 [(date, rows, median_rows)]。"""
    today = today or today_str()
    rows_by_date = dict(con.execute(
        "SELECT date, COUNT(*) FROM klines WHERE code != 'sh000001' "
        "GROUP BY date ORDER BY date DESC LIMIT ?", (LOOKBACK_DAYS * 3,)).fetchall())
    dates = sorted(rows_by_date.keys())[-LOOKBACK_DAYS:]
    if not dates:
        return []
    median = sorted(rows_by_date.values())[len(rows_by_date) // 2]
    gaps = []
    for d in dates:
        n = rows_by_date[d]
        if n < median * GAP_RATIO:
            gaps.append((d, n, median))
    return gaps


def repair_gaps(con, targets, universe_codes, days=15):
    """对残缺日期：删当日行 → 腾讯源重拉近 N 日 → 只写目标日期。
    返回 {date: written_rows}。"""
    targets = set(targets)
    if not targets or not universe_codes:
        return {}
    # 删除残缺行（先隔离备份——M04 纪律）
    from .quality import quarantine_records
    try:
        quarantine_records(con, sorted(targets))
    except Exception:  # noqa: BLE001
        pass
    for d in targets:
        con.execute("DELETE FROM klines WHERE date=? AND code!='sh000001'", (d,))
    con.commit()
    pairs = [(c, "sh" if c.startswith("6") else "sz") for c in universe_codes]
    batch = core.kline_batch(pairs, days=days, con=con)
    written = {}
    for num, rows in batch.items():
        full = ("sh" if num.startswith("6") else "sz") + num
        for d, o, c, h, l, v in rows:
            if d in targets:
                prev = None
                con.execute(
                    "INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (full, d, o, h, l, c, v, None, None, None))
                written[d] = written.get(d, 0) + 1
    con.commit()
    return written


def self_heal(con, universe_codes, today=None):
    """fetch 后调用：检测今日半根 + 历史空洞 → 修复。返回修复摘要。
    非交易日（周末/节假日）不把「今日」当缺口——今天本就不该有K线。"""
    from . import trade_calendar as holiday_cal
    today = today or today_str()
    targets = []
    if holiday_cal.is_trade_day(today):
        total_snap = con.execute(
            "SELECT COUNT(*) FROM snapshot WHERE date=?", (today,)).fetchone()[0]
        today_rows = con.execute(
            "SELECT COUNT(*) FROM klines WHERE date=? AND code!='sh000001'",
            (today,)).fetchone()[0]
        if total_snap and today_rows < total_snap * GAP_RATIO:
            targets.append(today)      # 盘中半根 → 删除重补
    for d, n, median in detect_gaps(con, today):
        if d not in targets and holiday_cal.is_trade_day(d):
            targets.append(d)
    if not targets:
        return {"repaired": False, "targets": []}
    written = repair_gaps(con, targets, universe_codes)
    return {"repaired": True, "targets": targets, "written": written}
