# -*- coding: utf-8 -*-
"""构建编排入口：守门 → 引擎扫描 → 评分决策 → 推送渲染 → 加密发布。

用法：python -m pipeline.build [--task pre|auction|close|review|site] [--date YYYY-MM-DD]
build.py 只认参数不认调用者（SCF/Actions/本地/自动化 谁来调都一样）。
注入顺序红线：数据字段必须先注入，再推送渲染。
"""
import argparse
import json
import os
import re
from datetime import datetime

from . import core, engines, scoring, notifier, publish, techniques
from . import alerts, decisions, emotion, mktfilter, quality, recveto
from .core import (get_conn, is_trading_day, is_trading_day_cross,
                   today_str, trade_calendar)
from .fetch_daily import data_ready_for, is_trading_day_today
from .mood import compute_mood, is_limit_up


def load_json(name, default=None):
    p = os.path.join(core.CONFIG_DIR, name)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else {}


def _codes_conf(env_name, file_name):
    """自选/持仓清单：本地文件 + CI Secret（env JSON）合并去重。
    公开仓库不落自选名单——CI 用 Secret 注入。"""
    codes = load_json(file_name, [])
    env = os.environ.get(env_name)
    if env:
        try:
            extra = json.loads(env)
            if isinstance(extra, list):
                codes = list(dict.fromkeys([*codes, *extra]))
        except Exception:  # noqa: BLE001 — Secret 格式错误不阻断
            pass
    return codes


def load_holdings():
    items = load_json("holdings.json", [])
    env = os.environ.get("HOLDINGS_CONF")
    if env:
        try:
            extra = json.loads(env)
            if isinstance(extra, list):
                by = {h.get("code"): h for h in items}
                for h in extra:
                    if isinstance(h, dict) and h.get("code"):
                        by[h["code"]] = h
                items = list(by.values())
        except Exception:  # noqa: BLE001
            pass
    return items


def recent_rows(con, code, n=60, date=None):
    """最近 n 根日K（升序）。传 date 时只看 date 及以前——
    不看未来数据（未来函数防护），并由调用方校验最后一根是否就是 date。"""
    if date:
        rows = con.execute(
            "SELECT date,o,c,h,l,v FROM klines WHERE code=? AND date<=? "
            "ORDER BY date DESC LIMIT ?", (code, date, n)).fetchall()
    else:
        rows = con.execute(
            "SELECT date,o,c,h,l,v FROM klines WHERE code=? "
            "ORDER BY date DESC LIMIT ?", (code, n)).fetchall()
    return [[d, o, c, h, l, v] for d, o, c, h, l, v in reversed(rows)]


def _snapshot(con, date):
    """date 当日快照缺失 → 取数据表中**最近可得**的一批（周六复盘周五时
    快照日期为周六，含周五收盘数据，名称/市值口径仍有效）。"""
    snap = {r[0]: r for r in con.execute(
        "SELECT code, name, amt, turn, fmv FROM snapshot WHERE date=?",
        (date,)).fetchall()}
    if not snap:
        row = con.execute(
            "SELECT MAX(date) FROM snapshot WHERE date<=?",
            (core.today_str(),)).fetchone()
        if row and row[0]:
            snap = {r[0]: r for r in con.execute(
                "SELECT code, name, amt, turn, fmv FROM snapshot WHERE date=?",
                (row[0],)).fetchall()}
    return snap


def _turn20(con, code, date):
    rows = con.execute(
        "SELECT turn FROM klines WHERE code=? AND date<=? AND turn>0 "
        "ORDER BY date DESC LIMIT 20", (code, date)).fetchall()
    return sum(r[0] for r in rows) / len(rows) if rows else None


def scan_universe(con, date):
    """扫描宇宙 = 全市场快照 ∪ K线历史（并集），按市场准入过滤。

    历史 bug：宇宙只取「当日 klines 有行」的代码（实测 4654 只），
    而全市场快照有 5558 只 → 约 900 只票**从未进入过扫描视野**，
    K线同步慢一天就永久消失。改为并集后，未同步的票会作为
    「K线未更新」显式留痕，而不是静默失踪。
    """
    codes = set()
    for (c,) in con.execute(
            "SELECT DISTINCT code FROM klines WHERE code!='sh000001'").fetchall():
        codes.add(c)
    row = con.execute("SELECT MAX(date) FROM snapshot WHERE date<=?",
                      (core.today_str(),)).fetchone()
    if row and row[0]:
        for (c,) in con.execute(
                "SELECT DISTINCT code FROM snapshot WHERE date=?",
                (row[0],)).fetchall():
            codes.add(c)
    return sorted(c for c in codes if mktfilter.tradable(c[2:]))


def split_universe(con, date, snap):
    """把宇宙切成「有效标的」与「不可交易标的」两半（2026-09-13 口径修正）。

    名单源陈旧：全市场快照里混着两类**永远扫不到、也永远买不了**的代码——
      · 已退市/私有化老代码（实测 340 只，最后交易日横跨 1997~2026）
      · 未上市新股（实测 4 只：只有名字和代码，成交额为 0/None）
    它们不是「数据没抓全」，而是「标的已不存在/尚未存在」。把它们算进覆盖率
    分母会让覆盖率永远卡在 93%，并持续误报「请跑 fetch_all 补齐」，掩盖真实
    缺口。此处按「当日无成交 + 当日无K线」判定为不可交易，单独留痕，不计缺口。
    """
    last_bar = dict(con.execute(
        "SELECT code, MAX(date) FROM klines WHERE code!='sh000001' "
        "GROUP BY code").fetchall())
    alive, dead = [], []
    for code in scan_universe(con, date):
        s = snap.get(code)
        amt = s[2] if s else None
        last = last_bar.get(code)
        if (not amt or amt <= 0) and (last is None or last < date):
            dead.append(code)
        else:
            alive.append(code)
    return alive, dead


