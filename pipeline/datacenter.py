# -*- coding: utf-8 -*-
"""数据中心小引擎群（吸收自原项目 margin/etfflow/lhbseats/blocktrade）。

每个引擎 try/except 兜底为 None，绝不阻断主流程；
数据缺失时 summary_lines 返回 []，推送自动跳过。
"""
import time
from collections import Counter

from . import emdc


def _fnum(x):
    try:
        return float(x)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 两融余额趋势（RPTA_RZRQ_LSHJ）
# ---------------------------------------------------------------------------

def margin_scan(n=20):
    rows = emdc.get("RPTA_RZRQ_LSHJ", columns="ALL", page_size=n + 2,
                    sort="DIM_DATE")
    if not rows:
        return None
    series = []
    for r in rows:
        d = str(r.get("DIM_DATE") or "")[:10]
        if d:
            series.append({"date": d,
                           "total_yi": round(_fnum(r.get("RZRQYE")) / 1e8, 0)})
    if len(series) < 2:
        return None
    series.sort(key=lambda x: x["date"])
    for i in range(1, len(series)):
        series[i]["delta_yi"] = round(series[i]["total_yi"]
                                      - series[i - 1]["total_yi"], 1)
    latest = series[-1]
    return {"date": latest["date"], "latest_yi": latest["total_yi"],
            "delta_yi": latest["delta_yi"], "series": series}


# ---------------------------------------------------------------------------
# ETF 主力资金流（clist 基金板块）
# ---------------------------------------------------------------------------

def etfflow_scan(max_pages=2):
    rows = []
    for page in range(1, max_pages + 1):
        url = ("https://push2.eastmoney.com/api/qt/clist/get?"
               f"pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f62&"
               "fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024&"
               "fields=f12,f14,f3,f62")
        try:
            from .core import fetch_text
            import json
            js = json.loads(fetch_text(url, timeout=10))
            diff = (js.get("data") or {}).get("diff") or []
            rows.extend(diff)
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        return None
    items = []
    for m in rows:
        code = str(m.get("f12") or "")
        name = m.get("f14") or ""
        if not code or not name:
            continue
        if "ETF" not in name.upper() and not (code[:1] in "51" and len(code) == 6):
            continue
        items.append({"code": code, "name": name,
                      "pct": round(_fnum(m.get("f3")), 2),
                      "net_yi": round(_fnum(m.get("f62")) / 1e8, 2)})
    if not items:
        return None
    items.sort(key=lambda x: x["net_yi"], reverse=True)
    return {"date": time.strftime("%Y-%m-%d"), "n": len(items),
            "total_net_yi": round(sum(x["net_yi"] for x in items), 1),
            "inflow_n": sum(1 for x in items if x["net_yi"] > 0),
            "outflow_n": sum(1 for x in items if x["net_yi"] < 0),
            "top": items[:8], "bottom": list(reversed(items[-5:]))}


# ---------------------------------------------------------------------------
# 龙虎榜席位（RPT_DAILYBILLBOARD_DETAILSNEW）
# ---------------------------------------------------------------------------

def lhb_scan(date):
    rows = emdc.get("RPT_DAILYBILLBOARD_DETAILSNEW", columns="ALL",
                    flt="(TRADE_DATE='%s')" % date, page_size=200,
                    sort="BILLBOARD_NET_AMT")
    if not rows:
        return None
    items = emdc.extract(rows, {
        "code": ["SECURITY_CODE"], "name": ["SECURITY_NAME_ABBR"],
        "chg": ["CHANGE_RATE"], "net": ["BILLBOARD_NET_AMT"],
        "reason": ["EXPLANATION", "EXPLAIN"]})
    if not items:
        return None
    top = sorted(items, key=lambda x: _fnum(x.get("net")), reverse=True)[:10]
    top = [{"code": t.get("code"), "name": t.get("name"),
            "net_yi": round(_fnum(t.get("net")) / 1e8, 2),
            "chg": round(_fnum(t.get("chg")), 2),
            "reason": (t.get("reason") or "")[:20]} for t in top]
    net_buy = [{"code": x.get("code"), "name": x.get("name"),
                "net_yi": round(_fnum(x.get("net")) / 1e8, 2)}
               for x in items if _fnum(x.get("net")) > 0]
    net_buy.sort(key=lambda x: -x["net_yi"])
    return {"date": date, "n": len(items), "top": top,
            "net_buy": net_buy[:40], "net_buy_n": len(net_buy)}


# ---------------------------------------------------------------------------
# 大宗交易（RPT_BULK_DEAL_DETAIL）：折价≥5% 视为减持/出货信号
# ---------------------------------------------------------------------------

