# -*- coding: utf-8 -*-
"""分析引擎：趋势 / 箱体波段 / 快箱体节奏 / 连板空间计划 / 回马枪 / Kronos / 近端买点。

全部阈值参数对齐《需求规格书 v2》第三章，未给出实证前不得"优化"。
bars 格式: [date, open, close, high, low, volume] 升序。
"""
import math
from collections import deque


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def ma20_series(closes):
    out, q = [], deque()
    s = 0.0
    for c in closes:
        q.append(c)
        s += c
        if len(q) > 20:
            s -= q.popleft()
        out.append(s / len(q) if q else None)
    return out


def atr(rows, n=14):
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i][3], rows[i][4], rows[i - 1][2]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    return sum(trs[-n:]) / min(n, len(trs))


def pct(chg, base):
    return (chg / base * 100) if base else 0.0


# ---------------------------------------------------------------------------
# 3.1 竞价纪律 + 连板空间计划 ladderplan
# ---------------------------------------------------------------------------

def auction_discipline(streak, gap_pct):
    """竞价纪律（118万根K线回测口径）。返回 (跟进?, 观望?)。
    高开≥阈值跟进 / 低开≤-2% 放弃 / st=2 需≥5% 强高开。"""
    if gap_pct <= -2.0:
        return False, False          # 放弃
    if streak >= 3:
        return gap_pct >= 2.0, gap_pct < 2.0
    if streak == 2:
        return gap_pct >= 5.0, 2.0 <= gap_pct < 5.0   # 2-5% 弱高开胜率仅14.3%
    return gap_pct >= 2.0, abs(gap_pct) < 2.0         # 首板：平开观望


_PRIOR = {1: {"exp": 2.0, "reach10": 0.18}, 2: {"exp": 3.5, "reach10": 0.26},
          3: {"exp": 5.0, "reach10": 0.30}, 4: {"exp": 6.0, "reach10": 0.32},
          5: {"exp": 4.0, "reach10": 0.28}}
_BUCKET_BLEND = 0.65
_MIN_N = 20
LOW_OPEN_PCT = -0.1  # 低开阈值（与败因否决器同口径）


def ladderplan_plan(streak, close, buckets=None):
    """连板空间计划：先验基线 + 真实分桶 65% 混合。"""
    pr = _PRIOR.get(min(streak, 5), _PRIOR[5])
    exp, reach10 = pr["exp"], pr["reach10"]
    if buckets and buckets.get("n", 0) >= _MIN_N:
        exp = buckets["exp"] * _BUCKET_BLEND + exp * (1 - _BUCKET_BLEND)
        reach10 = buckets["reach10"] * _BUCKET_BLEND + reach10 * (1 - _BUCKET_BLEND)
    buy_low, buy_high = close * 0.995, close * 1.03
    t1, t2 = close * (1 + exp / 100 * 0.5), close * (1 + exp / 100)
    # 间距守卫：目标价下沿 < 买区上沿×1.06 → 整体上移
    if t1 < buy_high * 1.06:
        shift = buy_high * 1.06 / t1
        t1, t2 = t1 * shift, t2 * shift
    return {"buy_low": buy_low, "buy_high": buy_high,
            "t1": t1, "t2": t2, "stop": close * 0.92,
            "mfe_levels": [5, 10, 15, 20], "exp": exp, "reach10": reach10}


# ---------------------------------------------------------------------------
# 3.2 趋势引擎 screen_uptrend（全市场扫描）+ 3.12 近端买点 zones
# ---------------------------------------------------------------------------