LAST_SCAN_COVERAGE = {}     # scan_all 覆盖面快照（供 build/站点/测试读取）


def scan_all(con, date):
    """全市场三池扫描（趋势/区间/波段 + 连板）→ 候选池。

    前置过滤（规格书 3.2）：ST/退/N 新股按名称剔除；成交额<1.2亿剔除；
    当日涨停剔除（归连板池）。MIN_FMV 15亿 / MIN_TURN 近20日均0.5% 在池内判。
    剔除全留痕：任何一只不进池都必须有 reason（333-五），禁止静默 continue。
    """
    holdings = {h["code"] for h in load_holdings()}
    watch = {c if c[:2] in ("sh", "sz") else
             ("sh" if c.startswith("6") else "sz") + c
             for c in _codes_conf("WATCH_CODES", "watch.json")}
    snap = _snapshot(con, date)
    zt_today = {code: streak for code, streak in con.execute(
        "SELECT code, streak FROM zt_pool WHERE date=?", (date,)).fetchall()}
    cands, skipped = [], []
    stat = {"stale": 0, "no_history": 0, "fresh": 0}
    # 数据新鲜度：宇宙中有多少只拿到了 date 当日K线（这才是"有没有扫到"的口径，
    # 不能把名称/市值等策略过滤掉的票也算成数据缺口）
    have_bar = {r[0] for r in con.execute(
        "SELECT DISTINCT code FROM klines WHERE date=?", (date,)).fetchall()}

    def mk_common(code, name, close):
        return {"code": code, "name": name, "close": close,
                "in_watch": code in watch}

    def reject(code, pool, reason):
        skipped.append({"code": code, "pool": pool, "reason": reason})

    def bars_of(code, pool):
        """取 date 及以前最近 60 根。返回 None 表示数据不可用（已留痕）。"""
        rows = recent_rows(con, code, date=date)
        if len(rows) < 30:
            stat["no_history"] += 1
            reject(code, pool, f"历史K线不足30根（{len(rows)}）")
            return None
        if rows[-1][0] != date:
            stat["stale"] += 1
            reject(code, pool, f"K线未更新至{date}（最新 {rows[-1][0]}）")
            return None
        stat["fresh"] += 1
        return rows

    def commit(c):
        """买区自洽闸门：过宽/倒挂/无盈利空间 → 不推（推出去也下不了单）。"""
        c["dist_pct"] = scoring.dist_pct(c)
        if not scoring.buy_zone_ok(c):
            reject(c["code"], c.get("pool", "-"),
                   f"买区不自洽（宽{_zone_w(c)}，目标区偏低）")
            return None
        cands.append(c)
        return c

    for code, streak in zt_today.items():        # 连板池（涨停池直接转候选）
        if code in holdings:
            reject(code, "连板", "持仓股")
            continue
        s0 = snap.get(code)
        if s0 and s0[2] is not None and s0[2] <= 0:
            reject(code, "连板", "停牌/零成交（不可买）")
            continue
        rows = bars_of(code, "连板")
        if rows is None:
            continue
        s = snap.get(code) or (None, "", None, None, None)
        plan = engines.ladderplan_plan(streak, rows[-1][2])
        c = mk_common(code, s[1], rows[-1][2])
        c.update({"pool": "连板", "streak": streak,
                  # 涨停封死 = 当日买不进 → 不入「可下单」名单，
                  # 只在竞价裁决达标（auction_adjudicate）后解除。
                  "limit_up": True,
                  "consecutive_limit_ups": streak,       # N03 字段命名分离
                  "tag": f"连板{streak}",
                  "buy_low": plan["buy_low"], "buy_high": plan["buy_high"],
                  "sell_low": plan["t1"], "sell_high": plan["t2"],
                  "stop": plan["stop"], "reach10": plan["reach10"],
                  "gap_pct": None, "t1": plan["t1"]})
        commit(c)

    codes = scan_universe(con, date)
    alive_codes, dead_codes = split_universe(con, date, snap)
    dead_set = set(dead_codes)
    for code in codes:
        num = code[2:]
        if code in holdings:
            reject(code, "-", "持仓股（已持有不再推荐）")
            continue                     # 持仓股剔出候选池（双保险之一）
        if code in dead_set:
            reject(code, "-", "退市/未上市/停牌（当日无成交，不可交易）")
            continue                     # 非数据缺口：标的本身不存在或未开盘
        s = snap.get(code)
        name = s[1] if s else ""
        amt = s[2] if s else None
        fmv = s[4] if s else None
        if s is None:
            reject(code, "-", "无当日快照（未同步）")
            continue
        if amt is not None and amt <= 0:
            reject(code, "-", "停牌/零成交（不可买）")
            continue
        if name and re.search(r"ST|\*ST|退市|退$|^N |^C ", name):
            reject(code, "-", f"名称过滤:{name}")
            continue
        if amt is not None and amt < 1.2e8:
            reject(code, "-", "成交额<1.2亿")
            continue
        if fmv is not None and fmv < engines.MIN_FMV:
            reject(code, "-", "流通市值<15亿")
            continue
        rows = bars_of(code, "-")
        if rows is None:
            continue
        close = rows[-1][2]
        pc = rows[-2][2] if len(rows) > 1 else 0
        if pc and is_limit_up(num, close, pc):
            if not any(c["code"] == code for c in cands):
                reject(code, "-", "当日涨停→归连板池")
            continue                     # 当日涨停归连板池，不进其他池
        turn20 = _turn20(con, code, date)
        r = engines.screen_uptrend(rows)
        c = None
        if r:
            plan = engines.entry_plan(rows)
            c = mk_common(code, name, close)
            c.update({"pool": "趋势", "tag": "趋势", "is_st": False,
                      "fmv": fmv,
                      "worth_score": r["worth_score"],
                      "trend_state": r["trend_state"],
                      "avg_daily": r["avg_daily"], "slope20": r["slope20"],
                      "buy_low": plan["now_zone"][0],
                      "buy_high": plan["now_zone"][1],
                      # 卖出目标区（原为 pull_zone——那是更深的第二买点，
                      # 被误当卖出区导致"卖价低于买价"的自相矛盾推送）
                      "sell_low": plan["target_zone"][0],
                      "sell_high": plan["target_zone"][1],
                      "stop": plan["stop"], "action_hint": plan["action"],
                      "entry_hint": f"四态:{plan['state']}"})
        if not c:
            r = engines.detect_stage_bottom(rows, turn20=turn20, fmv=fmv)
            if r:
                c = mk_common(code, name, close)
                c.update(r)
                c["pool"], c["tag"] = "区间", "区间"
        if not c:
            r = engines.screen_pullback_relay(rows)
            if r:
                c = mk_common(code, name, close)
                c.update(r)
                c["pool"], c["tag"] = "波段", "波段"
        if not c:
            continue                     # 三池皆不中：合规未入选，无需留痕
        commit(c)
    # 覆盖率分母 = 有效标的（剔除退市/未上市/停牌）——把不可交易的票算成
    # 「没扫到」会永远压低覆盖率并掩盖真实缺口。
    with_bar = sum(1 for c in alive_codes if c in have_bar)
    total = len(alive_codes) or 1
    LAST_SCAN_COVERAGE.clear()
    LAST_SCAN_COVERAGE.update({
        "date": date, "universe": len(alive_codes), "zt_pool": len(zt_today),
        "untradable": len(dead_codes),
        "with_bar": with_bar, "missing_bar": len(alive_codes) - with_bar,
        "stale": stat["stale"], "no_history": stat["no_history"],
        "fresh": stat["fresh"], "candidates": len(cands),
        "skipped": len(skipped),
        "coverage": round(with_bar / total * 100, 1)})
    return cands, skipped


