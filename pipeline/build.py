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


def _norm_code(raw):
    """600519 / sh600519 / SH600519 → sh600519 / sz000001；非法返回 None。
    ★ 2026-09-25 修（用户「跟进的股票缺乏数据」根因）：自选/持仓的代码
    形态不一致——库内全部是带前缀形态，而 WATCH_CODES Secret/本地文件里
    可能是裸码。裸码直接查 klines 必然 0 行 → 建议「数据不足」。
    在**入口边界统一归一化**，所有消费方拿到的都是带前缀形态。"""
    t = str(raw or "").strip().lower().replace(" ", "")
    if re.match(r"^(sh|sz)\d{6}$", t):
        return t
    if re.match(r"^\d{6}$", t):
        # 6/9 开头沪市，其余深市（0/2/3）；北交所 4/8 不支持——返回 None
        return ("sh" if t[0] in ("5", "6", "9") else "sz") + t
    return None


def _codes_conf(env_name, file_name):
    """自选/持仓清单：本地文件 + CI Secret（env JSON）合并、**归一化**、去重。
    公开仓库不落自选名单——CI 用 Secret 注入。非法代码静默丢弃并留痕。"""
    codes = load_json(file_name, [])
    env = os.environ.get(env_name)
    if env:
        try:
            extra = json.loads(env)
            if isinstance(extra, list):
                codes = list(dict.fromkeys([*codes, *extra]))
        except Exception:  # noqa: BLE001 — Secret 格式错误不阻断
            pass
    out, dropped = [], []
    for c in codes:
        nc = _norm_code(c)
        if nc:
            if nc not in out:
                out.append(nc)
        else:
            dropped.append(c)
    if dropped:
        print(f"[codes] {env_name}/{file_name} 丢弃非法代码: {dropped}")
    return out


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


def split_universe(con, date, snap, asof=None):
    """把宇宙切成「有效标的」与「不可交易标的」两半（2026-09-13 口径修正）。

    名单源陈旧：全市场快照里混着两类**永远扫不到、也永远买不了**的代码——
      · 已退市/私有化老代码（实测 340 只，最后交易日横跨 1997~2026）
      · 未上市新股（实测 4 只：只有名字和代码，成交额为 0/None）
    它们不是「数据没抓全」，而是「标的已不存在/尚未存在」。把它们算进覆盖率
    分母会让覆盖率永远卡在 93%，并持续误报「请跑 fetch_all 补齐」，掩盖真实
    缺口。此处按「当日无成交 + 当日无K线」判定为不可交易，单独留痕，不计缺口。

    `asof`：**判「不可交易」的基准日**，默认 = date。
    ⚠️ 2026-09-16 二次修（血案：盘前推送「宇宙 0 只 / 候选 0 只」）：
    盘前(08:50) 当日快照成交额**天然全 0**（集合竞价未开始），用它做
    「无成交 ⇒ 停牌/退市」判定会把**全市场**判死 —— CI 实测 alive=0 /
    dead=4937 / 覆盖 0.0%，用户收到一份没有任何标的的盘前计划。
    因此盘前/竞价任务必须传 `asof=上一交易日`，并用**该日快照**做判定
    （由 scan_all 统一保证：snap 与 asof 同源）。"""
    ref = asof or date
    last_bar = dict(con.execute(
        "SELECT code, MAX(date) FROM klines WHERE code!='sh000001' "
        "GROUP BY code").fetchall())
    alive, dead = [], []
    for code in scan_universe(con, date):
        s = snap.get(code)
        amt = s[2] if s else None
        last = last_bar.get(code)
        if (not amt or amt <= 0) and (last is None or last < ref):
            dead.append(code)
        else:
            alive.append(code)
    return alive, dead


LAST_SCAN_COVERAGE = {}     # scan_all 覆盖面快照（供 build/站点/测试读取）


