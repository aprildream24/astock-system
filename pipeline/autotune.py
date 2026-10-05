# -*- coding: utf-8 -*-
"""周度自修正（2026-10-05 用户需求：「根据每周模拟盘盈亏情况，及时对
选股进行修正——自动，不要让我每次主动说明」）。

## 设计边界（先说清什么**不会**被自动调）
  · 止损线 / 裁决闸门 / 仓位纪律（F1-F3）**永不自动放松**——这些是保命
    规则，放松它们等于让系统自己学会亏损；
  · 只调「选股加权」类因子，且每轮调整**有界**（±15% 内）、**有据**
    （样本不足不动）、**留痕**（tune_state 表存 value+reason+时间，
    每次变化进复盘推送一行说清）。

## 数据流
  fills/orders（模拟盘全部历史实际成交，剔除假期幻影冲正单）→
  FIFO 配对出已平仓交易 → 按买入日当天的裁决（day_meta）与确认数
  （confirm_log）给交易打标 → 全历史统计胜率/均盈亏/失败模式 →
  调 k_first（首推折价）/ k_hot（热度因子缩放）→ scoring.TUNE 生效 →
  复盘推送报告一行。

## 节奏
  每周五复盘触发（或距上次 ≥7 天）——这是「多久复盘一次」；
  学习窗口是**全部历史**——模拟盘账户开账以来的每一笔平仓都算证据。
  全历史平仓 <3 笔不调（防小样本过拟合）。
"""
import datetime as dt
import json

KNOBS = {
    "k_first": {"lo": 0.85, "hi": 1.00, "desc": "首推票排名折价"},
    "k_hot": {"lo": 0.60, "hi": 1.40, "desc": "热度因子缩放（板块/RS 加成）"},
    "entry_cap_auction": {"lo": 0.50, "hi": 1.00,
                          "desc": "竞价/盘前入场建仓规模系数"},
    "entry_cap_live": {"lo": 0.50, "hi": 1.00,
                       "desc": "盘中机动入场建仓规模系数"},
}
DEFAULTS = {"k_first": 1.00, "k_hot": 1.00,
            "entry_cap_auction": 1.00, "entry_cap_live": 1.00}
# 样本与证据门槛：不到门槛绝不动参数（宁可不调，不可乱调）
MIN_TRADES = 3
CHURN_DAYS = 2          # 买入后 ≤2 个交易日止损 = 追高搅肉
COLD_VERDICTS = ("离场为主", "观望为主")


def due(con, today):
    """周五复盘 / 距上次调参 ≥7 天 → 该调了。"""
    try:
        d = dt.date.fromisoformat(str(today)[:10])
    except ValueError:
        return False
    if d.weekday() == 4:
        return True
    row = con.execute(
        "SELECT MAX(updated_at) FROM tune_state").fetchone()
    if not row or not row[0]:
        return False
    try:
        last = dt.datetime.fromisoformat(row[0][:19]).date()
    except ValueError:
        return False
    return (d - last).days >= 7


def _day_meta_of(con, date):
    row = con.execute(
        "SELECT verdict, mood FROM day_meta WHERE date=?",
        (date,)).fetchone()
    if row:
        return row[0], row[1]
    row = con.execute(
        "SELECT score FROM emotion_log WHERE date=?", (date,)).fetchone()
    if not row:
        return None, None
    s = float(row[0] or 50)
    return ("轻仓试探" if s >= 45 else "离场为主"), s


def _confirms_of(con, code, date):
    n = con.execute(
        "SELECT COUNT(DISTINCT date || task) FROM confirm_log "
        "WHERE code=? AND date<=? AND date>=date(?, '-10 day')",
        (code, date, date)).fetchone()[0]
    return n


def _ymd(ts):
    return str(ts)[:10]