def _zone_w(c):
    lo, hi = c.get("buy_low"), c.get("buy_high")
    return f"{(hi - lo) / lo * 100:.1f}%" if lo and hi and hi > lo else "—"


def auction_adjudicate(con, cands):
    """竞价裁决（09:25 后）：拉实际竞价高开，逐票执行竞价纪律。"""
    ladder = [c for c in cands if c["pool"] == "连板"]
    if not ladder:
        return
    lv = core.fetch_open_snapshot([c["code"] for c in ladder])
    for c in ladder:
        info = lv.get(c["code"])
        if info and info.get("open_pct") is not None:
            c["gap_pct"] = info["open_pct"]
        follow, watch = engines.auction_discipline(
            c.get("streak", 1), c.get("gap_pct") if c["gap_pct"] is not None else 99)
        # 败因否决器竞价闸（recveto，低开<-0.1% 灾难区）：证据链标注
        gate = recveto.auction_gate(c.get("gap_pct"))
        c["gate_evidence"] = gate.get("evidence", "")
        if gate["action"] == "avoid":
            follow, watch = False, False     # 低开 → 当日放弃（证据：胜率仅24%）
        # gap 未知（快照拉不到）→ 保持「次日竞价达标买」不动
        if c["gap_pct"] is not None:
            c["action"] = "现在买" if follow else ("观望" if watch else "禁买")
            if follow:
                c["hot_pick"] = True      # 🔥优选标记
                c["limit_up"] = False     # 竞价已达标 → 开盘可照价下单


def fill_outcomes(con, date):
    """T+2 结局回填：对 ≥2 个交易日前推荐的票，用今日收盘对比推荐日收盘。
    （胜率熔断闸 tag_winrate 的数据基础）"""
    days = trade_calendar(con)
    if date not in days:
        return 0
    idx = days.index(date)
    if idx < 2:
        return 0
    cutoff = days[idx - 2]
    rows = con.execute(
        "SELECT date, code, tag FROM rec_picks WHERE outcome='' AND date<=?",
        (cutoff,)).fetchall()
    n = 0
    for d0, code, tag in rows:
        base = con.execute("SELECT c FROM klines WHERE code=? AND date=?",
                           (code, d0)).fetchone()
        last = con.execute(
            "SELECT c FROM klines WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
            (code, date)).fetchone()
        if not base or not last or not base[0]:
            continue
        ret = (last[0] / base[0] - 1) * 100
        con.execute(
            "UPDATE rec_picks SET outcome=?, outcome_ret=? "
            "WHERE date=? AND code=? AND tag=?",
            ("win" if ret > 0 else "lose", round(ret, 2), d0, code, tag))
        n += 1
    con.commit()
    return n


