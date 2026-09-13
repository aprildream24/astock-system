# -*- coding: utf-8 -*-
"""推荐池历史胜率曲线（吸收自原项目 recperf.py）。

纯本地、零网络：数据来自 rec_picks（outcome/outcome_ret，T+2 口径回填）。
输出：每日 T+2 平均收益、盈利占比、等权买入累计净值曲线，以及
「行情阶段分层胜率」（当日均值 vs 近20日滚动均值 → 上升/震荡/下降）。

附录B 披露口径：样本时间/数量/费用=未含/退出=T+2收盘/不可成交未剔除
——本曲线是研究参考，不是可执行收益承诺。
"""


def build(con, limit=2400, min_days=5):
    rows = con.execute(
        "SELECT date, tag, outcome, outcome_ret FROM rec_picks "
        "WHERE outcome_ret IS NOT NULL ORDER BY date LIMIT ?",
        (limit,)).fetchall()
    if not rows:
        return None
    by_date = {}
    for date, tag, outcome, ret in rows:
        d = by_date.setdefault(date, {"n": 0, "pn": 0.0, "win": 0})
        d["n"] += 1
        ret = ret or 0.0
        d["pn"] += ret
        if ret > 0:
            d["win"] += 1
    dates = sorted(by_date.keys())
    if len(dates) < min_days:
        return None

    daily_avg = [round(by_date[d]["pn"] / by_date[d]["n"], 2) if by_date[d]["n"]
                 else 0.0 for d in dates]
    win_by_phase = {"上升": [], "震荡": [], "下降": []}
    for i, d in enumerate(dates):
        if i >= 1:
            window = daily_avg[max(0, i - 20):i]
            roll = sum(window) / len(window) if window else 0.0
            diff = daily_avg[i] - roll
            phase = "上升" if diff > 0.5 else ("下降" if diff < -0.5 else "震荡")
        else:
            phase = "震荡"
        n, win = by_date[d]["n"], by_date[d]["win"]
        if n:
            win_by_phase[phase].append(win / n * 100.0)
    phase_winrate = {ph: {"win_rate": round(sum(v) / len(v), 1),
                          "n_days": len(v)}
                     for ph, v in win_by_phase.items() if v}

    series_dates, win_rate, avg_pct, cumulative = [], [], [], []
    cum = 1.0
    for d in dates:
        s = by_date[d]
        series_dates.append(d)
        win_rate.append(round(100.0 * s["win"] / s["n"], 1) if s["n"] else 0.0)
        avg = s["pn"] / s["n"] if s["n"] else 0.0
        avg_pct.append(round(avg, 2))
        cum *= (1.0 + avg / 100.0)
        cumulative.append(round(cum, 3))

    def _avg(lst, n):
        v = lst[-n:]
        return round(sum(v) / len(v), 1) if v else None

    return {"dates": series_dates, "win_rate": win_rate,
            "avg_pct": avg_pct, "cumulative": cumulative,
            "n_days": len(dates), "phase_winrate": phase_winrate,
            "recent30": {"win_rate": _avg(win_rate, 30),
                         "avg_pct": _avg(avg_pct, 30)},
            "final_cum": round(cum, 3)}


DISCLOSURE = ("口径：T+2收盘 vs 推荐日收盘（升级-4 附录B）｜样本=%(n_days)s 日｜"
              "费用/滑点=未含｜不可成交=未剔除｜样本内回溯，非样本外")


def summary_lines(rp):
    if not rp:
        return ["推荐池胜率：暂无足够历史样本（T+2 结局回填累计中）"]
    r = rp.get("recent30") or {}
    out = ["推荐池近 30 日：盈利占比 **%s%%** ｜ 平均收益 **%s%%**"
           % (r.get("win_rate"), r.get("avg_pct"))]
    out.append("累计净值（等权 T+2 了结）：**%s**（回溯 %d 个交易日）"
               % (rp.get("final_cum"), rp.get("n_days")))
    out.append(DISCLOSURE % {"n_days": rp.get("n_days")})
    return out