def blocktrade_scan(date):
    rows = emdc.get("RPT_BULK_DEAL_DETAIL",
                    columns="SECURITY_CODE,SECURITY_NAME_ABBR,DISCOUNT,"
                            "TRADE_AMOUNT,BUYER_NAME,SELLER_NAME",
                    flt="(TRADE_DATE='%s')" % date, page_size=300,
                    sort="TRADE_AMOUNT")
    if not rows:
        return None
    items = emdc.extract(rows, {
        "code": ["SECURITY_CODE"], "name": ["SECURITY_NAME_ABBR"],
        "discount": ["DISCOUNT"], "amt": ["TRADE_AMOUNT"],
        "buyer": ["BUYER_NAME"], "seller": ["SELLER_NAME"]})
    if not items:
        return None
    discount = [x for x in items if _fnum(x.get("discount")) <= -5.0]
    discount.sort(key=lambda x: _fnum(x.get("discount")))
    top = [{"code": d.get("code"), "name": d.get("name"),
            "discount": round(_fnum(d.get("discount")), 2),
            "amt_yi": round(_fnum(d.get("amt")) / 1e8, 2)}
           for d in discount[:10]]
    inst = [{"code": x.get("code"), "name": x.get("name"),
             "side": "buy" if "机构专用" in (x.get("buyer") or "") else "sell",
             "amt_yi": round(_fnum(x.get("amt")) / 1e8, 2)}
            for x in items
            if "机构专用" in (x.get("buyer") or "")
            or "机构专用" in (x.get("seller") or "")]
    inst.sort(key=lambda x: -x["amt_yi"])
    return {"date": date, "n": len(items), "discount_n": len(discount),
            "top": top, "inst": inst[:10], "inst_n": len(inst)}


# ---------------------------------------------------------------------------
# 题材主线（输入注入式：涨停股需带 concepts/industry 字段，缺数据 → None）
# ---------------------------------------------------------------------------

STOPLIST = {
    "融资融券", "转融券标的", "昨日涨停", "昨日触板", "昨日跌停", "机构重仓",
    "QFII重仓", "深股通", "沪股通", "标普道琼斯A股", "MSCI概念", "富时罗素",
    "证金持股", "养老金持股", "国家队", "中央汇金", "沪伦通", "注册制", "ST股",
    "可转债", "股权激励", "员工持股", "业绩预增", "高送转", "摘帽", "融资标的",
    "融券标的", "深成500", "上证180", "上证50", "沪深300", "中证500",
    "中证1000", "大盘", "中盘", "小盘", "破净股", "低价股", "昨日连板",
    "昨日首板", "新股与次新股",
}
CONCEPT_WEIGHT = 1.0
INDUSTRY_WEIGHT = 0.4


def theme_scan(date, limit_ups):
    if not limit_ups:
        return None
    cnt = Counter()
    for x in limit_ups:
        for c in (x.get("concepts") or []):
            if c and c not in ("--", "无") and c not in STOPLIST:
                cnt[c] += CONCEPT_WEIGHT
        ind = x.get("industry")
        if ind and ind not in ("--", "无") and ind not in STOPLIST:
            cnt[ind] += INDUSTRY_WEIGHT
    if not cnt:
        return None
    ranked = [(t, round(v, 1)) for t, v in cnt.most_common(10) if v >= 1.0]
    if not ranked:
        return None
    return {"date": date, "main_theme": ranked[0][0], "main_n": ranked[0][1],
            "sub_themes": [{"theme": t, "n": v} for t, v in ranked[1:4]]}


# ---------------------------------------------------------------------------
# 连续信号（跨日硬信号：单日快照看不到，必须靠历史序列）
# ---------------------------------------------------------------------------

def _tail_streak(vals):
    if not vals:
        return 0, 0, None
    pos = neg = 0
    for v in reversed(vals):
        if v > 0:
            if neg == 0:
                pos += 1
            else:
                break
        elif v < 0:
            if pos == 0:
                neg += 1
            else:
                break
        else:
            break
    return pos, neg, vals[-1]


def margin_signal(con, days=25, table="margin_daily", col="delta_yi"):
    """读本地序列表（建表见 store schema）；无历史友好降级 None。"""
    try:
        rows = con.execute(
            f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL "
            "ORDER BY date DESC LIMIT ?", (days,)).fetchall()
    except Exception:  # noqa: BLE001 — 表不存在=无历史
        return None
    deltas = [r[0] for r in rows][::-1]
    if len(deltas) < 3:
        return None
    pos, neg, last = _tail_streak(deltas)
    verdict = "中性"
    if pos >= 3:
        verdict = "连续%d日净流入（资金加仓）" % pos
    elif neg >= 3:
        verdict = "连续%d日净流出（资金撤退）" % neg
    return {"streak_in": pos, "streak_out": neg, "verdict": verdict}


def summary(margin=None, etf=None, lhb=None, block=None):
    out = []
    if margin:
        d = margin.get("delta_yi") or 0
        out.append("两融余额 %.0f 亿（当日 %+.0f 亿）"
                   % (margin["latest_yi"], d))
    if etf:
        out.append("ETF 主力净流入合计 %+.1f 亿（净流入 %d 只/净流出 %d 只）"
                   % (etf["total_net_yi"], etf["inflow_n"], etf["outflow_n"]))
    if lhb:
        for t in lhb.get("top", [])[:3]:
            out.append("龙虎榜 %s 净买 %.2f 亿（%s）"
                       % (t["name"], t["net_yi"], t.get("reason") or "—"))
    if block and block.get("discount_n"):
        out.append("大宗交易折价≥5%% 共 %d 笔" % block["discount_n"])
    return out