def scan_all(con, date, bar_anchor=None):
    """全市场三池扫描（趋势/区间/波段 + 连板）→ 候选池。

    前置过滤（规格书 3.2）：ST/退/N 新股按名称剔除；成交额<1.2亿剔除；
    当日涨停剔除（归连板池）。MIN_FMV 15亿 / MIN_TURN 近20日均0.5% 在池内判。
    剔除全留痕：任何一只不进池都必须有 reason（333-五），禁止静默 continue。

    `bar_anchor`：新鲜度锚定日（默认 = date）。
    ⚠️ 2026-09-16 修（血案：盘前推送「候选 0 只」）：
    pre（08:50）/ auction（09:25）在设计上就跑在**当日收盘K线入库之前**，
    此时 `klines` 最新只有上一交易日。旧实现用 `rows[-1][0] != date` 判陈旧
    ⇒ 全市场 4937 只被判「K线未更新至{date}」⇒ 覆盖 0.0%、候选 0
    ⇒ 用户收到一份**没有任何标的的盘前计划**（比收不到更让人困惑）。
    修法：盘前任务把锚改到**上一交易日**（由 build 传入），收盘任务保持 date。
    """
    index_rows = recent_rows(con, "sh000001", n=25)
    holdings = {h["code"] for h in load_holdings()}
    watch = {c if c[:2] in ("sh", "sz") else
             ("sh" if c.startswith("6") else "sz") + c
             for c in _codes_conf("WATCH_CODES", "watch.json")}
    # ⚠️ 2026-09-16 二次修（血案：盘前/竞价推送「候选 0 只」）：
    # 上一次只修了「K线新鲜度锚定」（bar_anchor），**快照口径漏了**——而
    # 停牌判定(split_universe)与流动性门槛(成交额<1.2亿) 全走当日快照：
    #   · pre(08:50)：当日快照成交额全 0 ⇒ 全市场判「停牌/退市」⇒ 宇宙 0；
    #   · auction(09:25)：当日只有竞价撮合额（全市场约 114 亿）⇒ 逐票
    #     远低于 1.2 亿门槛 ⇒ 4403 只被剔除 ⇒ 候选 0。
    # 本地受控复现（把当日快照 amt 置 0 / 压到 114 亿）逐条复刻了 CI 日志：
    #   盘前 alive=0 dead=4937 universe=0 coverage=0.0% cands=0
    #   竞价 4403 只死于「成交额<1.2亿」cands=0
    # 修法：盘前/竞价的**筛选口径一律取锚定日（上一交易日）快照**——
    # 选股本就应该看最近一个已收盘交易日的量能与市值，今天还没发生的
    # 成交量不构成任何判定依据。收盘/复盘任务 bar_anchor=None ⇒ 仍用当日。
    snap_date = bar_anchor or date
    snap = _snapshot(con, snap_date)
    zt_today = {code: streak for code, streak in con.execute(
        "SELECT code, streak FROM zt_pool WHERE date=?", (date,)).fetchall()}
    cands, skipped = [], []
    stat = {"stale": 0, "no_history": 0, "fresh": 0}
    # 新鲜度锚：盘前任务（pre/auction）当日K线尚未产生，锚定上一交易日；
    # 收盘/复盘任务锚定当日。两者语义不同，混用会全量误判（见函数注释）。
    expected_bar = bar_anchor or date
    # 数据新鲜度：宇宙中有多少只拿到了锚定日K线（这才是"有没有扫到"的口径，
    # 不能把名称/市值等策略过滤掉的票也算成数据缺口）
    have_bar = {r[0] for r in con.execute(
        "SELECT DISTINCT code FROM klines WHERE date=?",
        (expected_bar,)).fetchall()}

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
        # ⚠️ 2026-09-16 修（血案：盘前日志「数据新鲜0 陈旧19」误导排查）：
        # 原为 `!= expected_bar` —— 把「K线**比锚定日更新**」的票也判成陈旧。
        # 判陈旧的语义是「数据**不够新**」，理应只对 `rows[-1][0] < expected_bar`
        # 成立；实测 auction 那 19 只正是已拿到当日实时K线的票，本该算新鲜。
        # 日期为 ISO 格式，字典序即时间序，字符串比较安全。
        if rows[-1][0] < expected_bar:
            stat["stale"] += 1
            reject(code, pool,
                   f"K线未更新至{expected_bar}（最新 {rows[-1][0]}）")
            return None
        stat["fresh"] += 1
        return rows

    def commit(c):
        """买区自洽闸门：过宽/倒挂/无盈利空间 → 不推（推出去也下不了单）。"""
        # RS 超额动量 + 决断力 + Alpha 因子 + 唐奇安突破（2026-09-25）：
        # 统一在入口注入一次，三池候选卡都能展示「为什么它不磨叽」的硬数据。
        _r = recent_rows(con, c["code"], n=60)
        c["rs_mom"] = engines.rs_momentum(_r, index_rows)
        if not c.get("decisive"):
            c["decisive"] = engines.decisive_stats(_r)
        c["alpha"] = engines.alpha_extras(_r)
        c["donchian"] = engines.donchian_breakout(_r)
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
    alive_codes, dead_codes = split_universe(con, date, snap, asof=snap_date)
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
                      "entry_hint": f"四态:{plan['state']}",
                      # 决断力证据（卡片展示：为什么它不磨叽）
                      "decisive": engines.decisive_stats(rows)})
        # ★ 一字板标注（用户 2026-09-22「推送的大部分都是涨停而且一字」）：
        # low==high 且涨停 = 全天无成交机会，当日起就买不进 → 只保留在
        # 连板观察通道并显式标注，绝不进"当下可买"主推。
        if c and c.get("pool") == "连板" and rows:
            _l, _h = rows[-1][4], rows[-1][3]
            _pc = rows[-1][2] / rows[-2][2] - 1 if rows[-2][2] else 0
            if _l == _h and _pc >= 0.095:
                c["yizi"] = True
                c["yizi_note"] = "一字板，全天无买入机会"
        # ★ 高/中/低位标签（用户 2026-09-22）：20 日区间位置
        if c and rows:
            _w = rows[-20:]
            _hi = max(r[3] for r in _w)
            _lo = min(r[4] for r in _w)
            if _hi > _lo:
                _pos = (c["close"] - _lo) / (_hi - _lo)
                c["pos_label"] = ("低位" if _pos < 0.33 else
                                  "中位" if _pos < 0.66 else "高位")
                c["pos_pct"] = round(_pos * 100, 1)
        # ★ 决断门控（用户 2026-09-18：不要推荐磨磨唧唧的股票）。
        # 趋势票也必须"走得出来"——缓坡震荡（net 为正但一路回撤、eff 低）
        # 同样属于磨叽，剔除。标准慢牛（稳定爬升）eff 高 → 放行。
        if c and c.get("pool") == "趋势" and not engines.screen_decisive(rows):
            reject(code, "趋势", "趋势过缓/来回震荡（磨磨唧唧，不推荐）")
            c = None
        if not c:
            r = engines.detect_stage_bottom(rows, turn20=turn20, fmv=fmv)
            if r:
                # ★ 箱体/区间池本质是横盘震荡 = 用户说的磨磨唧唧，直接剔除，
                # 不进候选池（reject 留痕，不静默 continue）。
                reject(code, "-", "横盘震荡（磨磨唧唧，不推荐）")
                continue
        if not c:
            r = engines.screen_pullback_relay(rows)
            if r:
                c = mk_common(code, name, close)
                c.update(r)
                c["pool"], c["tag"] = "波段", "波段"
                # ★ 决断门控同样覆盖波段池（2026-09-19 用户重申「要么上要么下」）：
                # 涨停回马枪若近 20 日整体走成横盘（净位移/效率不足），
                # 说明 D0 之后动能已经散掉，同样不推。
                if not engines.screen_decisive(rows):
                    reject(code, "波段", "波段动能衰减/横盘（磨磨唧唧，不推荐）")
                    c = None
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