def screen_uptrend(rows, streak=0):
    """准入双通道（主通道进攻型 / 缓坡通道慢牛）。rows 最近 ≥25 根。
    返回 dict(cand / None) + worth 评分。"""
    if len(rows) < 25:
        return None
    closes = [r[2] for r in rows]
    vols = [r[5] for r in rows]
    ma5, ma10, ma20 = sma(closes, 5), sma(closes, 10), sma(closes, 20)
    if not (ma5 and ma10 and ma20) or not (ma5 > ma10 > ma20):
        return None
    close = closes[-1]
    if close <= ma5:
        return None
    last5 = rows[-5:]
    dailies = [pct(rows[i][2] - rows[i - 1][2], rows[i - 1][2])
               for i in range(len(rows) - 4, len(rows))]
    avg_daily = sum(dailies) / 5
    up_days = sum(1 for d in dailies if d > 0)
    flat_days = sum(1 for d in dailies if abs(d) < 1)
    if avg_daily < 1.0 or up_days < 3 or flat_days > 2:
        return None
    # MA20 斜率（20日前）与 20 日涨幅
    m20 = ma20_series(closes)
    slope20 = pct(m20[-1] - m20[-20], m20[-20]) if m20[-20] else 0.0
    chg20 = pct(close - closes[-21], closes[-21]) if len(closes) > 21 else 0.0
    main_channel = (avg_daily >= 2.0 and up_days >= 4 and flat_days <= 1)
    slow_channel = (slope20 >= 1.5 and chg20 >= 8)
    if not (main_channel or slow_channel):
        return None
    # 双态：加速 / 放缓
    accel = avg_daily / (sum(dailies[-20:]) / 20 if len(dailies) >= 20 else avg_daily)
    trend_state = "加速上行" if accel > 1.45 else "增速放缓"
    if streak:  # 当日涨停归连板池
        return None
    # 评分（0-100）
    score = 28.0
    score += min(34.0, max(0.0, (avg_daily - 1) / 4 * 34))
    score += up_days / 5 * 13
    score += min(9.0, max(0.0, slope20 / 1.5 * 9))
    dev = pct(close - ma20, ma20)
    if 5 <= dev <= 45:
        score += 14
    elif dev > 45:
        score += max(0.0, 14 - (dev - 45) * 0.5)
    else:
        score -= 6
    if len(vols) >= 20 and vols[-6] > 0:
        vol_ratio = sum(vols[-5:]) / 5 / (sum(vols[-20:-5]) / 15)
        if 1 <= vol_ratio <= 3:
            score += 10
        elif vol_ratio > 5:
            score -= 6
    if slow_channel:
        score += min(10.0, slope20 / 3 * 10)
    score += 5 if trend_state == "加速上行" else -5
    return {"close": close, "avg_daily": avg_daily, "up_days": up_days,
            "slope20": slope20, "trend_state": trend_state,
            "worth_score": max(0.0, min(100.0, score))}


def entry_plan(bars, box_low=None):
    """近端买点阶梯（zones.py 口径）。返回 zones + 四态判定。"""
    closes = [b[2] for b in bars]
    ref = max(sma(closes, 5), sma(closes, 10))
    close = closes[-1]
    low3 = min(b[4] for b in bars[-3:])
    anchor = min(ref, close - 0.8 * atr(bars))
    now_low = max(low3, ref * 0.97)
    now_zone = [now_low, close * 1.005]
    pull_low = min(min(sma(closes, 5), sma(closes, 10)) * 0.99, low3)
    pull_zone = [pull_low, max(anchor * 1.01, sma(closes, 5) * 1.005)]
    t3 = [box_low * 0.99, box_low * 1.05] if box_low else pull_zone
    stop = box_low * 0.94 if box_low else min(low3, close * 0.94)
    if close <= stop:
        state, action = "已破位", "禁买"
    elif close <= ref * 1.03:
        state, action = "可买", "现在买"
    elif close <= ref * 1.06:
        state, action = "微超", "小仓试"
    elif close <= ref * 1.12:
        state, action = "等回踩", "等回踩"
    else:
        state, action = "过热", "勿追"
    return {"ref": ref, "now_zone": now_zone, "pull_zone": pull_zone,
            "deep_zone": t3, "stop": stop, "state": state, "action": action}


# ---------------------------------------------------------------------------
# 3.3 箱体波段 detect_stage_bottom + 3.4 快箱体节奏 classify_box_speed
# ---------------------------------------------------------------------------

BOX_WIN = 25
MIN_FMV = 15e8
MIN_TURN = 0.5
MIN_MOVE = 1.0
MIN_UPSIDE = 8.0


