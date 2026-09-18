# -*- coding: utf-8 -*-
"""法定节假日交易日历（吸收自原项目 trade_calendar.py）。

为什么需要：主调度 cron 写的是 `* * 1-5`（周一至周五），法定节假日照样点火。
若不拦截，国庆/春节期间会连续多日把「节前那根K线」当作『今日复盘』推给用户。

规则（沪深交易所）：
    交易日 = 周一~周五 且 不在国务院法定放假期间
    ⚠ 调休补班日（周六上班）A股不开市，故只认周一~周五。

安全设计（宁可多推、不可漏推）：
    未收录年份 → 一律视为交易日，绝不因日历缺失而漏掉真实交易日的推送。
    每年国务院发布次年放假安排后（通常 11 月），把新一年补进 HOLIDAYS
    并加入 COVERED_YEARS。

数据来源：国务院办公厅《关于2026年部分节假日安排的通知》国办发明电〔2025〕7号。
"""
import datetime

COVERED_YEARS = {2026}

HOLIDAYS = {
    # 元旦：1月1日(周四)至3日(周六)放假调休，共3天。1月4日(周日)上班。
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节：2月15日(周日)至23日(周一)放假调休，共9天。2月14日(周六)、2月28日(周六)上班。
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    # 清明节：4月4日(周六)至6日(周一)放假，共3天。
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节：5月1日(周五)至5日(周二)放假调休，共5天。5月9日(周六)上班。
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午节：6月19日(周五)至21日(周日)放假，共3天。
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋节：9月25日(周五)至27日(周日)放假，共3天。
    "2026-09-25", "2026-09-26", "2026-09-27",
    # 国庆节：10月1日(周四)至7日(周三)放假调休，共7天。
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
}


def _to_date(d=None):
    if d is None:
        tz = datetime.timezone(datetime.timedelta(hours=8))
        return datetime.datetime.now(tz).date()
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    return datetime.date.fromisoformat(str(d)[:10])


def is_trade_day(d=None):
    """d 为 None 时判断『北京时间今天』是否为沪深交易日。"""
    day = _to_date(d)
    if day.weekday() >= 5:
        return False
    if day.year not in COVERED_YEARS:
        return True                  # 未收录年份：保守视为交易日，绝不漏推
    return day.isoformat() not in HOLIDAYS


def why_closed(d=None):
    day = _to_date(d)
    if day.weekday() >= 5:
        return "周末休市"
    if day.year in COVERED_YEARS and day.isoformat() in HOLIDAYS:
        return "法定节假日休市"
    return ""


def calendar_covered(d=None):
    return _to_date(d).year in COVERED_YEARS


# ---------------------------------------------------------------------------
# 交易时段守门（用户 2026-09-18 需求：「需要考虑周末和节假日，今天已经不在
# 交易时间了又开始购买」）
# ---------------------------------------------------------------------------
# ★ 为什么必须单独做时段判断，而不是只看日期：
#   `close` 这班定时器是 **15:22**（收盘后 22 分钟），`pre` 是 08:50（开盘前）。
#   如果只判"今天是不是交易日"，15:22 那班就会拿**当日收盘价**去建仓——
#   用户看到的是"收盘了还在买"。日期对、时段错，是两件不同的事。
#   沪深交易时段：集合竞价 09:15–09:25，连续竞价 09:30–11:30 / 13:00–15:00。
#   取并集 09:15–11:30 + 13:00–15:00 作为「可下单窗口」。
SESSIONS = ((9, 15, 11, 30), (13, 0, 15, 0))
CST = datetime.timezone(datetime.timedelta(hours=8))


def now_cst():
    """北京时间当前时刻。

    ⚠️ CI runner 是 UTC：`datetime.now()` 在 15:22（北京）拿到的是 07:22。
    任何"现在几点"的判断都必须显式换算到 UTC+8，否则时段门控形同虚设。
    """
    return datetime.datetime.now(CST)


def in_trading_session(now=None):
    """当前是否处于「可下单」交易时段（同时要求当天是交易日）。

    ★ 必须 `astimezone(CST)` 归一化：调用方可能给一个 UTC 表示的同一时刻
    （CI runner 的 `datetime.now()` 就是 UTC）。直接用 `t.hour` 读到的会是
    凌晨 2 点而不是北京 10 点 —— 闸门于是在正确的时间点放行/拦截全反。
    """
    t = (now or now_cst()).astimezone(CST)
    if not is_trade_day(t):
        return False
    hm = t.hour * 60 + t.minute
    return any(a * 60 + b <= hm <= c * 60 + d for a, b, c, d in SESSIONS)


def session_note(now=None, today=None):
    """不处于交易时段时的**人类可读原因**（直接进推送正文）。

    `today` 用于区分"日期本身是不是交易日"与"时刻是否在盘中"——
    两者会给出完全不同的解释，混在一起会让读者以为系统坏了。
    """
    t = (now or now_cst()).astimezone(CST)
    hhmm = t.strftime("%H:%M")
    if today is not None and not is_trade_day(today):
        return f"{today} {why_closed(today)}，不建仓"
    if not is_trade_day(t):
        return f"{t.strftime('%Y-%m-%d')} {why_closed(t)}，不建仓"
    hm = t.hour * 60 + t.minute
    if hm < 9 * 60 + 15:
        return f"北京时间 {hhmm}，开盘前（09:15 才开始撮合），不建仓"
    if 11 * 60 + 30 < hm < 13 * 60:
        return f"北京时间 {hhmm}，午间休市（11:30–13:00），不建仓"
    if hm > 15 * 60:
        return f"北京时间 {hhmm}，已收盘（15:00 后不再撮合），不建仓"
    return f"北京时间 {hhmm} 非交易时段，不建仓"
