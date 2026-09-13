# -*- coding: utf-8 -*-
"""风险预警三级分级（吸收自原项目 risklevel.py）：红/黄/蓝统一灯号。

  🔴 红 · 立即行动：跌破止损/触发退出规则——今天就该动作
  🟡 黄 · 提高警惕：贴近止损/接近周期上限/接近熔断——设好预案
  🔵 蓝 · 正常跟踪：暂无结构性风险

分级不是新算法，而是把 executor 退出规则、信号生命周期、账户熔断
按优先级编排成统一灯号。红 > 黄 > 蓝；命中多条保留全部原因。
全部容错，绝不抛出。
"""

LEVEL_META = {
    "red": {"emoji": "🔴", "label": "红·立即行动", "color": "#e02020"},
    "yellow": {"emoji": "🟡", "label": "黄·提高警惕", "color": "#e6a700"},
    "blue": {"emoji": "🔵", "label": "蓝·正常跟踪", "color": "#2f6fed"},
}


def classify_holding(op):
    """op = {code, name, close, stop, pnl_pct, action, reasons, hold_days,
              hold_limit} → (level, reasons)。"""
    reasons_r, reasons_y = [], []
    close, stop = op.get("close"), op.get("stop")
    pnl = op.get("pnl_pct")
    action = op.get("action") or ""
    # 红级：跌破止损线（无条件，最高优先级）
    if close and stop and close <= stop:
        reasons_r.append("现价%.2f 已跌破止损%.2f" % (close, stop))
    if action == "SELL" or any(
            k in str(r) for r in (op.get("reasons") or [])
            for k in ("止损", "破位", "退出")):
        reasons_r.append("触发退出规则：%s" % (action or "；".join(
            str(r) for r in (op.get("reasons") or []))))
    if close and stop and stop < close <= stop * 1.04:
        reasons_y.append("贴近止损线（止损%.2f，现价%.2f）" % (stop, close))
    if pnl is not None and -8 < pnl <= -5:
        reasons_y.append("浮亏 %.1f%%，接近纪律线" % pnl)
    hl, hd = op.get("hold_limit"), op.get("hold_days")
    if hl and hd and hl - 1 <= hd < hl:
        reasons_y.append("持有周期临近上限（%d/%d 日）" % (hd, hl))
    elif hl and hd and hd >= hl:
        reasons_r.append("持有周期超上限（%d/%d 日）" % (hd, hl))
    if reasons_r:
        return "red", reasons_r
    if reasons_y:
        return "yellow", reasons_y
    return "blue", []


def compute(ops, day_pnl=None, halt_line=None):
    """主入口：持仓操作记录列表 → risk_levels dict。全部容错。"""
    try:
        holdings, counts = [], {"red": 0, "yellow": 0, "blue": 0}
        for op in ops or []:
            if not isinstance(op, dict) or not op.get("code"):
                continue
            level, reasons = classify_holding(op)
            counts[level] += 1
            holdings.append({"code": op.get("code"), "name": op.get("name"),
                             "level": level, "reasons": reasons,
                             "action": op.get("action")})
        ov_r, ov_y = [], []
        if day_pnl is not None and halt_line is not None \
                and day_pnl <= halt_line * 100 * 0.8:
            ov_y.append("当日组合亏损 %.1f%%，接近熔断线 %.1f%%"
                        % (day_pnl, halt_line * 100))
        if counts.get("red"):
            ov_r.append("持仓中 %d 只亮红灯" % counts["red"])
        elif counts.get("yellow"):
            ov_y.append("持仓中 %d 只亮黄灯" % counts["yellow"])
        overall = "red" if ov_r else ("yellow" if ov_y else "blue")
        return {"overall": {"level": overall, "reasons": ov_r + ov_y},
                "holdings": holdings, "counts": counts}
    except Exception as e:  # noqa: BLE001
        return {"overall": {"level": "blue", "reasons": ["分级计算异常:%r" % e]},
                "holdings": [], "counts": {"red": 0, "yellow": 0, "blue": 0}}


def summary_lines(rl):
    if not rl:
        return []
    o = rl.get("overall") or {}
    m = LEVEL_META.get(o.get("level"), LEVEL_META["blue"])
    out = ["%s%s：账户整体状态" % (m["emoji"], m["label"])]
    for r in (o.get("reasons") or [])[:3]:
        out.append("- " + str(r))
    for h in (rl.get("holdings") or [])[:5]:
        if h["level"] == "blue":
            continue
        hm = LEVEL_META[h["level"]]
        out.append("- %s%s %s：%s" % (hm["emoji"], h.get("name") or "",
                                      h["code"], "；".join(h["reasons"]) or "—"))
    return out