def build(task="close", date=None):
    con = get_conn()
    date = date or today_str()
    # M04 交易日守门：日历交叉确认 + 数据就绪判断（周六可复盘周五——
    # 条件：目标日是真实交易日、当日K线已入库、fetch_stats 不早于目标日）
    certain, why = is_trading_day_cross(con, date)
    ready, ready_why = data_ready_for(con, date)
    if not certain or not ready:
        print(f"[build] {date} 拒绝构建（{why}/{ready_why}）")
        return None
    # 技巧只增不减：注册表基线守门（数量跌了直接拒绝构建）
    _tb = os.path.join(core.BASE_DIR, "tools", "baseline_techniques.json")
    _tn = 0
    if os.path.exists(_tb):
        with open(_tb, encoding="utf-8") as f:
            _tn = json.load(f).get("count", 0)
    techniques.baseline_guard(_tn)
    # T+2 结局回填 → 胜率熔断数据基础（先回填，再算胜率）
    filled = fill_outcomes(con, date)
    if filled:
        print(f"[build] outcomes filled: {filled}")
    # 涨停池统计（须在扫描前：连板池依赖 zt_pool）
    mood = compute_mood(con, date)
    # 十维情绪温度计（M05-M08）：缺失维度不补中性；达标才用于策略加权
    extra = {}
    if mood:
        extra["zha_count"] = mood.get("zhaban_count")
        extra["seal_rate"] = (mood["zt_count"] /
                              (mood["zt_count"] + mood["zhaban_count"]) * 100
                              if (mood["zt_count"] + mood["zhaban_count"]) else None)
    amt_today = con.execute(
        "SELECT SUM(amt) FROM snapshot WHERE date=?", (date,)).fetchone()[0]
    amt_prev = con.execute(
        "SELECT SUM(amt) FROM snapshot WHERE date=?",
        (core.prev_trading_day(con, date),)).fetchone()[0] \
        if core.prev_trading_day(con, date) else None
    if amt_today and amt_prev:
        extra["amt_chg"] = (amt_today / amt_prev - 1) * 100
        avg20 = con.execute(
            "SELECT AVG(s) FROM (SELECT SUM(amt) AS s FROM snapshot "
            "WHERE date<=? GROUP BY date ORDER BY date DESC LIMIT 20)",
            (date,)).fetchone()[0]
        if avg20:
            extra["heat"] = amt_today / avg20
    emo = emotion.emotion_ten(con, date, extra_dims=extra)
    print(f"[build] 情绪 {emo['score']}（{emo['label']}/{emo['phase']}）"
          f" 有效{emo['effective']}/10 覆盖{emo['coverage']:.0%} "
          f"{'达标' if emo['qualified'] else '未达标——不用于策略加权'}")
    # M09/M10：环境层晋级率与情绪表同源（zt_pool），多条件统一连乘单入口
    if emo["qualified"]:
        emotion_param = emo["score"]
    else:
        emotion_param = 50.0
    env_w = scoring.env_weights(
        mood["promote_rate"] if mood else 0.5,
        mood["zhaban_rate"] if mood else 0.30, emotion_param)
    cands, skipped = scan_all(con, date)
    # 败因否决器（吸收 recveto）：放量候选标注式降权（V1 WARN），极端拦下（VETO）
    def _vol_ratio(c):
        rows = recent_rows(con, c["code"], n=6)
        if len(rows) < 6:
            return None
        return recveto.day_vol_ratio(rows[-1][5], [r[5] for r in rows[:-1]])
    cands, vetoed = recveto.apply_veto(cands, vol_ratio_of=_vol_ratio)
    for v in vetoed:
        skipped.append({"code": v["code"], "pool": v.get("pool", "-"),
                        "reason": "败因否决器 VETO：" + v.get("veto_reason", "")})
    # 覆盖面审计：宇宙/新鲜/陈旧/缺历史 全量披露——「未扫描全部个股」不再静默
    cov = LAST_SCAN_COVERAGE
    print(f"[build] 扫描覆盖 宇宙{cov.get('universe')}只 涨停池{cov.get('zt_pool')} "
          f"数据新鲜{cov.get('fresh')} 陈旧{cov.get('stale')} "
          f"缺历史{cov.get('no_history')} → 覆盖{cov.get('coverage')}% "
          f"候选{len(cands)} 剔除{len(skipped)}")
    if cov.get("coverage", 100) < 90:
        print(f"[build][WARN] 扫描覆盖 {cov.get('coverage')}% < 90%："
              f"{cov.get('stale')} 只K线陈旧 / {cov.get('no_history')} 只缺历史，"
              "请跑 tools/fetch_all.py 补齐后再推")
    # 胜率熔断闸（推送通道；网站买点报告同款闸在 build_data 内）
    winrates = scoring.tag_winrate(con, today=date)
    cands = scoring.observe_mute(cands, winrates)
    # 评分 + 决策（先注入数据，再渲染——红线6）
    for c in cands:
        c["score"] = scoring.score_candidate(c, env_w)
        c["position"] = scoring.position_hint(c["pool"], c["score"])
        c["action"] = scoring._decide(c)
    if task == "auction":
        auction_adjudicate(con, cands)   # 实际竞价逐票裁决 + 🔥优选
    # 4.5% 窄带红线兜底（红线1，2026-09-14 实测区间池 91 只宽 6.06% 漏网）：
    # 任何引擎造出的「现在买」买区宽过 MAX_NOW_ZONE_WIDTH 时，围绕现价收窄
    # （close 必在区内）——推出去的必须是能照价挂单的窄带，不是统计区间。
    for c in cands:
        if c.get("action") != "现在买" or c.get("limit_up"):
            continue
        lo, hi, close = c.get("buy_low"), c.get("buy_high"), c.get("close")
        if not (lo and hi and close) or hi / lo - 1 <= engines.MAX_NOW_ZONE_WIDTH:
            continue
        nhi = close * 1.005
        nlo = max(lo, nhi / (1 + engines.MAX_NOW_ZONE_WIDTH))
        if nhi > nlo:
            c["buy_low"], c["buy_high"] = nlo, nhi
            c["dist_pct"] = scoring.dist_pct(c)
    # 用户口径（2026-09-13）：主推荐只放「当下就能下单买入」的票。
    # 当日已涨停（一字/封死）的票买不进 → 归「次日竞价确认」独立通道，不混入。
    # buyable_now 收紧：action=现在买 **且** 现价确实落在买区内（dist_pct==0）。
    # 历史 bug：只要 action 在 NOW_ACTIONS 就上台，把「等回踩」的票推成主推，
    # 用户点开一看现价早跳出买区——这就是「推的票不在购买区间」的直接来源。
    NOW_ACTIONS = ("现在买", "等回踩", "小仓试")
    for c in cands:
        # 单一出口：能不能照价下单只由 is_buyable_now 说了算（渲染层禁止重判）
        c["buyable_now"] = scoring.is_buyable_now(c)
    picks = scoring.compute_top_picks(
        [c for c in cands if c.get("action") in NOW_ACTIONS],
        env_w, winrates, sector_of=lambda c: c.get("sector", c["pool"]))
    ladder_next = scoring.compute_top_picks(
        [c for c in cands if c.get("action") == "次日竞价达标买"],
        env_w, winrates, sector_of=lambda c: c.get("sector", c["pool"]),
        limit=2)
    # 展示口径（2026-09-14 用户困惑整改）：可下单的票永远排在「等回踩/小仓试」
    # 前面——此前详情报告把高分的等回踩票排在首位，用户第一眼看到"不能买"，
    # 再往下才看到可买票，产生"一下说观望一下说能买"的矛盾观感。
    picks.sort(key=lambda c: (not c.get("buyable_now"), -(c.get("score") or 0)))
    for c in picks + ladder_next:
        con.execute("INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (date, c["code"], c.get("name", ""), c["tag"], c["action"],
                     c["buy_low"], c["buy_high"], c.get("stop"),
                     c.get("sell_low"), c.get("sell_high"), c["score"], "", None))
    for c in cands:
        con.execute("INSERT OR REPLACE INTO candidate_snapshots VALUES(?,?,?,?,?,?,?,?)",
                    (date, c["code"], c.get("name", ""), c["pool"], c["score"],
                     c["action"], json.dumps({"observe": c.get("observe", False),
                                              "veto_reason": c.get("veto_reason", "")},
                                             ensure_ascii=False),
                     json.dumps({k: c.get(k) for k in
                                 ("speed", "hold_days", "streak",
                                  "consecutive_limit_ups", "is_st", "fmv",
                                  "buy_low", "buy_high", "stop",
                                  "entry_hint", "cycle_hint", "trend_state",
                                  "gate_evidence", "hot_pick")},
                                ensure_ascii=False)))
    for s in skipped:                    # 333-五：未入选原因全量落库
        con.execute("INSERT OR REPLACE INTO candidate_snapshots VALUES(?,?,?,?,?,?,?,?)",
                    (date, s["code"], "", s.get("pool", "-"), 0, "未推荐",
                     json.dumps({"reason": s["reason"]}, ensure_ascii=False), "{}"))
    con.commit()
    # 信号生命周期（333-三）：推进旧信号 → 变化记录；今日 picks 建/更新决策
    cal = trade_calendar(con)
    idx = cal.index(date)
    valid_until = cal[min(idx + decisions.VALID_DAYS, len(cal) - 1)]
    for c in picks:
        d = decisions.make_decision(c, date, missing_fields=())
        d["valid_until"] = valid_until
        d["score"] = c.get("score")
        decisions.persist_decision(con, d)
    def close_of(code):
        row = con.execute(
            "SELECT c, l, h FROM klines WHERE code=? AND date=?",
            (code, date)).fetchone()
        return (row[0], row[1], row[2]) if row else None
    changes = decisions.advance_signals(con, date, close_of)
    if changes:
        print(f"[build] 信号变化 {len(changes)} 条")
    # 触发式盯盘（吸收 alerts）：止损/止盈/买点/锁定 条件命中
    trigs = alerts.build_triggers(
        decisions.active_signals(con),
        {c["code"]: close_of(c["code"]) for c in cands if close_of(c["code"])})
    # 自选股每日操作建议（用户需求 + 规格书 十：未持仓语境翻译）
    watch_codes = _codes_conf("WATCH_CODES", "watch.json")
    holdings_codes = [h["code"] for h in load_holdings()]
    watch_advice = []
    if watch_codes:
        try:
            from . import watchlist
            watch_advice = watchlist.build_watch_advice(
                con, date, watch_codes, holdings_codes)
        except Exception as e:  # noqa: BLE001 — 自选建议失败不阻断主流程
            print(f"[build] watch advice failed: {e}")
    # 推荐池胜率曲线（吸收 recperf，附录B 披露口径）
    try:
        from . import recperf
        rp = recperf.build(con)
        rp_lines = recperf.summary_lines(rp)
    except Exception:  # noqa: BLE001
        rp_lines = []
    # M35 变化式主推送（简洁）+ 详情报告落盘（HTML+JSON）
    if task in ("close", "pre", "auction"):
        # 主推荐位 = 「现在买」且现价落在买区内；其余（等回踩/小仓试/已跳出买区）
        # 一律进「等待更好买点」独立分组，并强制标注距买区 —— 不再混入备选，
        # 否则用户点开看到现价早跳出买区，就是"推的票不在购买区间"。
        first = None
        backups = []
        pending = []
        for c in picks:
            d = decisions.make_decision(c, date, missing_fields=())
            d.update({"valid_until": valid_until, "score": c.get("score"),
                      "close": c.get("close"), "dist_pct": c.get("dist_pct"),
                      "sell_low": c.get("sell_low"),
                      "sell_high": c.get("sell_high"),
                      "pool": c.get("pool"),
                      "status": "条件满足" if c.get("buyable_now")
                      else "等待确认"})
            if c.get("buyable_now"):
                if first is None:
                    first = d
                else:
                    backups.append(d)
            else:
                pending.append(d)
        # 昨日推荐今日复核（#601-B）：给「上次推的票现在怎么样了」一个闭环
        prev_review = []
        try:
            for p in prev_picks_of(con, date)[:4]:
                prev_review.append({
                    "code": p["code"], "name": p.get("name", ""),
                    "status": notifier._prev_pick_status(
                        p, cands, None, compact=True)})
        except Exception as e:  # noqa: BLE001 — 复核失败不阻断推送
            print(f"[build] prev review failed: {e}")
        cov = LAST_SCAN_COVERAGE
        ut = cov.get("untradable", 0)
        meta = {"reviewed": len(cands), "data_date": date,
                "valid_until": valid_until,
                "coverage": cov.get("coverage"),
                "universe": cov.get("universe"),
                "note": f"情绪{emo['score']}({emo['label']}/{emo['phase']})；"
                        f"覆盖{'达标' if emo['qualified'] else '不足'}；"
                        f"扫描{cov.get('universe', 0)}只/"
                        f"数据新鲜{cov.get('coverage', 0)}%"
                        + (f"（另有{ut}只退市/未上市/停牌已剔除）" if ut else "")
                        + "；评分不是上涨概率。仅含当下可下单买入的标的；"
                          "次日竞价确认通道单独列出。"}
        ladder_cards = []
        for c in ladder_next:
            d = decisions.make_decision(c, date, missing_fields=())
            d.update({"valid_until": valid_until, "score": c.get("score"),
                      "close": c.get("close"), "dist_pct": c.get("dist_pct"),
                      "sell_low": c.get("sell_low"),
                      "sell_high": c.get("sell_high"),
                      "pool": c.get("pool"), "status": "等待确认",
                      "gate_evidence": c.get("gate_evidence", "")})
            ladder_cards.append(d)
        brief = notifier.render_brief(date, first, backups, changes, meta,
                                      ladder_next=ladder_cards,
                                      pending=pending[:2],
                                      prev_review=prev_review)
        detail = notifier.render_candidates(
            f"{'盘前计划' if task=='pre' else '竞价裁决' if task=='auction' else '收盘观察'} {date}",
            picks, [f"{c['code']}: {c['old']}→{c['new']} {c['reason']}"
                    for c in changes]
            + alerts.summary_lines(trigs)
            + (["【自选股操作建议】"] +
               [f"{a.get('name','')} {a['code']}：{a['advice']}"
                for a in watch_advice] if watch_advice else [])
            + rp_lines)
        detail = notifier._clip_html(detail)
        site_data = build_data_for_site(con, date)
        site_data["watch_advice"] = watch_advice
        # 三源抽查（吸收 multi_source，M01）：对 picks 抽样交叉验证
        try:
            from . import multi_source
            site_data["xcheck"] = multi_source.quality_block(
                [c["code"] for c in picks], sample=10)
        except Exception:  # noqa: BLE001
            site_data["xcheck"] = {"skipped": True}
        notifier.save_detail_report(detail, date, site_data)
        r = notifier.push(f"build_{task}", date, brief, date=date, con=con,
                          force=_force_push())
        print(f"[build] push={r}")
    # 自选股建议独立推送（独立 biz_key，不与主报告互相吃去重）
    if watch_advice and task in ("close", "review"):
        wmd = notifier.md2html("# 自选股操作建议 " + date + "\n" + "\n".join(
            f"- **{a.get('name','')} {a['code']}**（{a['action']}）：{a['advice']}"
            + (f"｜距买区 {a['dist_pct']:+.1f}%" if a.get("dist_pct") is not None else "")
            for a in watch_advice))
        wr = notifier.push("watch_advice", date, wmd, date=date, con=con,
                           force=_force_push())
        print(f"[build] watch push={wr}")
    if task == "review":
        # AI 叙事降级链（未配置任何 key 时自动落到规则引擎，永不失败）
        from . import narrative
        text = narrative.narrate({"date": date, "mood": mood or {},
                                  "emotion": emo, "picks": picks})
        nr = notifier.push("narrative", date, notifier.md2html(text),
                           date=date, con=con, force=_force_push())
        print(f"[build] narrative push={nr}")
    return {"date": date, "candidates": len(cands), "picks": picks,
            "ladder_next": ladder_next, "emotion": emo, "changes": changes}