def _interp_quantile(vals, q):
    """线性插值分位（P15/P85 口径）。"""
    s = sorted(vals)
    if not s:
        return None
    pos = (len(s) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def detect_stage_bottom(rows, turn20=None, fmv=None):
    """箱体波段检出。turn20=近20日均换手%，fmv=流通市值(元)。"""
    if len(rows) < BOX_WIN:
        return None
    win = rows[-BOX_WIN:]
    closes = [r[2] for r in win]
    box_low = _interp_quantile(closes, 0.15)
    box_high = _interp_quantile(closes, 0.85)
    if box_low <= 0 or not (box_low * 1.05 <= box_high <= box_low * 1.35):
        return None
    close = closes[-1]
    touches = sum(1 for r in win if r[4] <= box_low * 1.015)
    tops = sum(1 for r in win if r[3] >= box_high * 0.985)
    if touches < 3 or tops < 3:
        return None
    if close > box_low * 1.12:                 # 不追箱体高位
        return None
    if close < box_low * 0.99:                 # 破位容差 1%，不接飞刀
        return None
    m20 = ma20_series([r[2] for r in rows])
    if len(m20) > 20 and m20[-1] < m20[-20] * 0.985:   # 闸1 MA20 斜率
        return None
    third = BOX_WIN // 3
    avg_front = sum(closes[:third]) / third
    avg_back = sum(closes[-third:]) / third
    if avg_back < avg_front * 0.97:            # 闸2 均价衰减（阴跌途中）
        return None
    # 30 日阴跌闸：三条件全中才拦
    c30 = [r[2] for r in rows[-30:]]
    net30 = pct(c30[-1] - c30[0], c30[0])
    has_big_yang = any(pct(c30[i] - c30[i - 1], c30[i - 1]) >= 3 for i in range(1, 30))
    last5 = sum(pct(c30[i] - c30[i - 1], c30[i - 1]) for i in range(-4, 0))
    if net30 <= -8 and not has_big_yang and last5 <= 2:
        return None
    if turn20 is not None and turn20 < MIN_TURN:
        return None
    dailies = [abs(pct(win[i][2] - win[i - 1][2], win[i - 1][2]))
               for i in range(1, BOX_WIN)]
    if sum(dailies) / len(dailies) < MIN_MOVE:  # 低位死水
        return None
    upside = pct(box_high - close, close)
    if upside < MIN_UPSIDE:
        return None
    pos = max(0.0, min(1.0, (close - box_low) / (box_high - box_low)))
    worth = (30 * (1 - pos) + min(12, touches * 1.2)
             + min(8, tops * 0.8) + min(20, upside))
    cand = {"pool": "区间", "close": close, "box_low": box_low,
            "box_high": box_high, "touches": touches, "tops": tops,
            "upside": upside, "worth": worth,
            "buy_low": box_low * 0.99, "buy_high": box_low * 1.05,
            "sell_low": box_high * 0.97, "sell_high": box_high,
            "stop": box_low * 0.94}
    cand.update(classify_box_speed(rows))
    return cand


FAST_BOX_WIN = 15
FAST_NET_FLOOR = 6.0
FAST_SURGE = 5.0
FAST_PULLBACK_MAX = 0.50


def classify_box_speed(rows):
    """#601-C 快箱体三要素（15 日窗，全部满足才标「快」）。

    回撤口径（勿改）：用主升后到窗末的**最深回踩** min(c15[hi_idx:])，
    而非当前回撤——N 字二波第二波确认后当前回撤自然收窄，无法区分。
    """
    c15 = [r[2] for r in rows[-FAST_BOX_WIN:]]
    net15 = pct(c15[-1] - c15[0], c15[0])
    hi_idx = max(range(len(c15)), key=lambda i: c15[i])
    max_day = max(pct(c15[i] - c15[i - 1], c15[i - 1]) for i in range(1, len(c15)))
    three = (net15 >= FAST_NET_FLOOR and max_day >= FAST_SURGE)
    speed, hold_days, cycle_hint = "常规", 20, "慢节奏箱体"
    if three:
        min_after = min(c15[hi_idx:])
        deepest = pct(min_after - c15[hi_idx], c15[hi_idx])  # 最深回踩（负值幅度）
        pull_ratio = abs(deepest) / net15 if net15 > 0 else 1.0
        if pull_ratio <= 0.20:
            speed, hold_days, cycle_hint = "快箱体", 8, "急拉后高位横住"
        elif pull_ratio <= FAST_PULLBACK_MAX and c15[-1] >= min_after:
            speed, hold_days, cycle_hint = "N字二波", 12, "主升→浅回踩→二波"
    return {"speed": speed, "hold_days": hold_days, "cycle_hint": cycle_hint}


# ---------------------------------------------------------------------------
# 3.5 涨停回马枪 screen_pullback_relay
# ---------------------------------------------------------------------------

def screen_pullback_relay(rows):
    """涨停 D0 → 缩量回调不破关键位 → 放量再启动。"""
    if len(rows) < 15:
        return None
    # 找最近 10 日内的涨停 D0
    d0 = None
    for i in range(len(rows) - 10, len(rows) - 1):
        if i > 0 and pct(rows[i][2] - rows[i - 1][2], rows[i - 1][2]) >= 9.5:
            d0 = i
    if d0 is None:
        return None
    close = rows[-1][2]
    d0_close = rows[d0][2]
    if close < d0_close * 0.92:     # 破关键位
        return None
    after = rows[d0 + 1:]
    if len(after) < 2:
        return None
    peak = max(r[3] for r in after[:-1])
    retreat = pct(peak - min(r[4] for r in after[:-1]), peak)
    vols = [r[5] for r in rows]
    base_vol = sum(vols[-8:-1]) / 7 if len(vols) >= 8 else vols[-2]
    vol_ratio = vols[-1] / base_vol if base_vol else 0
    if vol_ratio < 1.25:            # 放量再启动门槛
        return None
    if pct(rows[-1][2] - rows[-2][2], rows[-2][2]) >= 9.5:
        return None                 # 当日涨停归连板池
    # 评分：50 底盘 + 量能25 + 回调质量25
    vq = max(0.0, 25 * (1 - retreat / 8)) if retreat is not None else 0
    ve = min(25.0, (vol_ratio - 1.25) / (3 - 1.25) * 25)
    swing_base = 50 + ve + vq
    return {"pool": "波段", "close": close, "retreat": retreat,
            "vol_ratio": vol_ratio, "worth": swing_base,
            "buy_low": close * 0.99, "buy_high": close * 1.02,
            "sell_low": close * 1.08, "sell_high": close * 1.15,
            "stop": min(rows[d0][4], close * 0.94),
            "speed": "常规", "hold_days": 10, "cycle_hint": "波段节奏"}


# ---------------------------------------------------------------------------
# 3.6 Kronos 结构健康度 kronos_lite（K线 tokenizer 思想纯 Python 蒸馏）
# ---------------------------------------------------------------------------

def _token(r, prev_c):
    o, c, h, l = r[1], r[2], r[3], r[4]
    body = abs(c - o)
    rng = (h - l) or 1e-9
    direction = 1 if c >= o else -1
    strength = 0 if body / rng < 0.3 else (1 if body / rng < 0.7 else 2)
    up_shadow = (h - max(o, c)) / rng
    dn_shadow = (min(o, c) - l) / rng
    shadow = (0 if max(up_shadow, dn_shadow) < 0.15 else
              1 if up_shadow > dn_shadow else 2)
    s1 = (direction, strength, shadow)
    r_rel = math.tanh(max(-0.10, min(0.10, (c / prev_c - 1))) / 0.05)
    s2 = int((r_rel + 1) / 0.25)  # 8 档
    return (s1, s2)


def kronos_lite(rows):
    """输出 kronos_score ∈ [0,100]。低熵=结构化/可预测（Kronos thesis）。"""
    if len(rows) < 30:
        return 50.0
    toks = [_token(rows[i], rows[i - 1][2]) for i in range(1, len(rows))]
    # pattern_entropy：token 序列信息熵
    freq = {}
    for t in toks:
        freq[t] = freq.get(t, 0) + 1
    n = len(toks)
    ent = -sum((f / n) * math.log(f / n, 2) for f in freq.values())
    max_ent = math.log(n, 2) or 1
    ent_norm = ent / max_ent
    # micro_pattern_edge：最近 5 日形态在历史中的后续方向胜率（无未来函数）
    cur = tuple(toks[-5:])
    wins = tot = 0
    for i in range(len(toks) - 6):
        if tuple(toks[i:i + 5]) == cur:
            tot += 1
            if rows[i + 6][2] >= rows[i + 5][2]:
                wins += 1
    edge = (wins / tot) if tot >= 5 else 0.5
    # vol_regime：5日/20日收益标准差比
    rets = [rows[i][2] / rows[i - 1][2] - 1 for i in range(1, len(rows))]
    def std(v):
        m = sum(v) / len(v)
        return math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
    vr = std(rets[-5:]) / (std(rets[-20:]) or 1e-9)
    # self_sim：近 5 日同向 K 线占比
    dirs = [1 if r[2] >= r[1] else -1 for r in rows[-5:]]
    self_sim = max(dirs.count(1), dirs.count(-1)) / 5
    score = (50
             + (0.5 - ent_norm) * 60          # 低熵加分
             + (edge - 0.5) * 40              # 形态边际
             + (1.0 - min(vr, 2.0) / 2.0) * 10
             + (self_sim - 0.5) * 10)
    return max(0.0, min(100.0, score))