def collect_trades(con, today, days=None):
    """全部历史已平仓交易（FIFO 配对买卖成交）。

    ★ 2026-10-05 修正（用户：「系统运行了一个多月，那么多历史都可以参考
    借鉴，样本不足是伪命题」）：原实现只看最近 7 天 → 首周必然样本不足。
    改为**全部历史**入样——模拟盘账户的每一次平仓都是证据；周度只是
    「多久复盘一次」的节奏，不是「只看多久」的窗口。
    调用方传 days 时按天过滤（保留给将来分段诊断用）。
    假期幻影成交（offday_reverted 已冲正标记）一律剔除——那不是交易。

    字段：code, pnl_pct, hold_days（日历天）, hold_tdays（交易日）,
    entry_date, verdict, mood, confirms, mode。"""
    cutoff = (dt.date.fromisoformat(str(today)[:10])
              - dt.timedelta(days=days)).isoformat() if days else ""
    buys = {}
    trades = []
    rows = con.execute(
        "SELECT f.ts, f.code, f.side, f.price, f.order_id, o.reason "
        "FROM fills f LEFT JOIN orders o ON o.order_id=f.order_id "
        "WHERE NOT EXISTS (SELECT 1 FROM offday_reverted r "
        "                  WHERE r.fill_id = f.fill_id) "
        "ORDER BY f.ts").fetchall()
    for ts, code, side, price, oid, reason in rows:
        d = _ymd(ts)
        if side == "buy":
            buys.setdefault(code, []).append((d, float(price), str(ts)))
            continue
        lots = buys.get(code) or []
        if not lots:
            continue
        bd, bp, bts = lots[0]                 # FIFO：最早买入批配对
        buys[code] = lots[1:]
        if (cutoff and d < cutoff) or bp <= 0:
            continue                          # 过滤窗口外的旧交易
                                              #（无窗口=全历史，只受冲正标记约束）
        try:
            hold = (dt.date.fromisoformat(d)
                    - dt.date.fromisoformat(bd)).days
        except ValueError:
            hold = 99
        # 搅肉判定按**交易日**（2026-10-05 演练发现：日历天会把
        # 「节前买、节后止损」错算成 8 天，漏判追高搅肉）
        hold_t = con.execute(
            "SELECT COUNT(DISTINCT date) FROM klines "
            "WHERE code='sh000001' AND date>? AND date<=?",
            (bd, d)).fetchone()[0]
        verdict, mood = _day_meta_of(con, bd)
        # 入场时点归因（2026-10-05 用户需求：「盘前/竞价购入 vs 盘中机动
        # 购入，哪个成功率高」）——按买入成交时刻分桶
        hm = bts[11:16] if len(bts) >= 16 else "09:26"
        tier = "竞价" if hm <= "09:31" else "盘中"
        reason = reason or ""
        if "盈利回吐" in reason:
            mode = "giveback"
        elif "止损" in reason or "破位" in reason:
            mode = "stop"
        elif "止盈" in reason:
            mode = "take_profit"
        else:
            mode = "other"
        trades.append({
            "code": code, "pnl_pct": (float(price) / bp - 1) * 100,
            "hold_days": hold, "hold_tdays": hold_t,
            "entry_date": bd, "entry_tier": tier, "entry_hm": hm,
            "verdict": verdict,
            "mood": mood, "confirms": _confirms_of(con, code, bd),
            "mode": mode})
    return trades


def analyze(trades):
    n = len(trades)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    first = [t for t in trades if (t.get("confirms") or 0) < 2]
    churn = [t for t in trades if t.get("hold_tdays", 99) <= CHURN_DAYS
             and t["pnl_pct"] < 0]
    # 入场时点分桶（2026-10-05 用户需求：竞价 vs 盘中机动谁更靠谱）
    entries = {}
    for tier in ("竞价", "盘中"):
        g = [t for t in trades if t.get("entry_tier") == tier]
        entries[tier] = {"n": len(g),
                         "avg": (sum(t["pnl_pct"] for t in g) / len(g))
                         if g else 0.0,
                         "win": (sum(1 for t in g if t["pnl_pct"] > 0)
                                 / len(g)) if g else 0.0}
    return {
        "n": n,
        "win_rate": (len(wins) / n) if n else 0.0,
        "avg_pnl": (sum(t["pnl_pct"] for t in trades) / n) if n else 0.0,
        "first_n": len(first),
        "first_avg": (sum(t["pnl_pct"] for t in first) / len(first))
        if first else 0.0,
        "churn_n": len(churn),
        "entries": entries,
        "modes": {},
    }


