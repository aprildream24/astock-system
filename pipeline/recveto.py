# -*- coding: utf-8 -*-
"""推荐负反馈闭环·标注式否决器（吸收自原项目 recveto.py，480 条回测实证）。

败因确诊结论（原项目 tools 回测 2026-08）：
  * 整体 T+1 收红率仅 43%，均值 +0.57%
  * 最强正向信号：竞价不低开+核心龙头 82%/+6.13%；不低开+(缩量或龙头) 78%/+5.50%
  * 灾难信号：低开 → T+1 收红率仅 24% / 均值 -2.24%

口径（2026-08-27 用户指令）：高位票不一刀切——只降权+标注（V1），
仅 p_break≥90 的极端值才拦（VETO）。低开标 avoid 证据不变（G1）。
阈值滚动自校准：默认只记录建议，RECVETO_AUTO_CALIB=1 才自动覆盖。

本系统适配：p_break（断板概率）当前无直接数据源，默认 None →
veto 退化为放量/低开判定；数据齐后阈值自校准自动生效。
"""
import os

VETO_PB = 82
HARD_VETO_PB = 90
SHRINK_RATIO = 0.7      # day_vol_ratio < 0.7 视为缩量（安全子集）
LOW_OPEN = -0.1         # 竞价 open_pct < -0.1% 视为低开（与 ladderplan 同口径）
VETO_TAG = "高位风险"


def day_vol_ratio(vol_today, vols_hist):
    """当日成交量 / 前 5 日均量。缺失时 None（调用方按中性处理）。"""
    try:
        hist = [v for v in (vols_hist or [])[-5:] if v and v > 0]
        if not hist or not vol_today or vol_today <= 0:
            return None
        return round(vol_today / (sum(hist) / len(hist)), 3)
    except Exception:
        return None


def is_shrunk(ratio):
    if ratio is None:
        return False
    return ratio < SHRINK_RATIO


def veto(p_break=None, day_vol_ratio=None, yizi=False):
    """标注式风险判定。返回 None / "WARN|原因" / "VETO|原因"。"""
    pb = p_break or 0
    shrunk = is_shrunk(day_vol_ratio)
    if pb >= HARD_VETO_PB and not shrunk and not yizi:
        return "VETO|极端断板率(%.0f%%)且放量接力——历史同条件 T+1 胜率仅 33%%" % pb
    if pb >= VETO_PB and not shrunk and not yizi:
        return "WARN|断板率%.0f%%偏高且放量——历史同条件 T+1 胜率仅 33%%，轻仓/快进快出" % pb
    if day_vol_ratio is not None and day_vol_ratio >= 3.0:
        return "WARN|异常放量 %.1f 倍——追高资金拥挤，注意回撤" % day_vol_ratio
    return None


def is_veto(verdict):
    return bool(verdict) and str(verdict).startswith("VETO")


def is_warn(verdict):
    return bool(verdict) and str(verdict).startswith("WARN")


def auction_gate(open_pct, is_leader=False, shrunk=False):
    """竞价后动作裁决（9:25 口径，回测可直接执行）。"""
    low = (open_pct is None) or (open_pct < LOW_OPEN)
    if low:
        return {"action": "avoid",
                "evidence": "竞价低开——历史同条件 T+1 收红率仅 24% / 均值 -2.24%"}
    if is_leader or shrunk:
        return {"action": "buy",
                "evidence": ("核心龙头" if is_leader else "")
                + ("缩量承接" if shrunk else "")
                + " · 不低开——历史胜率 78% / 均值 +5.5%"}
    return {"action": "watch", "evidence": "不低开但无龙头/缩量加成 · 历史胜率约 53%"}


def suggest_thresholds(con, recent_n=120):
    """基于 rec_picks 滚动回测自动建议阈值（样本<30 返回 None 维持默认）。
    本系统 outcome_ret 为 T+2 口径；p_break 未采集 → 仅校准 low_open。"""
    try:
        rows = con.execute(
            "SELECT outcome_ret FROM rec_picks WHERE outcome_ret IS NOT NULL "
            "ORDER BY date DESC LIMIT ?", (recent_n,)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    if len(rows) < 30:
        return None
    losers = [r[0] for r in rows if r[0] is not None and r[0] <= 0]
    return {"n": len(rows), "win_rate":
            round(sum(1 for r in rows if (r[0] or 0) > 0) / len(rows) * 100, 1),
            "note": "p_break 未采集，仅统计口径可校准"}


def auto_calibrate(con):
    """RECVETO_AUTO_CALIB=1 时才允许覆盖默认阈值（默认行为不变）。"""
    if os.environ.get("RECVETO_AUTO_CALIB") != "1":
        return None
    return suggest_thresholds(con)


def apply_veto(items, vol_ratio_of=None):
    """对候选全列表执行标注式判定，返回 (kept, vetoed)。
    WARN 者保留并带 veto_reason/risk_flag；VETO 者进回避。"""
    kept, vetoed = [], []
    for it in items:
        ratio = vol_ratio_of(it) if vol_ratio_of else it.get("day_vol_ratio")
        reason = veto(it.get("p_break"), ratio, it.get("yizi"))
        if not reason:
            kept.append(it)
        elif is_veto(reason):
            it.setdefault("veto_reason", reason.split("|", 1)[1])
            vetoed.append(it)
        else:
            it.setdefault("veto_reason", reason.split("|", 1)[1])
            it["risk_flag"] = "⚠"
            kept.append(it)
    return kept, vetoed
