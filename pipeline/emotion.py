# -*- coding: utf-8 -*-
"""十维情绪温度计（审计 三、全参数落地 + M05/M06/M07/M08）。

维度锚点与权重为【原版参数基线 V02】——保留数值，不认定为新版最优。
权重合计 1.32；情绪分 = Σ(维度分×权重) ÷ Σ有效权重（M05 加权平均，
缺失维度不参与、不补中性值 M07）。
反向指标（跌停/炸板家数）用"数值-分数"锚点分段线性插值（M06）。
情绪标签≠周期阶段：phase 综合变化方向/连续天数/梯队结构（M08）。
"""
import json
from datetime import datetime

from .core import prev_trading_day

TEN_DIMS = [
    {"key": "money_effect", "name": "赚钱效应",
     "anchors": [(-6, 0), (0, 50), (6, 100)], "w": 0.24},      # 全市场平均涨幅%
    {"key": "promote", "name": "连板晋级率",
     "anchors": [(4, 0), (14, 50), (30, 100)], "w": 0.18},     # %
    {"key": "zt_count", "name": "涨停总量",
     "anchors": [(15, 0), (55, 50), (120, 100)], "w": 0.14},   # 家
    {"key": "max_streak", "name": "空间高度",
     "anchors": [(2, 0), (4, 50), (8, 100)], "w": 0.12},       # 板
    {"key": "up_ratio", "name": "上涨家数占比",
     "anchors": [(22, 0), (50, 50), (78, 100)], "w": 0.12},    # %
    {"key": "dt_count", "name": "跌停家数",
     "anchors": [(0, 100), (8, 50), (30, 0)], "w": 0.08},      # 家（反向）
    {"key": "zha_count", "name": "炸板家数",
     "anchors": [(0, 100), (6, 50), (25, 0)], "w": 0.06},      # 家（反向）
    {"key": "amt_chg", "name": "成交额环比",
     "anchors": [(-18, 0), (0, 50), (18, 100)], "w": 0.12},    # %
    {"key": "seal_rate", "name": "封板率",
     "anchors": [(45, 0), (72, 50), (92, 100)], "w": 0.10},    # %
    {"key": "heat", "name": "市场热度",
     "anchors": [(0.8, 0), (1.0, 50), (1.3, 100)], "w": 0.16}, # 倍
]
CORE_KEYS = {"money_effect", "promote", "zt_count"}
MIN_COVERAGE = 0.60          # 有效权重覆盖率低于此 → 不用于策略加权（M07）
TOTAL_WEIGHT = sum(d["w"] for d in TEN_DIMS)   # = 1.32