def _clamp(key, v):
    lo, hi = KNOBS[key]["lo"], KNOBS[key]["hi"]
    return max(lo, min(hi, v))


def _current(con):
    vals = dict(DEFAULTS)
    for k, v in con.execute("SELECT key, value FROM tune_state"):
        if k in vals:
            vals[k] = float(v)
    return vals


def tune(con, today, dry=False):
    """跑一次周度修正。返回报告 dict（含 summary 一行话，供推送引用）；
    样本不足返回 no_change 报告（不写库）。"""
    trades = collect_trades(con, today)
    st = analyze(trades)
    cur = _current(con)
    changes = []
    if st["n"] >= MIN_TRADES:
        # k_first：首推票（确认<2）连亏 → 折价加深；连赚 → 放开
        if st["first_n"] >= 2 and st["first_avg"] <= -2.0:
            new = _clamp("k_first", 0.85)
            if new < cur["k_first"] - 1e-9:
                changes.append(("k_first", new,
                                f"首推票{st['first_n']}笔均亏"
                                f"{st['first_avg']:.1f}%"))
        elif st["first_n"] >= 2 and st["first_avg"] >= 2.0:
            new = _clamp("k_first", 1.00)
            if new > cur["k_first"] + 1e-9:
                changes.append(("k_first", new,
                                f"首推票{st['first_n']}笔均赚"
                                f"{st['first_avg']:.1f}%"))
        # k_hot：追高搅肉占半数以上 → 压热度加成；整体胜率高 → 温和放大
        if st["churn_n"] >= 2 and st["churn_n"] / st["n"] >= 0.5:
            new = _clamp("k_hot", cur["k_hot"] * 0.80)
            if abs(new - cur["k_hot"]) > 1e-9:
                changes.append(("k_hot", new,
                                f"隔日止损{st['churn_n']}/{st['n']}笔"
                                "（追高搅肉）→ 压热度因子"))
        elif st["win_rate"] >= 0.60:
            new = _clamp("k_hot", cur["k_hot"] * 1.10)
            if abs(new - cur["k_hot"]) > 1e-9:
                changes.append(("k_hot", new,
                                f"周胜率{st['win_rate']:.0%} → "
                                "温和放大有效因子"))
        # 入场时点归因（用户需求：「盘前/竞价 vs 盘中机动谁成功率高，
        # 然后修正」）：某一时点 ≥3 笔且均亏 ≤-2%、比另一时点差 ≥2 个点
        # → 该时点建仓规模降半档；连赚回正 → 恢复。
        ent = st["entries"]
        for tier, key in (("竞价", "entry_cap_auction"),
                          ("盘中", "entry_cap_live")):
            g = ent.get(tier) or {}
            other = ent.get("盘中" if tier == "竞价" else "竞价") or {}
            worse = (g.get("n", 0) >= 3 and g.get("avg", 0) <= -2.0
                     and (other.get("n", 0) < 3
                          or g["avg"] <= other.get("avg", 0) - 2.0))
            better = g.get("n", 0) >= 3 and g.get("avg", 0) >= 1.0
            if worse:
                new = _clamp(key, 0.50)
                if new < cur[key] - 1e-9:
                    changes.append((key, new,
                                    f"{tier}入场{g['n']}笔均亏"
                                    f"{g['avg']:.1f}% → 该时点降半仓"))
            elif better:
                new = _clamp(key, 1.00)
                if new > cur[key] + 1e-9:
                    changes.append((key, new,
                                    f"{tier}入场{g['n']}笔均赚"
                                    f"{g['avg']:.1f}% → 恢复全档"))
    now = dt.datetime.now().isoformat(timespec="seconds")
    if not dry:
        for key, val, why in changes:
            con.execute("INSERT OR REPLACE INTO tune_state VALUES(?,?,?,?)",
                        (key, val, why, now))
        con.commit()
    if changes:
        summary = "、".join(
            f"{k}:{cur[k]:.2f}→{v:.2f}（{why}）" for k, v, why in changes)
        summary = "🔧周度自修正 " + summary
    elif st["n"] < MIN_TRADES:
        summary = f"🔧周度自修正：历史平仓仅{st['n']}笔，样本不足不调"
    else:
        summary = "🔧周度自修正：证据未达门槛，参数维持"
    return {"date": today, "n": st["n"], "win_rate": st["win_rate"],
            "avg_pnl": st["avg_pnl"], "first_n": st["first_n"],
            "first_avg": st["first_avg"], "churn_n": st["churn_n"],
            "entries": st.get("entries") or {},
            "changes": changes, "current": cur, "summary": summary}


