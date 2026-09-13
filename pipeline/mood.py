# -*- coding: utf-8 -*-
"""涨停池情绪统计：晋级率 / 炸板率 / 情绪分 → env_bias 实时接入。

口径说明（无逐笔数据时的日K近似，阈值按板块涨幅制度区分）：
- 涨停判定：收盘 ≥ 昨收×(1+涨停幅)×0.998（主板 10% / 创业板科创板 20% / 北交所 30%）
- 炸板：盘中高点触板但收盘未封住
- 晋级率：今日连板(≥2板)家数 ÷ 昨日涨停家数
- 情绪分 ∈ [0,100]：涨停家数35 + 连板高度20 + 晋级率25 + (1-炸板率)20
"""
from .core import prev_trading_day


def limit_ratio_for(code):
    if code.startswith(("30", "68")):
        return 0.19
    if code.startswith(("4", "8")):
        return 0.29
    return 0.095


def is_limit_up(code, close, prev_close):
    return prev_close > 0 and close >= prev_close * (1 + limit_ratio_for(code)) * 0.998


def touched_limit(code, high, prev_close):
    return prev_close > 0 and high >= prev_close * (1 + limit_ratio_for(code)) * 0.998


def compute_mood(con, date, write_zt_pool=True):
    """返回 dict(zt_count, max_streak, promote_rate, zhaban_rate, emotion)
    或 None（当日无K线）。涨停池同步落库供次日连板高度递推。"""
    prev = prev_trading_day(con, date)
    rows = con.execute(
        "SELECT code, h, c FROM klines WHERE date=? AND code!='sh000001'",
        (date,)).fetchall()
    if not rows:
        return None
    prevc = dict(con.execute(
        "SELECT code, c FROM klines WHERE date=?", (prev,)).fetchall()) if prev else {}
    prev_streaks = dict(con.execute(
        "SELECT code, streak FROM zt_pool WHERE date=?", (prev,)).fetchall()) if prev else {}
    prev_zt = len(prev_streaks)
    zt, zhaban, zt_rows = 0, 0, []
    for code, h, c in rows:
        num = code[2:] if code[:2] in ("sh", "sz") else code
        pc = prevc.get(code)
        if not pc:
            continue
        if touched_limit(num, h, pc) and not is_limit_up(num, c, pc):
            zhaban += 1
        if is_limit_up(num, c, pc):
            zt += 1
            streak = prev_streaks.get(code, 0) + 1
            zt_rows.append((date, code, streak, ""))
    if write_zt_pool and zt_rows:
        con.executemany("INSERT OR REPLACE INTO zt_pool VALUES(?,?,?,?)", zt_rows)
        con.commit()
    n2 = sum(1 for _, _, s, _ in zt_rows if s >= 2)
    max_streak = max([s for _, _, s, _ in zt_rows], default=0)
    promote = (n2 / prev_zt) if prev_zt else 0.0
    zhaban_rate = (zhaban / (zt + zhaban)) if (zt + zhaban) else 0.0
    emotion = (min(1.0, zt / 80) * 35 + min(1.0, max_streak / 6) * 20
               + promote * 25 + (1 - zhaban_rate) * 20)
    return {"date": date, "zt_count": zt, "zhaban_count": zhaban,
            "max_streak": max_streak, "promote_rate": round(promote, 4),
            "zhaban_rate": round(zhaban_rate, 4),
            "emotion": round(emotion, 1)}