def _force_push():
    """ASTOCK_FORCE_PUSH=1 时绕过当日去重强制重发（用户明确要求重发时用）。

    平时恒为 False——去重是防打扰的核心，不能默认关闭。"""
    return os.environ.get("ASTOCK_FORCE_PUSH") == "1"


def prev_picks_of(con, date):
    """昨日推荐（供 #601-B 复核）。"""
    rows = con.execute(
        "SELECT code,name,tag,buy_low,buy_high,stop FROM rec_picks "
        "WHERE date < ? ORDER BY date DESC", (date,)).fetchall()
    seen, out = set(), []
    for code, name, tag, lo, hi, stop in rows:
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": name, "tag": tag,
                    "buy_low": lo, "buy_high": hi, "stop": stop})
    return out[:10]


def coverage_snapshot(con, date):
    """覆盖快照：主流程用 scan_all 写入的结果；站点单独构建时用聚合查询补算。

    历史坑：站点 *_task site* 与推送是两条独立入口，站点若不补算就会读到
    空的 LAST_SCAN_COVERAGE → 页面覆盖率显示「—」，与推送口径不一致。
    """
    cov = dict(LAST_SCAN_COVERAGE)
    if cov.get("date") == date:
        return cov
    snap = _snapshot(con, date)
    alive, dead = split_universe(con, date, snap)
    have = {r[0] for r in con.execute(
        "SELECT DISTINCT code FROM klines WHERE date=?", (date,)).fetchall()}
    with_bar = sum(1 for c in alive if c in have)
    return {"date": date, "universe": len(alive), "untradable": len(dead),
            "with_bar": with_bar, "missing_bar": len(alive) - with_bar,
            "coverage": round(with_bar / max(1, len(alive)) * 100, 1)}