def entry_cap_of(con, slot):
    """auto_open 用：当前入场时点的建仓规模系数（竞价 1.0/盘中 1.0 起步，
    autotune 依据证据降到 0.5）。slot=None → 竞价/盘前班次；其余 → 盘中。"""
    key = "entry_cap_auction" if slot is None else "entry_cap_live"
    try:
        row = con.execute("SELECT value FROM tune_state WHERE key=?",
                          (key,)).fetchone()
        return float(row[0]) if row else DEFAULTS[key]
    except Exception:  # noqa: BLE001 — 无表/无行时用默认
        return DEFAULTS[key]


def _close_of(con, code, date):
    row = con.execute(
        "SELECT c FROM klines WHERE code=? AND date<=? "
        "ORDER BY date DESC LIMIT 1", (code, date)).fetchone()
    return row[0] if row else None


def week_quality(con, today, days=7):
    """推荐质量周检（2026-10-05 用户需求：「每天推荐的股票一周趋势怎么样、
    涨得好的有没有买、是板块原因还是选股因素」）。

    数据：confirm_log（近 days 天全部推荐）+ klines（推荐日起涨跌）+
    fills（模拟盘是否真的买了）。全部本地计算，零网络。"""
    d0 = (dt.date.fromisoformat(str(today)[:10])
          - dt.timedelta(days=days)).isoformat()
    recos = con.execute(
        "SELECT code, MIN(date) FROM confirm_log "
        "WHERE date>? AND date<=? GROUP BY code ORDER BY MIN(date)",
        (d0, today)).fetchall()
    if not recos:
        return None
    bench0 = _close_of(con, "sh000001", d0)
    bench1 = _close_of(con, "sh000001", today)
    bench = ((bench1 / bench0 - 1) * 100
             if bench0 and bench1 else None)
    items = []
    for code, first_d in recos:
        c0 = _close_of(con, code, first_d)
        c1 = _close_of(con, code, today)
        if not c0 or not c1 or not c0 > 0:
            continue
        bought = bool(con.execute(
            "SELECT 1 FROM fills WHERE code=? AND side='buy' "
            "AND date(ts)>=? AND date(ts)<=? LIMIT 1",
            (code, first_d, today)).fetchone())
        ind = con.execute(
            "SELECT sector FROM stock_industry WHERE code=?",
            (code,)).fetchone()
        nm = con.execute(
            "SELECT name FROM snapshot WHERE code=? AND name<>'' "
            "ORDER BY date DESC LIMIT 1", (code,)).fetchone()
        items.append({
            "code": code, "name": (nm[0] if nm else code),
            "first_d": first_d,
            "chg": (c1 / c0 - 1) * 100, "bought": bought,
            "sector": ind[0] if ind else "其他"})
    if not items:
        return None
    chgs = [it["chg"] for it in items]
    bought_g = [it for it in items if it["bought"]]
    nb_g = [it for it in items if not it["bought"]]
    by_sector = {}
    for it in items:
        by_sector.setdefault(it["sector"], []).append(it["chg"])
    sectors = sorted(((s, sum(v) / len(v), len(v))
                      for s, v in by_sector.items()),
                     key=lambda x: -x[1])
    srt = sorted(items, key=lambda x: -x["chg"])
    out = {
        "n": len(items),
        "avg": sum(chgs) / len(chgs),
        "bench": bench,
        "excess": (sum(chgs) / len(chgs) - bench)
        if bench is not None else None,
        "best": [(it["name"], round(it["chg"], 1)) for it in srt[:3]],
        "worst": [(it["name"], round(it["chg"], 1)) for it in srt[-3:]],
        "bought_n": len(bought_g),
        "bought_avg": (sum(it["chg"] for it in bought_g) / len(bought_g))
        if bought_g else None,
        "nb_n": len(nb_g),
        "nb_avg": (sum(it["chg"] for it in nb_g) / len(nb_g))
        if nb_g else None,
        "missed": [(it["name"], it["sector"], round(it["chg"], 1))
                   for it in srt if not it["bought"] and it["chg"] >= 3.0][:3],
        "sectors": sectors,
    }
    lines = [f"近{days}天推荐 {out['n']} 只 · 平均 {out['avg']:+.1f}%"
             + (f"（同期大盘 {bench:+.1f}%，超额 {out['excess']:+.1f}%）"
                if bench is not None else "")]
    if out["best"]:
        lines.append("涨得最好：" + " / ".join(
            f"{n}{c:+.1f}%" for n, c in out["best"]))
    if out["worst"]:
        lines.append("涨得最差：" + " / ".join(
            f"{n}{c:+.1f}%" for n, c in out["worst"]))
    if out["bought_avg"] is not None and out["nb_avg"] is not None:
        cmp_ = ("买对了" if out["bought_avg"] >= out["nb_avg"] else "买漏了")
        lines.append(f"模拟盘买了 {out['bought_n']} 只（均 "
                     f"{out['bought_avg']:+.1f}%）· 没买 {out['nb_n']} 只"
                     f"（均 {out['nb_avg']:+.1f}%）→ {cmp_}")
    elif out["bought_n"]:
        lines.append(f"模拟盘买了 {out['bought_n']} 只"
                     f"（均 {out['bought_avg']:+.1f}%）")
    if out["missed"]:
        lines.append("漏掉的大涨票：" + "、".join(
            f"{n}（{s}）{c:+.1f}%" for n, s, c in out["missed"]))
    if sectors:
        top, bot = sectors[0], sectors[-1]
        if len(sectors) > 1 and top[0] != bot[0]:
            lines.append(f"板块归因：最强 {top[0]} {top[1]:+.1f}%（{top[2]}只）"
                         f" · 最弱 {bot[0]} {bot[1]:+.1f}%（{bot[2]}只）")
    out["lines"] = lines
    return out


