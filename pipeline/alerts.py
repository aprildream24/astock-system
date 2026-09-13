# -*- coding: utf-8 -*-
"""触发式盯盘（吸收自原项目 alerts.py）：把观察区间/持仓成本转化为
可执行的「条件命中」，供推送即时提醒（日K收盘口径）。

触发类型：
  止损(3) —— 持仓或关注票跌破止损线（高优先级）
  止盈(2) —— 触及卖出区
  买点(1) —— 回踩买入区/进入关注区间
  锁定(1) —— 关注以来涨幅过大，提示部分获利了结
日K纪律：仅判定「当日曾触及」，不做盘中先后顺序断言（333-三）。
"""
SEV = {"止损": 3, "止盈": 2, "买点": 1, "锁定": 1}


def build_triggers(signals, prices, holdings_pnl=None, watch_since=None):
    """signals: active_signals() 输出（含 zone/stop/status）；
    prices: {code: (close, low, high)}；
    holdings_pnl: [{code, name, pnl_pct, close}]（持仓止盈/锁定）；
    watch_since: [{code, name, since_pct}]（关注以来涨幅）。
    返回 {date, n, hits:[...]}，同 code+type 去重留最严重。"""
    hits = []
    hold_codes = {h.get("code") for h in (holdings_pnl or []) if h.get("code")}

    def add(code, name, tp, detail, ref, pool):
        hits.append({"code": code, "name": name, "type": tp, "sev": SEV[tp],
                     "pool": pool, "detail": detail, "ref": ref})

    for s in signals or []:
        code = s.get("code")
        bar = (prices or {}).get(code)
        if not bar:
            continue
        close, low, high = bar
        lo, hi = (s.get("zone") or [None, None])
        name = s.get("name", "")
        stop = s.get("stop")
        pool = "持仓" if code in hold_codes else "观察"
        if stop and low <= stop:
            add(code, name, "止损", "当日触及止损 %.2f（收盘 %.2f，先后顺序不可知）"
                % (stop, close), stop, pool)
        elif lo and hi and lo <= close <= hi and low <= hi:
            add(code, name, "买点", "当日回踩进入关注区间 %.2f~%.2f（收盘 %.2f）"
                % (lo, hi, close), lo, pool)
        if s.get("sell_low") and high >= s["sell_low"]:
            add(code, name, "止盈", "触及卖出区下沿 %.2f" % s["sell_low"],
                s["sell_low"], pool)

    for h in holdings_pnl or []:
        pnl = h.get("pnl_pct")
        if pnl is not None and pnl >= 15:
            add(h["code"], h.get("name", ""), "止盈",
                "持仓浮盈 %+.1f%%，触及纪律止盈线" % pnl, None, "持仓")

    for w in watch_since or []:
        pct = w.get("since_pct")
        if pct is not None and pct >= 30:
            add(w["code"], w.get("name", ""), "锁定",
                "关注以来累计 %+.1f%%，可考虑部分获利了结" % pct, None, "关注")

    uniq = {}
    for h in hits:
        k = (h["code"], h["type"])
        if k not in uniq or h["sev"] > uniq[k]["sev"]:
            uniq[k] = h
    out = sorted(uniq.values(), key=lambda x: (-x["sev"], x["code"]))
    import time
    return {"date": time.strftime("%Y-%m-%d"), "n": len(out), "hits": out}


def summary_lines(tr):
    if not tr or not tr.get("hits"):
        return []
    out = ["触发盯盘：%d 条条件命中" % tr["n"]]
    for h in tr["hits"][:8]:
        out.append("· 【%s·%s】%s（%s）：%s"
                   % (h["pool"], h["type"], h["name"], h["code"], h["detail"]))
    return out