def build_data_for_site(con, date):
    """网站数据（v2 完整版）：候选 + 信号生命周期 + 情绪 + 触发盯盘 +
    胜率曲线 + 变化记录 + 元信息。observe 闸全通道生效（muted → skipped 组）。"""
    from . import alerts as alerts_mod
    rows = con.execute(
        "SELECT code,name,pool,score,action,reason,extra FROM candidate_snapshots "
        "WHERE date=?", (date,)).fetchall()
    muted, shown, rejected = [], [], []
    skip_reasons = {}
    # 站点也要回答「这只能不能照价下单」：官网口径与推送同源（单一出口）
    closes = dict(con.execute(
        "SELECT code, c FROM klines WHERE date=?", (date,)).fetchall())
    n_buyable = 0
    for code, name, pool, score, action, reason, extra in rows:
        r = json.loads(reason or "{}")
        why = r.get("reason", "")
        item = {"code": code, "name": name, "pool": pool, "score": score,
                "action": action, "extra": json.loads(extra or "{}"),
                "reason": why, "buyable": False}
        if action != "未推荐" and not r.get("observe"):
            e = item["extra"]
            item["buyable"] = scoring.is_buyable_now(
                {"code": code, "action": action, "close": closes.get(code),
                 "buy_low": e.get("buy_low"), "buy_high": e.get("buy_high"),
                 "sell_high": e.get("sell_high"),
                 "limit_up": action == "次日竞价达标买"})
            if item["buyable"]:
                n_buyable += 1
        if r.get("observe"):
            muted.append(item)      # observe_muted → skipped 组，0 泄露
        elif action == "未推荐":
            # 未入选票不再混进候选列表（历史 bug：它们被当成候选展示给读者）；
            # 全量留痕仍在 candidate_snapshots，站点只带聚合统计 + 抽样
            skip_reasons[why] = skip_reasons.get(why, 0) + 1
            rejected.append(item)
        else:
            shown.append(item)
    # 抽样：数据缺口类（未更新/缺历史）优先暴露，便于发现"没扫到的票"
    SITE_SKIP_CAP = 120
    rejected.sort(key=lambda it: 0 if ("未更新" in it["reason"]
                                       or "不足30根" in it["reason"]
                                       or "无当日快照" in it["reason"]) else 1)

    def close_of(code):
        row = con.execute(
            "SELECT c, l, h FROM klines WHERE code=? AND date=?",
            (code, date)).fetchone()
        return (row[0], row[1], row[2]) if row else None

    sigs = decisions.active_signals(con)
    prices = {}
    for s in sigs:
        bar = close_of(s["code"])
        if bar:
            prices[s["code"]] = bar
    trigs = alerts_mod.build_triggers(sigs, prices)
    try:
        from . import recperf as recperf_mod
        rp = recperf_mod.build(con)
    except Exception:  # noqa: BLE001
        rp = None
    emo_row = con.execute(
        "SELECT score, effective, coverage, qualified, phase, parts "
        "FROM emotion_log WHERE date=?", (date,)).fetchone()
    if emo_row:
        emo = {"score": emo_row[0], "effective": emo_row[1],
               "coverage": emo_row[2], "qualified": bool(emo_row[3]),
               "phase": emo_row[4],
               "parts": json.loads(emo_row[5] or "{}")}
        emo["label"] = emotion.label(emo_row[0])
    else:
        emo = None
    changes = [{"code": r[0], "status": r[1], "reason": r[2]}
               for r in con.execute(
                   "SELECT code, status, status_reason FROM signals "
                   "WHERE substr(changed_at,1,10)=?", (date,)).fetchall()]
    n_reviewed = con.execute(
        "SELECT COUNT(*) FROM candidate_snapshots WHERE date=? "
        "AND action!='未推荐'", (date,)).fetchone()[0]
    n_universe = con.execute(
        "SELECT COUNT(*) FROM candidate_snapshots WHERE date=?",
        (date,)).fetchone()[0]
    # 次日竞价确认通道（当日涨停买不进 → 非即时可买，单独分组）
    ladder_next = [{"code": r[0], "name": r[1], "pool": r[2], "score": r[3],
                    "action": r[4], "extra": json.loads(r[6] or "{}")}
                   for r in con.execute(
                       "SELECT code,name,pool,score,action,reason,extra "
                       "FROM candidate_snapshots WHERE date=? AND "
                       "action='次日竞价达标买' ORDER BY score DESC LIMIT 2",
                       (date,)).fetchall()]
    # 自选股建议（build_site 独立构建站点时也生成）
    try:
        from . import watchlist as _wl
        _watch = _codes_conf("WATCH_CODES", "watch.json")
        _hold = [h["code"] for h in load_holdings()]
        watch_advice = (_wl.build_watch_advice(con, date, _watch, _hold)
                        if _watch else [])
    except Exception as e:  # noqa: BLE001
        print(f"[site] watch advice failed: {e}")
        watch_advice = []
    # 有效期 = 目标日之后第 5 个交易日（用节假日日历推算，不依赖库内未来数据）
    import datetime as _dtmod
    from . import trade_calendar as _hol
    _d = _dtmod.date.fromisoformat(date)
    _n = 0
    valid_until = date
    while _n < decisions.VALID_DAYS:
        _d += _dtmod.timedelta(days=1)
        if _hol.is_trade_day(_d):
            _n += 1
        valid_until = _d.isoformat()
    cov = coverage_snapshot(con, date)
    return {"date": date,
            "meta": {"reviewed": n_reviewed, "universe": n_universe,
                     "buyable": n_buyable,
                     "untradable": cov.get("untradable"),
                     "coverage": cov.get("coverage"),
                     "skip_reasons": skip_reasons,
                     "rule_version": decisions.RULE_VERSION,
                     "valid_until": valid_until,
                     "note": "评分不是上涨概率；未触发、未委托、未成交如实区分。",
                     "disclosure": ("口径：T+2收盘 vs 推荐日收盘｜费用/滑点未含｜"
                                    "不可成交未剔除｜样本内回溯")},
            "emotion": emo,
            "candidates": shown,
            "ladder_next": ladder_next,
            "watch_advice": watch_advice,
            "signals": sigs,
            "changes": changes,
            "triggers": trigs,
            "recperf": rp,
            "skipped": muted + rejected[:SITE_SKIP_CAP]}