def anchor_score(value, anchors):
    """M06：锚点分段线性插值（统一升序排列后插值，反向维度天然支持）。
    区间外按端点截断。"""
    pts = sorted(anchors)
    if value <= pts[0][0]:
        return float(pts[0][1])
    if value >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, s0), (x1, s1) in zip(pts, pts[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return float(s0)
            return s0 + (value - x0) / (x1 - x0) * (s1 - s0)
    return 50.0


def label(score):
    """3.3 市场分档（显示层基线）。"""
    if score >= 76:
        return "亢奋"
    if score >= 60:
        return "偏热"
    if score >= 45:
        return "均衡"
    if score >= 30:
        return "偏冷"
    return "冰点"


def market_phase(history, max_streak_now=None):
    """M08 周期定位：综合情绪分变化方向/连续天数/梯队结构。
    返回 启动/发酵/高潮/退潮/震荡/不可判。"""
    if len(history) < 2:
        return "不可判"
    scores = [s for _, s in history]
    cur = scores[-1]
    diff = cur - scores[-2]
    run = 1
    for i in range(len(scores) - 1, 0, -1):
        step = scores[i] - scores[i - 1]
        if diff >= 0 and step >= 0:
            run += 1 if i < len(scores) - 1 else 0
        elif diff < 0 and step < 0:
            run += 1 if i < len(scores) - 1 else 0
        else:
            break
    streak = max_streak_now or 0
    if cur < 30 and diff <= 0:
        return "冰点"
    if cur < 45 and diff > 0:
        return "启动"          # 冰点/偏冷后回升
    if cur >= 76 and diff > 0 and run >= 2 and streak >= 5:
        return "高潮"
    if diff > 0 and run >= 2 and 45 <= cur < 76:
        return "发酵"
    if diff < 0 and (run >= 2 or (scores[-2] >= 60 and cur < 45)):
        return "退潮"
    return "震荡"


def _raw_dims(con, date):
    """从库内数据计算十维原始值；不可得维度记 None（缺失不补 M07）。"""
    prev = prev_trading_day(con, date)
    rows = con.execute(
        "SELECT code, pct FROM klines WHERE date=? AND code!='sh000001'",
        (date,)).fetchall()
    pcts = [p for _, p in rows if p is not None]
    out = {}
    if pcts:
        n = len(pcts)
        out["money_effect"] = sum(pcts) / n
        out["up_ratio"] = sum(1 for p in pcts if p > 0) / n * 100
        out["dt_count"] = sum(1 for p in pcts if p <= -9.5)
    # 涨停/晋级/高度/炸板/封板（mood 口径，同源同算 → M09 口径统一）
    zt_rows = con.execute(
        "SELECT streak FROM zt_pool WHERE date=?", (date,)).fetchall()
    if zt_rows:
        out["zt_count"] = len(zt_rows)
        out["max_streak"] = max(s for s, in zt_rows)
        prev_zt = con.execute(
            "SELECT COUNT(*) FROM zt_pool WHERE date=?", (prev,)).fetchone()[0] \
            if prev else 0
        if prev_zt:
            out["promote"] = (sum(1 for s, in zt_rows if s >= 2) / prev_zt * 100)
    return out


def emotion_ten(con, date, extra_dims=None, write_log=True):
    """计算十维情绪分。extra_dims: 由调用方补充的维度值（如 zhaban/amt/heat）。

    返回 dict(score, label, phase, effective, coverage, qualified, parts)。
    qualified=False 时不得用于策略加权（M07）。
    """
    dims = _raw_dims(con, date)
    if extra_dims:
        for k, v in extra_dims.items():
            if v is not None:
                dims[k] = v
    parts, w_sum, s_sum, effective = {}, 0.0, 0.0, 0
    for d in TEN_DIMS:
        v = dims.get(d["key"])
        if v is None:                     # M07：缺失即缺失，不补中性值
            parts[d["key"]] = {"name": d["name"], "value": None,
                               "score": None, "ok": False}
            continue
        s = anchor_score(v, d["anchors"])
        parts[d["key"]] = {"name": d["name"], "value": round(v, 3),
                           "score": round(s, 1), "ok": True}
        w_sum += d["w"]
        s_sum += s * d["w"]
        effective += 1
    score = (s_sum / w_sum) if w_sum > 0 else 50.0     # M05 归一化
    coverage = w_sum / TOTAL_WEIGHT
    qualified = (coverage >= MIN_COVERAGE
                 and all(parts[k]["ok"] for k in CORE_KEYS))
    # 周期定位：近 6 日情绪分序列 + 当前梯队
    hist = [(r[0], r[1]) for r in con.execute(
        "SELECT date, score FROM emotion_log WHERE date<=? "
        "ORDER BY date DESC LIMIT 5", (date,))][::-1]
    hist.append((date, score))
    phase = market_phase(hist, dims.get("max_streak"))
    out = {"date": date, "score": round(score, 1), "label": label(score),
           "phase": phase, "effective": effective,
           "coverage": round(coverage, 3), "qualified": bool(qualified),
           "parts": parts}
    if write_log:
        con.execute(
            "INSERT OR REPLACE INTO emotion_log VALUES(?,?,?,?,?,?,?)",
            (date, out["score"], effective, out["coverage"],
             1 if qualified else 0, phase,
             json.dumps(parts, ensure_ascii=False)))   # 存完整维度对象
        con.commit()
    return out