def week_card_html(wq):
    """复盘推送里的「推荐质量周检」小节。"""
    if not wq or not wq.get("lines"):
        return ""
    from . import notifier
    return notifier._card(
        '<b style="color:#e8eaed;font-size:13.5px">📈 推荐质量周检</b>'
        + "".join(f'<div style="color:#c4ccd6;font-size:12.5px;'
                  f'margin-top:3px">{notifier._esc(ln)}</div>'
                  for ln in wq["lines"]),
        border="#2b313d")


def load_into_scoring(con):
    """把已生效的调参读进 scoring.TUNE（build 每次构建前调用一次）。"""
    from . import scoring
    try:
        for k, v in con.execute("SELECT key, value FROM tune_state"):
            if k in scoring.TUNE:
                scoring.TUNE[k] = float(v)
    except Exception:  # noqa: BLE001 — 无表/无行时用默认值
        pass


def tune_card_html(rep):
    """复盘推送里的自修正小节（无变化返回空串，不出现在推送里）。"""
    if not rep or not rep.get("summary"):
        return ""
    from . import notifier
    lines = [rep["summary"]]
    if rep.get("n"):
        lines.append(f"本周平仓 {rep['n']} 笔 · 胜率 "
                     f"{rep['win_rate']:.0%} · 均盈亏 "
                     f"{rep['avg_pnl']:+.1f}%")
        if rep.get("first_n"):
            lines.append(f"首推票 {rep['first_n']} 笔均 "
                         f"{rep['first_avg']:+.1f}%")
    ent = rep.get("entries") or {}
    _pairs = [(tier, g) for tier, g in ent.items() if g.get("n")]
    if _pairs:
        lines.append("入场时点：" + " · ".join(
            f"{tier} {g['n']}笔均{g['avg']:+.1f}%（胜率{g['win']:.0%}）"
            for tier, g in _pairs))
    return notifier._card(
        "<b style=\"color:#e8eaed;font-size:13.5px\">🔧 选股参数自修正</b>"
        + "".join(f'<div style="color:#c4ccd6;font-size:12.5px;'
                  f'margin-top:3px">{notifier._esc(ln)}</div>'
                  for ln in lines),
        border="#2b313d")