def _preauction_ready(con, date):
    """盘前/竞价任务的专用就绪判定（2026-09-15 新增）。

    语义纠错：pre（08:50）与 auction（09:25）在设计上就跑在**当日收盘
    K线入库之前**——它们要的是「历史K线 + 当日竞价快照」，不是当日收盘K线。
    而原实现统一套用 is_trading_day_cross + data_ready_for（二者都以
    「指数日K含当日」为必要条件，日历源自指数K线），导致这两个任务在
    正常情况下**永远无法通过闸门** → build 静默 return → 盘前/竞价永不推送。

    本判定要求：① 日历/数据库里有上一交易日（保证历史K线可用）；
    ② 当日快照已入库（保证竞价数据在新）；③ 日历确认 date 是工作日。
    返回 (ok, reason)。"""
    from .trade_calendar import is_trade_day as _cal_trade
    if not _cal_trade(date):
        return False, "非法定交易日"
    prev = core.prev_trading_day(con, date)
    if not prev:
        return False, "无上一交易日（历史K线缺失）"
    n_prev = con.execute("SELECT COUNT(*) FROM klines WHERE date=?",
                         (prev,)).fetchone()[0]
    if n_prev == 0:
        return False, f"{prev} 无K线（历史数据未就绪）"
    n_snap = con.execute("SELECT COUNT(*) FROM snapshot WHERE date=?",
                         (date,)).fetchone()[0]
    if n_snap == 0:
        return False, f"{date} 无快照（竞价数据未入库）"
    # ★ 2026-09-16 新增（血案：闸门放行了「用不了」的数据 ⇒ 推空计划）：
    # 盘前/竞价的筛选口径是**上一交易日收盘快照**（见 scan_all 注释），所以
    # 「就绪」必须同时确认 prev 日快照有真实成交额。旧实现只查当日快照**行数**
    # 就放行，而 08:50 当日快照行数正常（5559 行）但成交额全 0 ⇒ 放行后
    # split_universe 判全市场停牌 ⇒ 宇宙 0 / 候选 0 ⇒ 用户收到一份空计划。
    # 校验锚定日快照有效性，才能在下游口径失效前就拦住。
    n_psnap = con.execute(
        "SELECT COUNT(*) FROM snapshot WHERE date=? AND amt>0",
        (prev,)).fetchone()[0]
    if n_psnap == 0:
        return False, (f"{prev} 无有效快照（成交额全空）"
                       "——盘前筛选口径不可用")
    # ⚠️ 2026-09-16 修（血案：盘前任务被自家闸门挡住）：
    # 本函数**曾经**有一处「快照 pct 全零 ⇒ 疑似休市日 ⇒ 拒绝构建」的分支。
    # 它对 pre/auction 是**必然误判**——08:50 集合竞价尚未开始，快照涨跌幅
    # 天然全 0；09:25 竞价刚结束时接口也可能尚未刷新。实测该判定让
    # pre/auction 只能发出一条「数据未就绪」，用户拿不到盘前计划/竞价裁决，
    # 与 fetch_daily.guard_snapshot 的 ValueError 同源（同一个错误判定的
    # 第二处副本）。
    # **现已彻底删除该分支**（不是加开关旁路）：留着死代码就是留一颗雷——
    # 后人只要把开关翻成 True 就会重新踩坑。盘前时段「全零」是**时点属性**，
    # 不是日历证据；休市由权威日历 `trade_calendar`（国务院放假安排）在
    # 上方 `_cal_trade(date)` 把关，那才是可靠信号。
    # 注：收盘路径（core.is_trading_day_cross）**保留**全零判定 ——
    # 15:22 收盘后全零确实是休市/数据异常的真信号，两者语义不同。
    return True, (f"盘前/竞价就绪（前值{prev} K线 {n_prev} 只 + "
                  f"当日快照 {n_snap} 只）")


def _notify_data_blocked(task, date, why, ready_why):
    """数据未就绪时主动告警（2026-09-15 新增，堵「全天零提示」静默洞）。

    背景：原实现在 not certain or not ready 时仅 print + return None，
    推送端完全静默。当日抓取若超时/失败 → 数据未入库 → 构建拒绝 → 用户
    一整天收不到任何消息且毫不知情（8:50 failure / 9:25 cancel 实证）。
    现改为：数据未就绪一律发一条明确的「数据未就绪」告警，让用户知道
    系统活着但数据没上来，而不是无声无息。告警失败不阻断主流程。"""
    try:
        from . import notifier
        t = (f"⚠️ {date} 数据未就绪，本次 {task} 未生成"
             f"（{ready_why or why}）")
        body = notifier.md2html(
            f"**{task}** 任务已触发，但目标日 `{date}` 的数据未通过就绪校验，"
            f"为避免用错数据误导决策，本次**不生成候选也不推送分析**。\n\n"
            f"- 日历判定:{why}\n"
            f"- 就绪判定:{ready_why}\n\n"
            "常见原因：当日行情抓取超时/失败，或非交易日。"
            "系统会在下个时点自动重试；若连续多次收到本提示，"
            "请检查数据源连通性。")
        notifier.push(f"data_blocked_{task}", t, body, date=date)
        print(f"[build] 已发数据未就绪告警：{date} {task}")
    except Exception as e:  # noqa: BLE001 — 告警失败不得影响主流程
        print(f"[build] 数据未就绪告警发送失败（忽略）：{type(e).__name__} {e}")


def _notify_holiday(date):
    """休市日提示（2026-09-16 新增）：**一天最多一条**，不按 task 分流。

    背景：外部定时器 cron-job.org 的 `wdays` 只能排除周六日（0=周日…
    6=周六），**不认法定节假日** —— 国庆/春节照样点火，四个任务
    pre/auction/close/review 全部走到「拒绝构建」。旧实现用
    mode=`data_blocked_{task}` ⇒ 四个不同 mode 互不拦截 ⇒
    **同一天发出 4 条「数据未就绪」告警**，把"系统正常休市"误报成"数据出问题"。

    修法：休市统一用固定 mode `data_holiday`。日级保险丝按 **mode+date**
    去重 ⇒ 当天第一条发出后，其余三条自动被拦 ⇒ 用户每天只收到一条
    明确的休市提示，且措辞说明这是正常休市、不是故障。

    与 `_notify_data_blocked` 的分工：
      - 休市（日历判定为假）→ 本函数，一天一条，措辞=正常休市；
      - 交易日但数据没到位 → `_notify_data_blocked`，按 task 分流，
        那是真异常，需要用户知道去查数据源。
    """
    try:
        from . import notifier
        from .trade_calendar import why_closed
        why = why_closed(date) or "非法定交易日"
        t = f"休市提醒 · {date}（{why}），今日无分析推送"
        body = notifier.md2html(
            f"**{date}** {why}，A股今日不开市。\n\n"
            "- 不抓取行情、不生成候选、不推送分析。\n"
            "- 下一个交易日 08:50 会自动恢复盘前计划推送。\n"
            "- 这是系统的**正常休市提示，不是故障**，无需处理。")
        notifier.push("data_holiday", t, body, date=date)
        print(f"[build] 已发休市提示：{date}（{why}）")
    except Exception as e:  # noqa: BLE001 — 提示失败不得影响主流程
        print(f"[build] 休市提示发送失败（忽略）：{type(e).__name__} {e}")