def build_site(date=None):
    con = get_conn()
    date = date or today_str()
    data = build_data_for_site(con, date)
    passwords = {}
    for uid, pwd in load_json("users.json", {}).items():
        passwords[uid] = pwd
    if not passwords:
        raise SystemExit("config/users.json 为空：请先设置口令（含 owner）")
    publish.build_site(data, passwords)
    issues = publish.verify_site()
    if issues:
        raise SystemExit("部署红线终止：\n" + "\n".join(issues))
    # N12 发布记录：代码/规则/数据版本 + 回归结果关联
    _tb = os.path.join(core.BASE_DIR, "tests", "baseline.json")
    _reg = 0
    if os.path.exists(_tb):
        with open(_tb, encoding="utf-8") as f:
            _reg = json.load(f).get("total_pass", 0)
    release = {"released_at": datetime.now().isoformat(timespec="seconds"),
               "data_date": date, "rule_version": decisions.RULE_VERSION,
               "regression_pass": _reg,
               "candidates": len(data.get("candidates", [])),
               "skipped": len(data.get("skipped", []))}
    os.makedirs(core.DIST_DIR, exist_ok=True)
    with open(os.path.join(core.DIST_DIR, "release.json"), "w",
              encoding="utf-8") as f:
        json.dump(release, f, ensure_ascii=False, indent=1)
    print(f"[site] build & verify OK（N12 发布记录：{release}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="close",
                    choices=["pre", "auction", "close", "review", "site"])
    ap.add_argument("--date", default=None)
    a = ap.parse_args()   # argv 隔离：内嵌任务用 parse_known_args 的精神
    if a.task == "site":
        build_site(a.date)
    else:
        build(a.task, a.date)


if __name__ == "__main__":
    main()
