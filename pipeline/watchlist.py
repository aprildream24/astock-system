# -*- coding: utf-8 -*-
"""自选股每日操作建议（用户需求 2026-09-13 + 规格书 十）。

核心口径（未持仓语境翻译——推荐池票冒充自选=历史事故）：
  未持仓票禁止显示"持有/持有（强势）"，改"观望（等回落至买区）"
  或"可小仓跟进（刚突破）"，统一附"距买点 X% 回落至 Y 再关注"。
持仓自选：走持仓裁决语境（止损/持有/减仓），不重复判买卖点。

动作优先级：已破位 > 触止损 > 可买（回落至买区） > 微超（小仓试）
  > 等回踩（挂单等回落） > 过热（追高风险） > 跟踪中。
全部容错：单只数据缺失 → "数据不足"，不阻断整份建议。
"""
import re

from . import engines
from .core import prev_trading_day
from .mood import is_limit_up


def _name_of(con, code):
    row = con.execute("SELECT name FROM snapshot WHERE code=? "
                      "ORDER BY date DESC LIMIT 1", (code,)).fetchone()
    return row[0] if row and row[0] else ""


def build_watch_advice(con, date, watch_codes, holdings_codes=()):
    """watch_codes: ["sh600000", ...]；holdings_codes: 持仓集合（语境切换）。
    返回 [{code,name,close,action,advice,dist_pct,zone,stop,reasons,tradable}]"""
    from . import mktfilter
    out = []
    holdings = set(holdings_codes or ())
    for code in watch_codes or []:
        num = code[2:] if code[:2] in ("sh", "sz") else code
        item = {"code": code, "name": _name_of(con, code), "close": None,
                "action": "数据不足", "advice": "当日行情缺失",
                "dist_pct": None, "zone": None, "stop": None,
                "reasons": [], "tradable": mktfilter.tradable(num),
                "is_holding": code in holdings}
        out.append(item)
        rows = con.execute(
            "SELECT date,o,c,h,l,v FROM klines WHERE code=? AND date<=? "
            "ORDER BY date DESC LIMIT 60", (code, date)).fetchall()
        rows = [[d, o, c, h, l, v] for d, o, c, h, l, v in reversed(rows)]
        if len(rows) < 30:
            continue
        close = rows[-1][2]
        prev = con.execute(
            "SELECT c FROM klines WHERE code=? AND date<? ORDER BY date DESC "
            "LIMIT 1", (code, date)).fetchone()
        day_pct = round((close / prev[0] - 1) * 100, 2) if prev and prev[0] else None
        item["close"] = close
        item["day_pct"] = day_pct
        reasons = []
        if day_pct is not None:
            reasons.append(f"今日 {day_pct:+.2f}%")
        # 当日涨停：可看不可买（连板体系跟踪）
        if prev and prev[0] and is_limit_up(num, close, prev[0]):
            item["action"] = "已涨停"
            item["advice"] = "今日涨停，当下买不进；连板通道次日竞价确认，低开放弃"
            item["reasons"].append("涨停归连板池")
            continue
        # 急跌不接刀：当日深跌先看企稳，不当日喊买（-14% 落进买区 ≠ 好买点）
        if day_pct is not None and day_pct <= -5:
            item["action"] = "急跌"
            item["advice"] = ("⚠️ 当日急跌 %.1f%%——企稳前不接刀，"
                              "等止跌信号出现再评估" % day_pct)
            item["reasons"].append("单日深跌")
            continue
        # 买卖区间：箱体优先；无箱体用「回踩档」pull_zone 做关注区。
        # 注：now_zone 已于 2026-09-13 修正为 ≤4.5% 窄带（旧版相对现价构造会让
        # 任何票都落在区间内），但自选语境仍须**绝对锚**——pull_zone 由
        # 均线/近端低点绝对定位，不随当日收盘漂移，破位票不会失真。
        box = engines.detect_stage_bottom(rows)
        plan = engines.entry_plan(rows, box_low=box["box_low"] if box else None)
        zone = (box and [box["buy_low"], box["buy_high"]]) or plan["pull_zone"]
        stop = box["stop"] if box else plan["stop"]
        item["zone"] = [round(zone[0], 2), round(zone[1], 2)]
        item["stop"] = round(stop, 2)
        if close <= stop:
            item["action"] = "已破位"
            item["advice"] = ("⛔ 跌破止损 %.2f——禁买/止损离场" % stop
                              if code in holdings else
                              "⛔ 破位票，移出自选或仅观察，禁买")
            item["reasons"].append("现价低于止损")
            continue
        hi = zone[1]
        if close < zone[0]:
            item["action"] = "已破位"
            item["advice"] = "跌破关注区间下沿，等待重新企稳"
            continue
        if zone[0] <= close <= hi:
            item["action"] = "可买（回落至买区）"
            item["advice"] = ("🟢 现价在买区内，可下单买入" if code in holdings
                              else "🟢 回落至买区，可首仓买入（小仓起步）")
            item["reasons"].append("贴近低吸区" if box else "近端买区")
        elif close <= hi * 1.03:
            item["action"] = "微超"
            item["advice"] = "🟡 微超买区 %.1f%%——小仓试探或等回落" % (
                (close / hi - 1) * 100)
            item["dist_pct"] = round((close / hi - 1) * 100, 1)
        elif close <= hi * 1.06:
            item["action"] = "等回踩"
            item["advice"] = ("⏳ 高于买区 %.1f%%——挂单等回落至 %.2f 再关注"
                              % ((close / hi - 1) * 100, hi * 1.005))
            item["dist_pct"] = round((close / hi - 1) * 100, 1)
        elif close <= hi * 1.12:
            item["action"] = "过热"
            item["advice"] = "🔴 明显过热——追高风险大，等回调至 %.2f 下方" % hi
            item["dist_pct"] = round((close / hi - 1) * 100, 1)
        else:
            item["action"] = "过热"
            item["advice"] = "🔴 严重超买（偏离 %.0f%%）——不追，等深度回调" % (
                (close / hi - 1) * 100)
            item["dist_pct"] = round((close / hi - 1) * 100, 1)
        if box:
            reasons.append("箱体 %.2f~%.2f" % (box["box_low"], box["box_high"]))
        if plan.get("state"):
            reasons.append("四态:%s" % plan["state"])
        if not item["tradable"]:
            item["advice"] = "⚠️ " + item["advice"] + "（该市场不可交易，仅观察）"
        item["reasons"] = reasons[:3]
    return out


def summary_lines(advice):
    out = ["自选股操作建议（%d 只）" % len(advice or [])]
    for a in advice or []:
        d = "" if a.get("dist_pct") is None else "（距买区 %+.1f%%）" % a["dist_pct"]
        out.append("- %s %s：%s%s" % (a.get("name") or "", a["code"],
                                      a["advice"], d))
    return out