def build(task="close", date=None, period_days=30):
    con = get_conn()
    date = date or today_str()
    # ★ 用户需求①：半月/月度周期复盘（盈利最大化 + 系统改进建议）。
    # 独立于选股主链：不依赖当日数据就绪/交易日判定，直接聚合历史账户与行情，
    # 故在休市门与就绪门之前早退；可由 cron/自动化在每月 1 日、16 日触发。
    if task == "period":
        return _build_period(con, date, period_days)
    # ★ 演练模式（2026-09-19 用户「全部在网络上运行一次，该推送的全部推送」）：
    # ASTOCK_REHEARSAL=1 时锚定**最近交易日**（而非今天），让周末也能在 CI
    # 上用真实数据走完 pre/auction/close/review 全链路并真实推送
    # （notifier 侧 mode 加 rehearsal_ 前缀，与正式推送的日熔丝完全隔离，
    # 不影响周一）。仅演练启用——正常调度的行为零变化。
    if (os.environ.get("ASTOCK_REHEARSAL") == "1"
            and task in ("pre", "auction", "close", "review")):
        date = trade_calendar(con)[-1]
        print(f"[build] 演练模式：锚定最近交易日 {date}")
    # ★ 2026-09-16 新增：休市日（周末 / 法定节假日）**第一道门**就早退。
    # 放在所有就绪判定之前，避免休市日还去做快照/K线检查、更避免
    # 四个任务各发一条告警（详见 _notify_holiday 注释）。
    # 交易日完全不受影响（is_trade_day 为 True 时不进入本分支）。
    from .trade_calendar import is_trade_day as _cal_trade
    if not _cal_trade(date):
        print(f"[build] {date} 非法定交易日 → 跳过构建（休市）")
        _notify_holiday(date)
        return None
    # M04 交易日守门：日历交叉确认 + 数据就绪判断（周六可复盘周五——
    # 条件：目标日是真实交易日、当日K线已入库、fetch_stats 不早于目标日）
    # 2026-09-15：pre/auction 走专用闸门（它们本就在当日收盘K线入库前运行，
    # 见 _preauction_ready 注释）——此前套用收盘闸门导致这两个任务永不通过。
    if task in ("pre", "auction"):
        # ⚠️ 2026-09-16：`pre`（08:50）集合竞价未开始、`auction`（09:25）集合
        # 竞价刚结束/接口尚未刷新——两者都**可能出现快照 pct 全 0 或大面积 0**，
        # 这不是休市证据。实测 09-16 08:50 定时任务因该判定被拒 ⇒ 用户只收到
        # 一条「数据未就绪」而拿不到盘前计划（与 fetch 侧 ValueError 同源）。
        # 修法：**pre/auction 一律不做全零判定**。休市由权威日历
        # `trade_calendar`（国务院放假安排）在上方把关 —— 那是可靠信号，
        # 快照全零只是**时点属性**，不是日历证据。
        ready, ready_why = _preauction_ready(con, date)
        certain, why = (ready, "盘前/竞价专用判定"
                        if ready else ready_why)
    else:
        certain, why = is_trading_day_cross(con, date)
        ready, ready_why = data_ready_for(con, date)
    if not certain or not ready:
        print(f"[build] {date} 拒绝构建（{why}/{ready_why}）")
        # 2026-09-15：拒绝构建不再静默——主动告警用户（详见函数注释）
        _notify_data_blocked(task, date, why, ready_why)
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
    # ⚠️ 2026-09-16：pre/auction 的K线新鲜度锚定**上一交易日**——它们跑在
    # 当日收盘K线入库之前，若锚定当日会把全市场判为陈旧 ⇒ 候选 0（详见
    # scan_all 注释）。close/review 仍锚定当日（bar_anchor=None）。
    _bar_anchor = (core.prev_trading_day(con, date)
                   if task in ("pre", "auction") else None)
    if _bar_anchor:
        print(f"[build] 盘前任务：K线新鲜度锚定上一交易日 {_bar_anchor}")
    cands, skipped = scan_all(con, date, bar_anchor=_bar_anchor)
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
    # ★ 2026-09-16 口径错配保险丝（血案：用户收到"没有标的的计划"）：
    # 上游已修 K线新鲜度锚定 + 快照口径两层，这里留最后一道闸——
    # 一旦再次出现「宇宙 0」或「候选 0 且覆盖不达标」，宁可发一条明确的
    # 「数据口径异常」告警，也**绝不推空壳**：一份没有任何标的的"计划"
    # 比收不到更让人困惑（用户原话）。
    _u = cov.get("universe", 0)
    _covp = cov.get("coverage", 0)
    _ncand = len(cands)
    if _u == 0 or (_ncand == 0 and _covp < 90):
        print(f"[build] 口径异常（宇宙{_u} 覆盖{_covp}% 候选{_ncand}）"
              "→ 拒绝推送空计划，改发告警")
        _notify_data_blocked(
            task, date,
            f"扫描口径异常（宇宙 {_u} 只 / 覆盖 {_covp}%）",
            f"候选 {_ncand} 新鲜 {cov.get('fresh')} 陈旧 {cov.get('stale')} "
            f"缺历史 {cov.get('no_history')}")
        return None
    # ★ 板块热度标注（2026-09-18 用户需求：推荐里要标注板块热度）。
    # 同时修掉两处**静默失效**：scoring 的 `sector_temp` 冷热因子此前无数据源
    # （恒 None，加成从未生效），`sector_of` 退化成"按池别去重"（不同行业的
    # 波段票互斥，每次只活一只）。详见 pipeline/sector.py 的模块注释。
    # 全链路 try/except：板块只是标注，拿不到就"不标注"，绝不影响推荐。
    sector_board, hot_sectors = [], []
    try:
        from . import sector as sector_mod
        _board = sector_mod.refresh(con, date)
        sector_board, hot_sectors = sector_mod.annotate(con, date, cands,
                                                        board=_board)
        _n_marked = sum(1 for c in cands if c.get("sector"))
        # 板块阶段 + 主线/副线（用户 2026-09-22：「是不是主线高潮板块、
        # 接力板块，还是退潮的」）。退潮板块的候选在下方被直接否决。
        _rank_map = {s.get("sector"): i for i, s in
                     enumerate(hot_sectors[:3])}
        for c in cands:
            sec = c.get("sector")
            if not sec:
                continue
            st_, dt_ = sector_mod.sector_state(con, date, sec,
                                               temp=c.get("sector_temp"))
            c["sector_state"] = st_
            c["sector_state_note"] = dt_
            if sec in _rank_map:
                c["mainline"] = "主线" if _rank_map[sec] == 0 else "副线"
        # 兜底：板块标注未覆盖的候选设为"未知"——保证 板块热度/板块阶段
        # 行在每张卡上都渲染（用户 09-25：「什么板块都不告诉我」）
        for c in cands:
            if not c.get("sector"):
                c["sector"] = "未知"
                c["sector_temp"] = ""
        print(f"[build] 板块热度 {len(sector_board)} 个行业，候选标注 {_n_marked}"
              f"/{len(cands)} 只；前三 {' '.join(s['sector'] for s in hot_sectors[:3])}")
    except Exception as e:  # noqa: BLE001 — 板块标注失败不得阻断主链
        print(f"[build] 板块标注失败（不影响主链）：{type(e).__name__} {e}")
    # ★ 板块退潮否决（2026-09-21 用户：「考虑板块更换周期，不要才进去就
    # 暴跌」）：候选所属板块若处于退潮（3日累计≤-3% 或 连跌≥2日且最新≤-1%），
    # 直接否决——个股再强也容易陪板块补跌。留痕进 skipped，绝不静默。
    try:
        from . import sector as _sec
        _still = []
        for c in cands:
            sec = c.get("sector")
            ret = _sec.retreat_signal(con, date, sec) if sec else None
            if ret and ret.get("retreat"):
                skipped.append({"code": c["code"], "pool": c.get("pool", "-"),
                                "reason": f"板块退潮不接刀：{sec} "
                                          f"{ret['detail']}"})
                continue
            _still.append(c)
        if len(_still) < len(cands):
            print(f"[build] 板块退潮否决 {len(cands) - len(_still)} 只")
        cands = _still
    except Exception as e:  # noqa: BLE001 — 退潮检测失败不得阻断主链
        print(f"[build] 板块退潮检测失败（不影响主链）：{e}")
    # 行情档位 → 推荐配额（用户口径：行情好时不再限制 3 只，全部推荐）
    heat_level, pick_limit, per_sector, ladder_cap = scoring.market_heat(emo)
    if pick_limit is None:
        print(f"[build] 行情{heat_level}（情绪{emo.get('score')}）→ 放开限量："
              f"全部符合条件标的（同板块≤{per_sector}）")
    else:
        print(f"[build] 行情{heat_level}（情绪{(emo or {}).get('score')}）→ "
              f"维持 TOP{pick_limit}")
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
    # 躺榜统计（用户 2026-09-19「不要几天横排在那里动都不动」）：
    # 同一只票在最近 5 个交易日的推荐位上反复出现（动作未兑现、无结局回填）
    # 的天数。>=5 日直接移出推荐；1~4 日由终审按 8%/日折价挤出。
    _wdays = _wait_days_map(con, date)
    for c in cands:
        # 单一出口：能不能照价下单只由 is_buyable_now 说了算（渲染层禁止重判）
        c["buyable_now"] = scoring.is_buyable_now(c)
        wd = _wdays.get(c["code"], 0)
        if wd:
            c["wait_days"] = wd
    _stale = [c["code"] for c in cands if c.get("wait_days", 0) >= 5]
    picks = scoring.compute_top_picks(
        [c for c in cands if c.get("action") in NOW_ACTIONS
         and c.get("wait_days", 0) < 5],
        env_w, winrates, sector_of=lambda c: c.get("sector") or c["pool"],
        limit=pick_limit, per_sector=per_sector, ladder_cap=ladder_cap)
    ladder_next = scoring.compute_top_picks(
        [c for c in cands if c.get("action") == "次日竞价达标买"
         and not c.get("yizi")],
        env_w, winrates, sector_of=lambda c: c.get("sector") or c["pool"],
        limit=pick_limit if pick_limit is None else 2,
        per_sector=per_sector, ladder_cap=ladder_cap)
    for c in cands:
        if c.get("yizi") and c.get("action") == "次日竞价达标买":
            skipped.append({"code": c["code"], "pool": "连板",
                            "reason": "一字板无法买入，仅观察（次日竞价确认）"})
    for code in _stale:
        skipped.append({"code": code, "pool": "-",
                        "reason": "连续>=5日挂推荐位未兑现，自动移出（不让名单躺平）"})

    # 展示口径（2026-09-14 用户困惑整改）：可下单的票永远排在「等回踩/小仓试」
    # 前面——此前详情报告把高分的等回踩票排在首位，用户第一眼看到"不能买"，
    # 再往下才看到可买票，产生"一下说观望一下说能买"的矛盾观感。
    picks.sort(key=lambda c: (not c.get("buyable_now"), -(c.get("score") or 0)))
    for c in picks + ladder_next:
        con.execute("INSERT OR REPLACE INTO rec_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (date, c["code"], c.get("name", ""), c["tag"], c["action"],
                     c["buy_low"], c["buy_high"], c.get("stop"),
                     c.get("sell_low"), c.get("sell_high"),
                     c.get("eff_score") or c["score"], "", None))
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
                                  "gate_evidence", "hot_pick",
                                  "wait_days", "decisive", "rs_mom",
                                  "pos_label", "pos_pct", "yizi",
                                  "yizi_note", "alpha", "donchian")},
                                ensure_ascii=False)))
    for s in skipped:                    # 333-五：未入选原因全量落库
        con.execute("INSERT OR REPLACE INTO candidate_snapshots VALUES(?,?,?,?,?,?,?,?)",
                    (date, s["code"], "", s.get("pool", "-"), 0, "未推荐",
                     json.dumps({"reason": s["reason"]}, ensure_ascii=False), "{}"))
    con.commit()
    # 信号生命周期（333-三）：推进旧信号 → 变化记录；今日 picks 建/更新决策
    cal = trade_calendar(con)
    # ⚠️ 2026-09-16 修（CI run 35000871359 实证）：
    # 原写法 `idx = cal.index(date)` 在日历不含 date 时抛
    # `ValueError: '2026-09-15' is not in list` → 整个 build 崩 → 推送失败。
    # 触发场景真实存在：K线已入库但 `trade_calendar()`（基于**指数**日K）
    # 尚未含当日——指数补拉失败/滞后时会这样，数据本身是好的
    # （同一次日志里 扫描覆盖=100.0%、宇宙4588只 全部正常）。
    # 改为**容错定位**：找不到就用「最后一个 ≤ date 的交易日」，
    # 日历为空则退化为只算 VALID_DAYS 之外的宽松窗口，绝不抛异常。
    try:
        idx = cal.index(date)
    except ValueError:
        idx = max((i for i, d in enumerate(cal) if d <= date), default=-1)
        print(f"[build] 交易日历不含 {date}（日历末位 "
              f"{cal[-1] if cal else '空'}）→ 退化用 idx={idx} 计有效窗口")
    valid_until = cal[min(idx + decisions.VALID_DAYS, len(cal) - 1)]
    for c in picks:
        d = decisions.make_decision(c, date, missing_fields=())
        d["valid_until"] = valid_until
        d["score"] = c.get("eff_score") or c.get("score")
        decisions.persist_decision(con, d)
    # 卡片显示字段透传（confirms/板块阶段/主线/位置/池别/alpha/周期）
    _CARD_KEYS = ("confirms", "confirm_note", "mainline", "sector_state",
                  "sector_state_note", "pos_label", "pool", "alpha",
                  "donchian", "wait_days", "hold_days", "hold_limit",
                  "phase", "cycle_hint", "yizi_note")
    # ★ 卡片标签回读（2026-09-25）：从 candidate_snapshots.extra 恢复
    # 板块阶段/主线/位置/一字/alpha/donchian —— 确保任何路径构建卡片都
    # 有完整标签，不依赖内存中的候选 dict 生命周期。
    def _extra_of(code):
        r = con.execute(
            "SELECT extra FROM candidate_snapshots WHERE code=? AND date=?",
            (code, date)).fetchone()
        if r and r[0]:
            try:
                return json.loads(r[0])
            except Exception:  # noqa: BLE001
                return {}
        return {}
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
            d.update({"valid_until": valid_until,
                      "score": c.get("eff_score") or c.get("score"),
                      "close": c.get("close"), "dist_pct": c.get("dist_pct"),
                      "sell_low": c.get("sell_low"),
                      "sell_high": c.get("sell_high"),
                      "status": "条件满足" if c.get("buyable_now")
                      else "等待确认"})
            d.update(_sector_fields(c))
            d.update({k: c.get(k) for k in _CARD_KEYS if c.get(k) is not None})
            _ex = _extra_of(c["code"])
            d.update({k: _ex[k] for k in _CARD_KEYS
                      if k in _ex and d.get(k) is None})
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
        _cc = _confirm_counts(con, date)
        for c in picks + ladder_next:
            n_conf = _cc.get(c["code"], 0)
            c["confirms"] = n_conf
            c["confirm_note"] = ({3: "三确认（强）",
                                  2: "双确认"}.get(n_conf)
                                 or f"{n_conf} 时点在列")
        _vd_level, _vd_text = today_verdict(emo, mood)
        meta = {"reviewed": len(cands), "data_date": date,
                "verdict": _vd_level, "verdict_text": _vd_text,
                "valid_until": valid_until,
                "coverage": cov.get("coverage"),
                "universe": cov.get("universe"),
                "heat_level": heat_level,          # 行情档位（2026-09-18）
                "hot_sectors": hot_sectors,        # 板块涨幅榜 TOP N
                "pick_limit": pick_limit,          # None = 不限量
                "note": f"情绪{emo['score']}({emo['label']}/{emo['phase']})；"
                        f"行情{heat_level}"
                        + ("（好，已放开至全部符合条件标的）"
                           if pick_limit is None else "（按 TOP%d 纪律）" % pick_limit)
                        + "；"
                        f"覆盖{'达标' if emo['qualified'] else '不足'}；"
                        f"扫描{cov.get('universe', 0)}只/"
                        f"数据新鲜{cov.get('coverage', 0)}%"
                        + (f"（另有{ut}只退市/未上市/停牌已剔除）" if ut else "")
                        + "；评分不是上涨概率。仅含当下可下单买入的标的；"
                          "次日竞价确认通道单独列出。"}
        ladder_cards = []
        for c in ladder_next:
            d = decisions.make_decision(c, date, missing_fields=())
            d.update({"valid_until": valid_until,
                      "score": c.get("eff_score") or c.get("score"),
                      "close": c.get("close"), "dist_pct": c.get("dist_pct"),
                      "sell_low": c.get("sell_low"),
                      "sell_high": c.get("sell_high"),
                      "status": "等待确认",
                      "gate_evidence": c.get("gate_evidence", "")})
            d.update(_sector_fields(c))
            d.update({k: c.get(k) for k in _CARD_KEYS if c.get(k) is not None})
            ladder_cards.append(d)
        brief = notifier.render_brief(date, first, backups, changes, meta,
                                      ladder_next=ladder_cards,
                                      pending=pending,
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
        # 补发标记（见 _backfill 注释）：仅在手工补发时出现，正常触发零影响。
        _title = f"【补发】{date}" if _backfill() else date
        r = notifier.push(f"build_{task}", _title, brief, date=date, con=con,
                          force=_force_push())
        print(f"[build] push={r}")
        if r.get("sent") and task in ("pre", "auction", "close"):
            _record_confirms(con, date, task, picks)
    # ★ 用户需求（2026-09-21「盘前、竞价、盘中都可以对我的自选、购买股票
    # 提出操作建议」）：pre/auction 时点对持仓（去弱留强·换股建议）与自选
    # （可买/破位/急跌）各推一条可执行建议——只在有实质动作时推，且各 mode
    # 日熔丝一天一条，绝不刷屏。盘中已有 holding_intraday（force 卖出信号）。
    if task in ("pre", "auction"):
        _hold2 = load_holdings()
        if _hold2:
            try:
                from . import executor as _ex2
                _heval = _ex2.evaluate_real_holdings(con, date, _hold2)
                _cur = con.execute(
                    "SELECT code,name,action,buy_low,buy_high,stop,score "
                    "FROM rec_picks WHERE date=?", (date,)).fetchall()
                _cands = [{"code": x[0], "name": x[1], "action": x[2],
                           "buy_low": x[3], "buy_high": x[4], "stop": x[5],
                           "score": x[6]} for x in _cur]
                # 排除持仓股（换出福莱蒽特不应再推荐福莱蒽特）——
                # ⚠️ 必须在 for 循环**之前**过滤：循环体内 remove 会跳过
                # 下一个候选（迭代器已前进），连续两只持仓股会漏排一只。
                _held_codes = {h["code"] for h in _hold2}
                _cands = [c for c in _cands if c["code"] not in _held_codes]
                for _c in _cands:
                    _ind = con.execute(
                        "SELECT sector FROM stock_industry WHERE code=?",
                        (_c["code"],)).fetchone()
                    if _ind:
                        _c["sector"] = _ind[0]
                    _ex_row = con.execute(
                        "SELECT pool, extra FROM candidate_snapshots "
                        "WHERE code=? AND date=?", (_c["code"], date)).fetchone()
                    if _ex_row:
                        _c["pool"] = _ex_row[0]
                        try:
                            _xc = json.loads(_ex_row[1] or "{}")
                            _c["pos_label"] = _xc.get("pos_label")
                        except Exception:  # noqa: BLE001
                            pass
                # 主线/副线：当日板块榜前 1 = 主线，2~3 = 副线
                try:
                    from . import sector as _sec
                    _board = _sec.load_board(con, date)
                    _ranked = _sec.rank_board(_board, n=6)
                    for _i, _s in enumerate(_ranked):
                        for _c in _cands:
                            if _c.get("sector") == _s.get("sector"):
                                _c["mainline"] = ("主线" if _i == 0
                                                  else "副线")
                except Exception:  # noqa: BLE001
                    pass
                _hhtml = notifier.render_holding_advice(_heval, _cands, date)
                if any(x.get("exit_action") == "SELL" or x.get("swap_hint")
                       or x.get("phase") in ("已到期", "接近到期")
                       or x.get("sector_retreat") for x in _heval):
                    hr = notifier.push(
                        "holding_check",
                        f"持仓操作建议 {date[5:]}"
                        + ("（盘前）" if task == "pre" else "（竞价）"),
                        _hhtml, date=date, con=con, force=_force_push())
                    print(f"[build] holding push={hr}")
            except Exception as e:  # noqa: BLE001
                print(f"[build] pre/auction holding advice failed: {e}")
        try:
            from . import watchlist as _wl
            _watch2 = _codes_conf("WATCH_CODES", "watch.json")
            watch_advice = (_wl.build_watch_advice(
                con, date, _watch2, [h["code"] for h in _hold2])
                if _watch2 else [])
        except Exception as e:  # noqa: BLE001
            print(f"[build] pre/auction watch advice failed: {e}")
            watch_advice = []
        if watch_advice:
            _act_watch = [a for a in watch_advice if a.get("action") in
                          ("可买（回落至买区）", "已破位", "急跌", "已涨停")]
            if _act_watch:
                _wlines = ["# 自选股操作建议 " + date]
                for a in _act_watch:
                    ln = (f"- **{a.get('name', '')} {a['code']}**"
                          f"（{a['action']}）：{a['advice']}")
                    if a.get("dist_pct") is not None:
                        ln += f"｜距买区 {a['dist_pct']:+.1f}%"
                    _wlines.append(ln)
                wr = notifier.push("watch_advice", f"自选股建议 {date[5:]}",
                                   notifier.md2html("\n".join(_wlines)),
                                   date=date, con=con, force=_force_push())
                print(f"[build] watch push={wr}")
    # ★ 用户需求⑤：晚间原本分散的多条推送（AI叙事 / 模拟盘日结 / 真实持仓体检 /
    # 自选建议）合并为**一条**「晚间综合」在 review 时点发出，显著降低消息数量。
    # 仅在 review 时点汇总（close 时点只发主报告，避免重复）。
    if task == "review":
        from . import narrative, executor as _ex
        text = narrative.narrate({"date": date, "mood": mood or {},
                                  "emotion": emo, "picks": picks})
        narrative_html = notifier.md2html(text)
        try:
            rep = _ex.report_daily_pnl(con, date)
            daily_html = notifier.render_daily_summary(rep, date)
        except Exception as e:  # noqa: BLE001
            print(f"[build] daily_summary failed: {e}")
            daily_html = ""
        holding_html = ""
        try:
            _hold = load_holdings()
            if _hold:
                heval = _ex.evaluate_real_holdings(con, date, _hold)
                cur = con.execute(
                    "SELECT code,name,action,buy_low,buy_high,stop,score "
                    "FROM rec_picks WHERE date=?", (date,)).fetchall()
                cands = [{"code": r[0], "name": r[1], "action": r[2],
                         "buy_low": r[3], "buy_high": r[4], "stop": r[5],
                         "score": r[6]} for r in cur]
                _held_set = {h["code"] for h in _hold}
                cands = [c for c in cands if c["code"] not in _held_set]
                for c in cands:
                    ind = con.execute(
                        "SELECT sector FROM stock_industry WHERE code=?",
                        (c["code"],)).fetchone()
                    if ind:
                        c["sector"] = ind[0]
                holding_html = notifier.render_holding_advice(
                    heval, cands, date)
        except Exception as e:  # noqa: BLE001
            print(f"[build] holding_check failed: {e}")
        watch_html = ""
        if watch_advice:
            watch_html = notifier.md2html(
                "# 自选股操作建议 " + date + "\n" + "\n".join(
                    f"- **{a.get('name','')} {a['code']}**（{a['action']}）：{a['advice']}"
                    + (f"｜距买区 {a['dist_pct']:+.1f}%" if a.get("dist_pct") is not None else "")
                    for a in watch_advice))
        digest = notifier.render_evening_digest(
            date, narrative_html, daily_html, holding_html, watch_html)
        if digest:
            r = notifier.push("review", date, digest, date=date, con=con,
                              force=_force_push())
            print(f"[build] evening_digest push={r}")
        else:
            print("[build] evening_digest empty → 跳过")
    return {"date": date, "candidates": len(cands), "picks": picks,
            "ladder_next": ladder_next, "emotion": emo, "changes": changes}


def _sector_fields(c):
    """卡片渲染用的板块字段（2026-09-18）。渲染层只透传、不重判。"""
    return {"sector": c.get("sector"), "sector_pct": c.get("sector_pct"),
            "sector_temp": c.get("sector_temp"),
            "sector_net_yi": c.get("sector_net_yi")}


def _wait_days_map(con, date, window=5):
    """最近 window 个交易日里，各代码出现在推荐位（未兑现）的天数。

    「兑现」口径：outcome 仍为 ''（T+2 结局未回填或未成交）且 action 属
    于可执行动作——只统计**仍在榜**的票：最近一个交易日必须也出现，
    否则是早已离场的历史推荐，不算躺榜。"""
    days = trade_calendar(con)
    if date not in days:
        return {}
    idx = days.index(date)
    if idx == 0:
        return {}
    prev_days = days[max(0, idx - window):idx]
    if not prev_days:
        return {}
    ph = ",".join("?" * len(prev_days))
    rows = con.execute(
        f"SELECT code, date, outcome FROM rec_picks "
        f"WHERE date IN ({ph}) AND action IN ('现在买','等回踩','小仓试')",
        prev_days).fetchall()
    by_code = {}
    for code, d, outcome in rows:
        if outcome:
            continue                    # 已兑现/已回填结局的不算躺榜
        by_code.setdefault(code, set()).add(d)
    latest = prev_days[-1]
    return {code: len(ds) for code, ds in by_code.items() if latest in ds}


def today_verdict(emo, mood):
    """今日仓位裁决（用户 2026-09-22：「告诉我今天能不能开仓、或者离场」）。

    基于十维情绪（达标才有效）+ 炸板率，输出 (级别, 说明)：
      可开仓 / 轻仓试探 / 观望为主 / 离场为主（持仓反弹减）。
    情绪数据覆盖不足 → 「谨慎」——不装懂。"""
    if not emo or not emo.get("qualified"):
        return ("谨慎", "情绪数据覆盖不足，轻仓试探或观望，重仓需等数据达标")
    s = float(emo.get("score"))
    zb = (mood or {}).get("zhaban_rate")
    zb_txt = f"，炸板率 {zb*100:.0f}%" if zb is not None else ""
    if s >= 60 and not (zb is not None and zb >= 0.40):
        return ("可开仓", f"情绪 {s:.0f} 分偏热{zb_txt}，按计划执行，"
                          "止盈止损照旧")
    if s >= 45:
        if zb is not None and zb >= 0.40:
            return ("观望为主", f"情绪 {s:.0f} 分但炸板率 {zb*100:.0f}% 偏高，"
                                "不开新仓，持仓反弹减仓")
        return ("轻仓试探", f"情绪 {s:.0f} 分中性{zb_txt}，只买进买区的，"
                            "不追高")
    return ("离场为主", f"情绪 {s:.0f} 分偏冷{zb_txt}，不开新仓，"
                        "持仓反弹减仓、破位离场")


def _record_confirms(con, date, task, picks):
    """登记该时点推送过的候选，供收盘打「双确认/三确认」标签。"""
    for c in picks:
        con.execute("INSERT OR REPLACE INTO confirm_log VALUES(?,?,?)",
                    (date, task, c["code"]))
    con.commit()


def _confirm_counts(con, date, window=10):
    """近 window 个自然日内，每只股票被推送推荐的次数（跨天累计）。

    用户模型：收盘首推 = 第 1 次；次日盘前仍在列 = 第 2 次（双确认）；
    竞价后仍在列 = 第 3 次（三确认）。次数 = confirm_log 里该 code 出现的
    (date, task) 对数（去重），近 window 天内的。"""
    cutoff = (datetime.fromisoformat(date)
              - __import__("datetime").timedelta(days=window)).isoformat()
    rows = con.execute(
        "SELECT code, COUNT(DISTINCT date || task) FROM confirm_log "
        "WHERE date>=? AND date<=? GROUP BY code",
        (cutoff, date)).fetchall()
    return {code: n for code, n in rows}


def _force_push():
    """ASTOCK_FORCE_PUSH=1 时绕过当日去重强制重发（用户明确要求重发时用）。

    平时恒为 False——去重是防打扰的核心，不能默认关闭。"""
    return os.environ.get("ASTOCK_FORCE_PUSH") == "1"


def _backfill():
    """ASTOCK_BACKFILL=1 时给标题打【补发】前缀（2026-09-16 新增）。

    为什么需要：补发消息与当日正常触发的消息**内容不同源**（补发跑在
    收盘后/次日，数据口径与时点都可能变化）。不标注的话，用户无法区分
    「刚发的」和「补昨天的」，反而制造新的困惑——而本次整改的初衷正是
    消除困惑。恒为 False 时零影响。"""
    return os.environ.get("ASTOCK_BACKFILL") == "1"


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


def _snapshot_date_for(con, date):
    """筛选口径的快照基准日 = 最近一个**有真实成交额**的交易日（≤ date）。

    ⚠️ 2026-09-16（血案：盘前/竞价的量能口径不成立）：当日快照在盘前
    （08:50）成交额全 0、在竞价（09:25）只有撮合额，两者都不能作为
    「停牌判定 / 1.2 亿流动性门槛」的依据。统一回退到最近一个已收盘、
    有量的交易日，站点补算口径才能与推送口径一致（否则页面显示
    覆盖率 0%，用户以为全市场没扫到）。"""
    row = con.execute(
        "SELECT MAX(date) FROM snapshot WHERE date<=? AND amt>0",
        (date,)).fetchone()
    return row[0] if row and row[0] else date


def coverage_snapshot(con, date):
    """覆盖快照：主流程用 scan_all 写入的结果；站点单独构建时用聚合查询补算。

    历史坑：站点 *_task site* 与推送是两条独立入口，站点若不补算就会读到
    空的 LAST_SCAN_COVERAGE → 页面覆盖率显示「—」，与推送口径不一致。
    ⚠️ 2026-09-16：补算也必须用**有效快照基准日**（见 _snapshot_date_for），
    否则盘前构建站点会算出覆盖率 0%，与推送口径打架。
    """
    cov = dict(LAST_SCAN_COVERAGE)
    if cov.get("date") == date:
        return cov
    snap_date = _snapshot_date_for(con, date)
    snap = _snapshot(con, snap_date)
    alive, dead = split_universe(con, date, snap, asof=snap_date)
    have = {r[0] for r in con.execute(
        "SELECT DISTINCT code FROM klines WHERE date=?",
        (snap_date,)).fetchall()}
    with_bar = sum(1 for c in alive if c in have)
    return {"date": date, "snap_date": snap_date,
            "universe": len(alive), "untradable": len(dead),
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
    # 持仓明细（buy 角色专属，含成本/浮盈——apply_roles 会按角色剥离）
    try:
        holdings_detail = _build_holdings_detail(con, date)
    except Exception as e:  # noqa: BLE001
        print(f"[site] holdings detail failed: {e}")
        holdings_detail = []
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
            "holdings_detail": holdings_detail,
            "signals": sigs,
            "changes": changes,
            "triggers": trigs,
            "recperf": rp,
            "skipped": muted + rejected[:SITE_SKIP_CAP]}


def _build_holdings_detail(con, date):
    """持仓明细 + 浮动盈亏（buy 角色专属数据）。

    浮盈口径：最新收盘 vs 买入价，不含费用/滑点（与 meta.disclosure 一致）。"""
    out = []
    for h in load_holdings():
        code = h.get("code")
        if not code:
            continue
        row = con.execute(
            "SELECT c FROM klines WHERE code=? AND date=?", (code, date)
        ).fetchone()
        close = row[0] if row else None
        bp = h.get("buy_price")
        pnl_pct = (round((close / bp - 1) * 100, 2)
                   if close and bp else None)
        out.append({"code": code, "name": h.get("name", ""),
                    "buy_date": h.get("buy_date"), "buy_price": bp,
                    "shares": h.get("shares"), "stop": h.get("stop"),
                    "close": close, "pnl_pct": pnl_pct})
    return out


def build_site(date=None):
    con = get_conn()
    date = date or today_str()
    data = build_data_for_site(con, date)
    from . import users as users_mod
    users = users_mod.load_users(os.path.join(core.CONFIG_DIR, "users.json"))
    if not users:
        raise SystemExit("config/users.json 为空或格式错误：请先设置口令（含 owner）")
    passwords = users_mod.passwords_of(users)
    publish.build_site(data, passwords, users=users)
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


def _build_period(con, date, days=30):
    """★ 用户需求①：半月/月度周期复盘（独立于选股主链）。

    聚合 accounts/历史成交/当前持仓，产出「盈利最大化 + 系统改进建议」，
    作为一条【周期】推送发出（默认每月 1 日、16 日触发，分别传 days=30/15）。"""
    from . import executor as _ex
    from . import notifier
    rep = _ex.report_period(con, date, days)
    html = notifier.render_period_report(rep, date, days)
    title = f"{days}天周期复盘"
    r = notifier.push("period", title, html, date=date, con=con,
                      force=_force_push())
    print(f"[build] period push={r}")
    return {"period": True, "days": days, "net": rep.get("net"),
            "win_rate": rep.get("win_rate")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="close",
                    choices=["pre", "auction", "close", "review", "site",
                             "intraday"])
    ap.add_argument("--date", default=None)
    # M41 盘中任务：--slot am|pm 决定早盘校验/尾盘机会；live=高频买点巡检
    # （每 10 分钟一轮，事件级去重，见 intraday.py 模块注释）；--dry 只算不推
    ap.add_argument("--slot", default="pm", choices=["am", "pm", "live"])
    # 用户需求①：半月/月度周期复盘窗口（天）；调度器在 1 日传 30、16 日传 15
    ap.add_argument("--period-days", type=int, default=30)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()   # argv 隔离：内嵌任务用 parse_known_args 的精神
    if a.task == "site":
        build_site(a.date)
    elif a.task == "intraday":
        # 盘中走**独立模块**，不进 build() —— 盘中用的是实时快照，
        # 与收盘主链（日K口径）必须物理隔离，避免互相污染。
        from . import intraday
        intraday.run(slot=a.slot, date=a.date, dry=a.dry)
    elif a.task == "period":
        build(a.task, a.date, period_days=a.period_days)
    else:
        build(a.task, a.date)


if __name__ == "__main__":
    main()
