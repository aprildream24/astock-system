# -*- coding: utf-8 -*-
"""盘中计划校验（M41）：不重新选股，只用实时价校验已发计划 + 持仓风控。

为什么盘中**不能**重新选股
------------------------
系统全部引擎（趋势/箱体波段/快箱体/连板空间/回马枪/近端买点）都以
**已收盘日K**为输入，算买区、止损与形态。盘中拿到的「当日K线」是
**半根未完成**数据（价格还在动），用它重跑引擎 = 用不该用的数据做判断，
与 2026-09-16「盘前候选 0」血案同源（那次是用了盘前还没发生的成交额）。
所以盘中只做**数据支持**的两件事：

  ① 已发计划的实时校验：现价还在买区里吗？已经涨飞还是跌破？
  ② 持仓风控：实时是否触及止损。

安全红线（不可回退）
------------------
本模块**只读** `fetch_daily.fetch_universe()`（纯 HTTP 分页，无副作用），
**绝不**调用 `fetch_daily.fetch_daily()`，**绝不**写 `klines` / `snapshot`
主表。盘中价不是收盘价，写进主表会污染历史库，让次日全部引擎基于假收盘价
出信号。实时价只落**独立表** `snapshot_live`，需人工显式查询，
不进任何引擎计算链路。

打扰纪律（用户口径：没有机会就不凑数）
------------------------------------
只在**有实质内容**时推送，否则静默留痕：
  · 持仓票实时触及止损 → 必推（最高优先）
  · pm（尾盘）时点出现「现价进入买区」的票 → 推（唯一"看到还能当天操作"的窗口）
  · am（早盘）时点盘前计划 ≥50% 跌破买区下沿 → 推计划转差警示
  · 其余情况 → 写库 + 日志，**不发消息**
am/pm 各自日熔丝一天一条；live（见下）改用**事件级**账本去重。

live 高频巡检（用户 2026-09-24「为什么又要等到看盘？我要尽可能快速地
告诉我可以买入的股票」）
------------------
astock-intraday-live 定时器每 10 分钟触发一次（09:30-11:35 / 13:00-15:00）。
一天最多 ~22 轮，靠 am/pm 那套「日熔丝一天一条」去重会把后续所有新事件
全部吞掉，所以 live 的推送单位是**事件**而不是**轮次**：

  · 同票同事件同一天只报一次（live_alerts 账本：(date, kind, code) 主键）
  · 首次进买区 / 首次触发止损 / 首次出现卖出信号 → **即刻 force 推送**
  · 已报过的票不再重复（哪怕仍停在买区里）；后续轮次全部静默
  · 事件的"发生"由轮询判定，最坏延迟 = 一个触发间隔（≤10 分钟）

am/pm 的摘要推送语义不变（日熔丝一天一条）；sell/止损类在**所有** slot
都过事件账本——同一持仓 09:45 报过卖出信号，14:40 不再重复报同一只。

时点选择依据
-----------
pm 定在 **14:40**：当日成交额此时已≈定局（量价基本成形），且是 T+1 制度下
唯一「今天买、明天可卖」的短持仓窗口——盘中真正可执行的机会只在尾盘。
am 定在 **09:45**：开盘 15 分钟即可识别「高开低走/低开走强」，
但 15 分钟数据噪声大 ⇒ 只做**转差警示**，默认静默。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
import urllib.request

# 盘中时段（北京时间，含端点、放宽缓冲）
_AM_WINDOW = (9 * 60 + 30, 11 * 60 + 35)
_PM_WINDOW = (13 * 60, 15 * 60)
# live 高频巡检覆盖整个连续交易时段（am+pm 两窗）
_LIVE_WINDOWS = (_AM_WINDOW, _PM_WINDOW)
# 事件账本的 kind 取值（live_alerts.kind）
KIND_ZONE = "zone"        # 推荐计划现价进入买区
KIND_WZONE = "wzone"      # 自选进入买区
KIND_STOP = "stop"        # 持仓触及手填止损
KIND_SELL = "sell"        # 持仓系统判定卖出
KIND_WSTOP = "wstop"      # 自选跌破止损
KIND_BO = "bo"            # 双轨买区：突破确认价越过（强而不板）
# 抓取异常的兜底：正常盘中应有 4500+ 只快照，低于此值说明源异常
_MIN_UNIVERSE = 500
# 09-30 用户口径：live/盘中推送里「进入买区（可当日下单）」组只放**当下
# 真能照价下单**的计划。连板通道（次日竞价达标买）的买区是竞价条件带、
# 观望/禁买根本不是买入计划——它们混进"可下单"组就是逻辑漏洞
# （用户实测：「收到信息现在进入买区当日可下单，结果详情又是观望候选」）。
# 这些统一并入 20:02 复盘总结。小仓试 = 可买（建议仓位行已注明小仓）。
_LIVE_BUYABLE = ("现在买", "等回踩", "小仓试")


def _bj_now():
    """北京时间。CI runner 时区是 UTC，**不能**用 datetime.now() 直接判断盘中。"""
    return _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=8)))


def bare(code):
    """去掉 sh/sz 前缀 → 六位裸码（快照接口的 key 空间）。"""
    return code[2:] if code[:2] in ("sh", "sz") else code


def prefixed(code):
    """裸码 → 带前缀。与 klines/snapshot 主表的 key 空间一致。"""
    if code[:2] in ("sh", "sz"):
        return code
    return ("sh" if code.startswith("6") else "sz") + code


def _purge_old(con, date):
    """保留期清理（2026-09-27）：live 每 10 分钟写一版全市场快照（~4500
    行/轮、22 轮/交易日），不清理则 market.db 每天膨胀 ~10 万行——
    GH cache 10GB 仓库上限会被无声吃穿，然后所有任务的缓存互相驱逐。
    snapshot_live 只留今昨两天（盘中 _last_price 只查当天；昨天留作排查），
    live_alerts 留 7 天（事件回溯用）。清理失败不阻断巡检。"""
    try:
        cur = con.execute(
            "DELETE FROM snapshot_live WHERE date < date(?, '-1 day') "
            "OR date > ?", (date, date))
        a = cur.rowcount
        cur = con.execute(
            "DELETE FROM live_alerts WHERE date < date(?, '-6 day')",
            (date,))
        b = cur.rowcount
        con.commit()
        if a or b:
            print(f"[intraday] 保留期清理：snapshot_live -{a} 行，"
                  f"live_alerts -{b} 行")
    except Exception as e:                      # noqa: BLE001
        print(f"[intraday] 保留期清理失败（不阻断）: {e}")


def _enrich(con, date, items):
    """给买区票补两行详情所需的信息：板块/板块热度/20日位置/决断力/确认数。
    逐项 best-effort：拿不到就不标注，绝不阻断推送。"""
    for it in items:
        try:
            ind = con.execute(
                "SELECT sector FROM stock_industry WHERE code=?",
                (it["code"],)).fetchone()
            it["sector"] = ind[0] if ind else ""
            if it["sector"]:
                heat = con.execute(
                    "SELECT pct FROM sector_heat WHERE date=? AND sector=?",
                    (date, it["sector"])).fetchone()
                it["sector_pct"] = heat[0] if heat else None
            ex = con.execute(
                "SELECT extra FROM ("
                "  SELECT extra, action, score, ROW_NUMBER() OVER ("
                "    PARTITION BY code ORDER BY date DESC,"
                "      CASE WHEN action IN ('现在买','等回踩','小仓试') THEN 0"
                "           WHEN action = '次日竞价达标买' THEN 1 ELSE 2 END,"
                "      COALESCE(score,0) DESC, rowid DESC) AS rn"
                "  FROM candidate_snapshots"
                "  WHERE code=? AND date<=?"
                "    AND json_extract(extra,'$.buy_low') IS NOT NULL"
                "  ) WHERE rn=1",
                (it["code"], date)).fetchone()
            if ex and ex[0]:
                x = json.loads(ex[0])
                it["pos_label"] = x.get("pos_label")
                dec = x.get("decisive") or {}
                it["dec_net"] = dec.get("net")
                if dec.get("net") is not None:
                    it["decisive"] = {"net": dec.get("net"),
                                      "eff": dec.get("eff") or 0}
            cc = con.execute(
                "SELECT COUNT(DISTINCT date || task) FROM confirm_log "
                "WHERE code=? AND date>=date(?, '-10 day')",
                (it["code"], date)).fetchone()
            it["confirms"] = cc[0] if cc else 0
        except Exception:                           # noqa: BLE001
            continue


def _live_stock_block(p):
    """两行一票（09-30 用户：「细分为属于什么板块，现在强度如何等等，
    一个股票用两行展示」）：第一行 名称/现价/买区，第二行 板块热度/位置/
    强度/确认。"""
    price = f'{p["price"]:.2f}' if p.get("price") else "—"
    pct = f'{p["pct"]:+.1f}%' if p.get("pct") is not None else ""
    zone = (f'{p["lo"]:.2f}~{p["hi"]:.2f}'
            if p.get("lo") and p.get("hi") else "—")
    sector = p.get("sector") or ""
    heat = ""
    if sector and p.get("sector_pct") is not None:
        heat = (f' {"🔥" if p["sector_pct"] >= 0 else "❄️"}'
                f'{p["sector_pct"]:+.1f}%')
    conf = {3: "✅三确认", 2: "●双确认"}.get(p.get("confirms") or 0, "")
    l1 = ('<div style="font-size:14.5px;font-weight:700;color:#e8eaed">'
          f'✅可买 {p.get("name") or ""}'
          f' <span style="color:#8a93a3;font-size:12px">{p["code"]}</span>'
          f' <span style="color:#3fae6b">现价{price} {pct}</span>'
          f' <span style="color:#8a93a3;font-weight:400">买区 {zone}</span>'
          '</div>')
    l2parts = [f'板块 {sector or "—"}{heat}',
               p.get("pos_label") or "",
               (f'20日净移{p["dec_net"]:+.1f}%'
                if p.get("dec_net") is not None else ""),
               conf]
    l2 = ('<div style="font-size:12px;color:#9aa0a6;margin:1px 0 8px">'
          + " · ".join(x for x in l2parts if x) + "</div>")
    return ('<div style="border-bottom:1px dashed #2b313d;padding:4px 0">'
            f"{l1}{l2}</div>")


def _cap_rows(rows, hint, cap=15):
    """live 首轮可能一次性出现几十上百只进买区（账本全新，全是"新事件"），
    推送不是阅读器——截前 cap 行 + 总数提示，全量走网页版。
    标记仍按**全量**记（用户已通过总数被告知，不重复轰炸）。"""
    if len(rows) <= cap:
        return rows, hint
    return rows[:cap], (hint + f"（仅列前 {cap} 只 / 共 {len(rows)} 只，"
                        "全量见网页版）")


# ---------------------------------------------------------------------------
# 盘中定向批量报价（2026-09-29 手术）
# 为什么不再全市场翻页：live 每 10 分钟一轮，全市场 = 50+ 页/轮 × 22 轮/天
# ≈ 1100 请求/天——09-29 实测把 EM 打到降级（快照 pct 全零）、新浪限流，
# 连 15:22 收盘主链都被殃及 data_blocked（自伤式限流）。监控对象只有
# 几百只，批量接口 60 码/请求、7 个请求就够，请求量降 ~99%。
# ---------------------------------------------------------------------------
_BATCH = 60          # 每请求代码数（腾讯/新浪批量接口的安全上限）
_QCOV = 0.9          # 覆盖率闸：要到的报价 < 90% 视为源异常


from .core import fetch_text  # noqa: E402


def _quotes_tx(codes):
    """腾讯批量：qt.gtimg.cn/q=sh600000,sz000001,...（GBK）。
    f[1]=名称 f[3]=现价 f[4]=昨收 → pct 现算（比信任字段更稳）。
    ⚠️ 必须自管 GBK 解码——fetch_text 按 UTF-8 解会让名称变乱码。"""
    out = {}
    for i in range(0, len(codes), _BATCH):
        batch = [prefixed(c) for c in codes[i:i + _BATCH]]
        s = urllib.request.urlopen(
            urllib.request.Request(
                f"https://qt.gtimg.cn/q={','.join(batch)}",
                headers={"User-Agent": "Mozilla/5.0"}),
            timeout=10).read().decode("gbk", "replace")
        for chunk in s.split(";"):
            if '"' not in chunk:
                continue
            var, inner = chunk.split("=", 1)
            var = var.strip()
            p = inner.strip().strip('"').split("~")
            # 变量形态 v_sh600000：[2:4]=市场前缀，[4:]=六位裸码
            if not var.startswith("v_") or len(var) < 10:
                continue
            num = var[4:]
            try:
                price, prev = float(p[3]), float(p[4])
            except ValueError:
                continue
            if price <= 0 or prev <= 0:
                continue
            out[num] = {"name": p[1], "price": price,
                        "pct": round((price / prev - 1) * 100, 2),
                        "amt": None}
        time.sleep(0.3)
    return out


def _quotes_sina(codes):
    """新浪批量：hq.sinajs.cn/list=...（GBK，需 Referer）。
    f[0]=名称 f[2]=昨收 f[3]=现价 → pct 现算。"""
    out = {}
    for i in range(0, len(codes), _BATCH):
        batch = [prefixed(c) for c in codes[i:i + _BATCH]]
        s = urllib.request.urlopen(
            urllib.request.Request(
                f"https://hq.sinajs.cn/list={','.join(batch)}",
                headers={"User-Agent": "Mozilla/5.0",
                         "Referer": "https://finance.sina.com.cn"}),
            timeout=10).read().decode("gbk", "replace")
        for line in s.splitlines():
            if '"' not in line or "=" not in line:
                continue
            var, inner = line.split("=", 1)
            p = inner.strip().strip('"').split(",")
            if len(p) < 4:
                continue
            num = var.strip().replace("var hq_str_", "")
            try:
                price, prev = float(p[3]), float(p[2])
            except ValueError:
                continue
            if price <= 0 or prev <= 0:
                continue
            out[num] = {"name": p[0], "price": price,
                        "pct": round((price / prev - 1) * 100, 2),
                        "amt": None}
        time.sleep(0.3)
    return out


def fetch_quotes(codes):
    """监控对象的批量实时价。→ ({裸码: {name,price,pct,amt}}, 来源)。
    腾讯主源；覆盖率 <90% 视为该源异常，换新浪补齐（合并，新浪只补缺）。"""
    codes = [bare(c) for c in dict.fromkeys(codes) if c]
    if not codes:
        return {}, "none"
    out = {}
    srcs = []
    for fn in (_quotes_tx, _quotes_sina):
        try:
            got = fn(codes)
        except Exception as e:                      # noqa: BLE001
            print(f"[intraday] 批量报价 {fn.__name__} 失败: "
                  f"{type(e).__name__} {e}")
            continue
        cov = len([c for c in codes if c in got]) / max(len(codes), 1)
        srcs.append(fn.__name__)
        for c, v in got.items():
            out.setdefault(c, v)                    # 首源优先，次源只补缺
        if cov >= _QCOV:
            break
        print(f"[intraday] {fn.__name__} 覆盖率 {cov:.0%} "
              f"({len([c for c in codes if c in got])}/{len(codes)}) → 补下一源")
    return out, "+".join(srcs) or "none"


def in_window(slot, now):
    """时段守门：防误触发（定时器故障 / 手工 dispatch 到非盘中）。"""
    t = now.hour * 60 + now.minute
    if slot == "live":
        return any(lo <= t <= hi for lo, hi in _LIVE_WINDOWS)
    lo, hi = _AM_WINDOW if slot == "am" else _PM_WINDOW
    return lo <= t <= hi


# ---------------------------------------------------------------------------
# 事件级告警账本（live_alerts）：live 巡检的"同票同事件当天只报一次"
# ---------------------------------------------------------------------------
def _ensure_ledger(con):
    """防御性建表：CI 缓存里的库可能还是旧 schema（无 live_alerts）。"""
    con.execute(
        "CREATE TABLE IF NOT EXISTS live_alerts("
        "date TEXT, kind TEXT, code TEXT, ts TEXT, detail TEXT,"
        "PRIMARY KEY(date, kind, code))")
    con.commit()


def _alerted_set(con, date):
    """当天已告警过的 (kind, code) 集合。"""
    return {(k, c) for k, c in con.execute(
        "SELECT kind, code FROM live_alerts WHERE date=?", (date,)).fetchall()}


def _entry_count(con, date, code, kind=KIND_ZONE):
    """同票同事件当日的已提醒次数（detail 首段 hits=N）。"""
    row = con.execute(
        "SELECT detail FROM live_alerts WHERE date=? AND kind=? AND code=?",
        (date, kind, code)).fetchone()
    if not row:
        return 0
    try:
        return int(str(row[0]).split("|", 1)[0].replace("hits=", "") or 0)
    except Exception:                               # noqa: BLE001
        return 1


def _mark_alerted(con, date, kind, items, ts):
    """推送成功后记账（detail 首段记 hits=N 供当日次数上限判断）。"""
    for x in items:
        n = _entry_count(con, date, x["code"], kind) + 1
        con.execute("INSERT OR REPLACE INTO live_alerts VALUES(?,?,?,?,?)",
                    (date, kind, x["code"], ts,
                     f"hits={n}|{x.get('name', '')}@{x.get('price', '')}"))
    con.commit()


def _fresh(kind, items, alerted):
    """过滤掉当天已报过的事件。"""
    return [x for x in items if (kind, x["code"]) not in alerted]


def classify(price, pct, lo, hi, stop):
    """把「实时价 vs 计划买区」判成一个状态。返回 (state, label)。

    只做**价格与区间的比较**，不引用任何引擎阈值——盘中不做形态判断
    （形态判断需要收盘K线，那是收盘构建的职责）。
    """
    if price is None or price <= 0:
        return "no_data", "无报价"
    if stop and price <= stop:
        return "broke_stop", "已破止损"
    if pct is not None and pct >= 9.8:
        return "limit_up", "涨停封板"
    if lo and hi and lo <= price <= hi:
        return "in_zone", "在买区内"
    if lo and price > hi:
        return "above", "已涨出买区"
    if lo and price < lo:
        return "below", "已跌破买区"
    return "plain", "—"


# ---------------------------------------------------------------------------
# 渲染（深色主题；webview 对 flex 支持差 → 一律 table）
# ---------------------------------------------------------------------------
_BG, _CARD, _BD = "#15181e", "#1d222b", "#2b313d"
_TXT, _MUT = "#e6e9ef", "#9aa4b2"
_UP, _DN, _HL = "#ff6b5e", "#4ecf8e", "#6ab0ff"

_STATE_COLOR = {"in_zone": _HL, "above": _MUT, "below": _DN,
                "broke_stop": _UP, "limit_up": _UP, "no_data": _MUT,
                "plain": _MUT}


def _row(cells, colors=None):
    colors = colors or [_TXT] * len(cells)
    tds = "".join(
        f'<td style="padding:4px 6px;border-bottom:1px solid {_BD};'
        f'color:{c};font-size:12px;white-space:nowrap">{v}</td>'
        for v, c in zip(cells, colors))
    return f"<tr>{tds}</tr>"


def _section(title, rows, hint=""):
    if not rows:
        return ""
    h = (f'<div style="margin:10px 0 4px;color:{_HL};font-size:13px;'
         f'font-weight:600">{title}</div>')
    if hint:
        h += (f'<div style="color:{_MUT};font-size:11px;margin-bottom:4px">'
              f'{hint}</div>')
    return (h + f'<table cellspacing="0" cellpadding="0" '
            f'style="width:100%;border-collapse:collapse">{rows}</table>')


def render_html(date, slot, now, groups, plan_n, coverage_note=""):
    """groups: [{"title","hint","rows":[(cells, colors)]}, ...]
    或 {"title","hint","html"}（一票一卡等整块 HTML，不包 table）。"""
    from .notifier import render_card  # 买点一票一卡（09-30）
    head = {"am": "早盘校验", "pm": "尾盘机会"}.get(slot, "买点巡检")
    body = ""
    for g in groups:
        if "html" in g:
            # html 组（一票一卡等）直接输出，不塞 _section 的 table——
            # 嵌套 div/table 是 webview 错乱（"全部都是乱的"）的根因
            _hint = (f'<div style="color:#9aa0a6;font-size:11px;'
                     f'margin:0 0 6px">{g["hint"]}</div>' if g.get("hint")
                     else "")
            body += (f'<div style="margin:10px 0 4px;color:#6ab0f2;'
                     f'font-size:13px;font-weight:600">{g["title"]}</div>'
                     + _hint + g["html"])
        else:
            body += _section(g["title"],
                             "".join(_row(c, col) for c, col in g["rows"]),
                             g.get("hint", ""))
    if not body:
        body = (f'<div style="color:{_MUT};font-size:12px">'
                f'本时点无实质变化（静默，不占额度）</div>')
    return (
        f'<div style="background:{_BG};padding:12px;font-family:'
        f'-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif">'
        f'<div style="color:{_TXT};font-size:15px;font-weight:700;'
        f'margin-bottom:2px">盘中{head} · {date}</div>'
        f'<div style="color:{_MUT};font-size:11px;margin-bottom:8px">'
        f'数据时点 {now:%H:%M}（北京时间）· 计划 {plan_n} 只'
        f'{("· " + coverage_note) if coverage_note else ""}</div>'
        f'<div style="background:{_CARD};border:1px solid {_BD};'
        f'border-radius:6px;padding:10px">{body}</div>'
        f'<div style="color:{_MUT};font-size:11px;margin-top:8px">'
        f'盘中只校验已发计划与持仓风控，不用实时价重新选股'
        f'（引擎口径基于已收盘日K）。</div></div>')


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _relevance_tier(con, code, watch_set, held_secs):
    """盘中提醒相关度（2026-10-05 用户需求③）：0=自选（你明确关注的），
    1=与持仓同板块（联动/换股视角），2=其余。"""
    if code in watch_set:
        return 0
    # stock_industry.code 是裸 6 位码，rec_picks/plan 是带前缀形态
    bare = code[2:] if code[:2] in ("sh", "sz") else code
    row = con.execute(
        "SELECT sector FROM stock_industry WHERE code=?", (bare,)).fetchone()
    return 1 if (row and row[0] and row[0] in held_secs) else 2


def relevance_sort(con, plans, watch_set, held_secs):
    """提醒顺序 = 相关度 → 有效分 → 代码。只排序不筛选：
    到价照推（告知不缺席），只是与你最相关的排最前。"""
    def _key(p):
        score = p[6] if len(p) > 6 else 0
        return (_relevance_tier(con, p[0], watch_set, held_secs),
                -(score or 0), p[0])
    plans.sort(key=_key)
    return plans


def run(slot="pm", date=None, con=None, dry=False, now=None,
        force_window=False):
    """跑一次盘中校验。返回结果字典（供测试与日志断言）。

    dry=True 时只计算不推送（本地验证用）；force_window=True 跳过时段守门
    （手工补发用）。
    """
    from . import core, trade_calendar as holiday_cal
    from .notifier import render_card  # 买点一票一卡（09-30）
    now = now or _bj_now()
    date = date or now.strftime("%Y-%m-%d")
    con = con or core.get_conn()
    out = {"slot": slot, "date": date, "pushed": False, "reason": "",
           "universe": 0, "in_zone": 0, "broken": 0, "stops": 0}

    if not holiday_cal.is_trade_day(date):
        out["reason"] = "非交易日"
        print(f"[intraday] {date} 非交易日 → 跳过")
        return out
    if not force_window and not in_window(slot, now):
        out["reason"] = f"非{slot}时段（现在 {now:%H:%M}）"
        print(f"[intraday] {out['reason']} → 跳过（防误触发）")
        return out
    _purge_old(con, date)

    # 计划 = 当日构建写入的推荐（pre/auction/close 都会写 rec_picks）
    # 2026-10-05 显式排序（有效分 desc → code）：不再依赖写入序，
    # 同分票在盘中推送里的先后也永不互换。
    plans = con.execute(
        "SELECT code, name, action, buy_low, buy_high, stop, score "
        "FROM rec_picks WHERE date=? ORDER BY score DESC, code",
        (date,)).fetchall()
    # ★ 历史候选并入（用户 2026-09-25「到达买点的票随时推，不要永远只是
    # 那几只」）：近 5 个交易日出现过的全部候选（含当日未入选的）都纳入
    # 到买点监控；同票以当日推荐优先，历史候选标注 src=hist。
    try:
        _seen = {p[0] for p in plans}
        # 2026-09-26 两处修复：
        # ① 原 SELECT 第二列写成未定义的 `n`，NameError 被 except 吞掉
        #    ⇒ 历史候选从未真正并入过（修复后近一周 ~1000 只带买区）；
        # ② 原 MAX(buy_low)/MAX(buy_high)/MAX(stop) 按**列独立**聚合，
        #    同票多池时会拼出"A 方案下沿 + B 方案上沿"的不存在买区 →
        #    改为每票选一条规范方案（最新日期 → 可执行动作优先 → 高分）。
        _hist = con.execute(
            "SELECT code, name, action, buy_low, buy_high, stop, score FROM ("
            "  SELECT code, name, action, score,"
            "         json_extract(extra,'$.buy_low')  AS buy_low,"
            "         json_extract(extra,'$.buy_high') AS buy_high,"
            "         json_extract(extra,'$.stop')     AS stop,"
            "         ROW_NUMBER() OVER ("
            "           PARTITION BY code"
            "           ORDER BY date DESC,"
            "             CASE WHEN action IN ('现在买','等回踩','小仓试',"
            "                                  '次日竞价达标买')"
            "                  THEN 0 ELSE 1 END,"
            "             COALESCE(score,0) DESC) AS rn"
            "  FROM candidate_snapshots"
            "  WHERE date>=date(?, '-6 day') AND date<?"
            "    AND json_extract(extra,'$.buy_low') IS NOT NULL"
            "    AND json_extract(extra,'$.buy_high') IS NOT NULL"
            ") WHERE rn=1", (date, date)).fetchall()
        _extra_plans = [(c, nm, (a or "等回踩") + "·候选", lo, hi, st, sc)
                        for c, nm, a, lo, hi, st, sc in _hist
                        if c not in _seen and lo and hi]
        plans = list(plans) + _extra_plans
    except Exception as e:  # noqa: BLE001 — 历史候选缺失不影响当日计划
        print(f"[intraday] 历史候选并入失败（不影响当日计划）: {e}")
    held = {}
    try:
        from .build import load_holdings        # 函数内延迟导入，避免循环
        for h in load_holdings():
            if h.get("code"):
                held[prefixed(h["code"])] = h
    except Exception as e:                       # noqa: BLE001
        print(f"[intraday] 持仓配置读取失败（不阻断）：{e}")

    # ---- 自选码提前装载（给定向报价并集用；下方自选检查复用同一份）----
    _watch_codes = []
    try:
        from .build import _codes_conf
        _watch_codes = _codes_conf("WATCH_CODES", "watch.json")
    except Exception as e:                       # noqa: BLE001
        print(f"[intraday] 自选配置读取失败（不阻断）：{e}")

    # ---- 2026-09-29 手术：定向批量报价替代全市场翻页 ----
    # ---- 2026-10-05 相关度排序（用户需求③）：先按「与你的关系」重排
    # 计划序——自选股最先，其次与持仓同板块的票，其余按分。到价照推
    # （告知不缺席），只是提醒列表里最相关的排最前、标题点名前两名。
    try:
        _watch_set = {prefixed(c) for c in _watch_codes}
        _hcodes = [bare(c) for c in held.keys()]   # 行业表是裸码口径
        _held_secs = set()
        if _hcodes:
            _q = ",".join("?" * len(_hcodes))
            _held_secs = {r[0] for r in con.execute(
                "SELECT DISTINCT sector FROM stock_industry "
                f"WHERE code IN ({_q})", _hcodes).fetchall()}
        relevance_sort(con, plans, _watch_set, _held_secs)
    except Exception as e:  # noqa: BLE001 — 排序失败保持原序，不影响推送
        print(f"[intraday] 相关度排序失败（保持原序）: {e}")
    _codes = ({bare(p[0]) for p in plans} | {bare(c) for c in held}
              | {bare(prefixed(w)) for w in _watch_codes})
    _codes.discard("")
    snap, _qsrc = fetch_quotes(list(_codes))
    out["universe"] = len(snap)
    out["qsrc"] = _qsrc
    _need = len(_codes)
    if _need == 0 or len(snap) < _need * _QCOV:
        # 源异常（限流/改版）时**不推**——宁可不推，也不推一份基于残缺数据的
        # 判断（09-16 血案的教训：宁可发"数据异常"告警，也不发空壳/错壳）。
        out["reason"] = f"快照异常（{_qsrc} {len(snap)}/{_need}）"
        print(f"[intraday] {out['reason']} → 不推送")
        return out
    # 上一轮价格（本写入覆盖前快照）——供「从区外进入区内」转换检测。
    # 09-30 用户实测：早上推过的票下午跌进买区，被一次性去重吞掉不推。
    _prev_price = {c: pr for c, pr in con.execute(
        "SELECT code, price FROM snapshot_live WHERE date=? AND slot=?",
        (date, slot)) if pr}
    # 只落独立表：**不碰** klines / snapshot 主表
    con.executemany(
        "INSERT OR REPLACE INTO snapshot_live VALUES(?,?,?,?,?,?,?)",
        [(date, slot, prefixed(code), v.get("name", ""), v.get("price"),
          v.get("pct"), v.get("amt")) for code, v in snap.items()])
    con.commit()

    def q(code):
        return snap.get(bare(code))

    in_zone, above, below, stopped, limit = [], [], [], [], []
    breaks = []                       # 突破确认事件（F4 双轨买区）
    for code, name, action, lo, hi, stop, score in plans:
        v = q(code) or {}
        state, label = classify(v.get("price"), v.get("pct"), lo, hi, stop)
        _pd = None
        if v.get("price") and lo and hi:
            _pd = (0.0 if lo <= v["price"] <= hi
                   else round((v["price"] / hi - 1) * 100, 1)
                   if v["price"] > hi
                   else round((v["price"] / lo - 1) * 100, 1))
        prev_p = _prev_price.get(code)
        fresh = bool(prev_p and lo and hi
                     and not (lo <= prev_p <= hi)
                     and lo <= v["price"] <= hi)  # 区外(上或下)→区内
        # F4 双轨买区·突破确认事件（2026-10-05）：越过 3 日高×1.005 且
        # 当日涨幅 3-7%（强而不板）→ 新事件提醒；fresh = 上轮价未越过。
        bo = None
        try:
            _r = con.execute(
                "SELECT json_extract(extra,'$.breakout') "
                "FROM candidate_snapshots WHERE code=? AND date=?",
                (code, date)).fetchone()
            bo = _r[0] if _r else None
        except Exception:  # noqa: BLE001
            bo = None
        fresh_bo = False
        if (bo and v.get("price") and v.get("pct") is not None
                and v["price"] >= bo and 3.0 <= v["pct"] <= 7.0):
            fresh_bo = bool(prev_p and prev_p < bo) or prev_p is None
        item = {"code": code, "name": name or v.get("name", ""),
                "price": v.get("price"), "pct": v.get("pct"),
                "lo": lo, "hi": hi, "state": state, "label": label,
                "action": action, "pct_dist": _pd, "fresh_entry": fresh,
                "stop": stop, "score": score, "bo": bo}
        {"in_zone": in_zone, "above": above, "below": below,
         "broke_stop": stopped, "limit_up": limit}.get(state, []).append(item)
        if fresh_bo:
            item["fresh_bo"] = True
            breaks.append(item)
    # 持仓实时风控（与计划无关，独立成组）
    hold_hits = []
    for code, h in held.items():
        v = q(code) or {}
        price, stop = v.get("price"), h.get("stop")
        if price is None:
            continue
        state, label = classify(price, v.get("pct"), None, None, stop)
        if state == "broke_stop":
            hold_hits.append({"code": code, "name": h.get("name", ""),
                              "price": price, "pct": v.get("pct"),
                              "stop": stop, "label": label})

    # ★ 用户需求③：真实持仓盘中随时提示下一步，尤其「该卖出」的时候。
    # 用 evaluate_real_holdings 跑完整退出裁决（ATR保护线/MA20破位/盈亏），
    # 叠加盘中实时价判断是否已破止损 → 给出「建议卖出」紧急提示。
    # 与上面的 manual-stop 不同：这里走系统规则，不依赖用户手填止损价。
    sell_hits = []
    try:
        from . import executor as _ex
        if held:
            heval = _ex.evaluate_real_holdings(con, date, list(held.values()))
            for h in heval:
                code = h["code"]
                v = q(code) or {}
                live = v.get("price")
                stop = h.get("stop")
                if h.get("exit_action") == "SELL":
                    sell_hits.append({
                        "code": code, "name": h.get("name"),
                        "price": live, "pct": v.get("pct"), "stop": stop,
                        "verdict": h.get("verdict") or "建议减仓/离场",
                        "live_broke": bool(live is not None and stop
                                          and live <= stop)})
    except Exception as e:                       # noqa: BLE001
        print(f"[intraday] 真实持仓体检失败（不阻断）：{e}")
    out["sell_hits"] = len(sell_hits)

    # ---- 自选股到点提醒（用户 2026-09-21：盘中也要给自选操作建议）----
    # 与 watchlist.zone_stop_for 同一口径；持仓股已由体检覆盖，不重复。
    watch_zone_hits, watch_stop_hits = [], []
    try:
        from . import watchlist as _wl
        from .mood import is_limit_up as _lu
        _wc = _watch_codes
        for code in _wc:
            pc = prefixed(code)
            if pc in held:
                continue
            v = q(pc) or {}
            live = v.get("price")
            if not live:
                continue
            krows = con.execute(
                "SELECT date,o,c,h,l,v FROM klines WHERE code=? AND date<=? "
                "ORDER BY date DESC LIMIT 60", (pc, date)).fetchall()
            krows = [[d, o, c, h, l, vv]
                     for d, o, c, h, l, vv in reversed(krows)]
            if len(krows) < 30:
                continue
            prev = con.execute(
                "SELECT c FROM klines WHERE code=? AND date<? "
                "ORDER BY date DESC LIMIT 1", (pc, date)).fetchone()
            if prev and prev[0] and _lu(pc[2:], krows[-1][2], prev[0]):
                continue          # 涨停买不进，归连板通道
            zone, stop, _box, _plan = _wl.zone_stop_for(krows)
            nm = v.get("name") or ""
            if live <= stop:
                watch_stop_hits.append({
                    "code": pc, "name": nm, "price": live,
                    "pct": v.get("pct"), "stop": stop})
            elif zone[0] <= live <= zone[1]:
                watch_zone_hits.append({
                    "code": pc, "name": nm, "price": live,
                    "pct": v.get("pct"),
                    "lo": zone[0], "hi": zone[1]})
    except Exception as e:                       # noqa: BLE001
        print(f"[intraday] 自选到点检查失败（不阻断）：{e}")
    out["watch_zone"] = len(watch_zone_hits)
    out["watch_stop"] = len(watch_stop_hits)

    out.update({"plan_n": len(plans), "in_zone": len(in_zone),
                "broken": len(below), "stops": len(hold_hits),
                "above": len(above), "limit": len(limit),
                "breakouts": len(breaks)})
    print(f"[intraday] {slot} 快照{len(snap)}只 计划{len(plans)}只 → "
          f"在买区{len(in_zone)} 涨出{len(above)} 跌破{len(below)} "
          f"涨停{len(limit)} 持仓止损{len(hold_hits)}")

    # ---- 事件级告警账本（live_alerts）----
    # live 巡检每 10 分钟一轮，去重单位必须是"事件"而不是"轮次"：
    # 同票同事件当天只报一次（报过就不再重复，哪怕仍停在买区里）。
    # 卖出/止损类是事故级信号，账本过滤在**所有** slot 生效——
    # 早盘报过的卖出信号，尾盘不再对同一只重复报。
    _live = (slot == "live")
    _ensure_ledger(con)
    _prev = _alerted_set(con, date)
    sell_hits = _fresh(KIND_SELL, sell_hits, _prev)
    watch_stop_hits = _fresh(KIND_WSTOP, watch_stop_hits, _prev)
    hold_hits = _fresh(KIND_STOP, hold_hits, _prev)
    breaks = _fresh(KIND_BO, breaks, _prev)   # 同票同日只报一次
    out["breakouts"] = len(breaks)             # 计数与实推保持一致
    if _live:
        # 09-30：连板通道/观望不进「可买」组（逻辑漏洞修复）
        in_zone = [p for p in in_zone if p.get("action") in _LIVE_BUYABLE]
        # 09-30 三次修：**从区外新跌进区内 = 新事件必推**——早上的一次性
        # 去重把「有些跌下来了」的票全吞了（用户实测指正）。同票当日
        # 进区提醒上限 2 次（防来回震荡刷屏）；其余维持一日一次。
        _n0 = (len(in_zone), len(watch_zone_hits))

        def _zone_allowed(p):
            cnt = _entry_count(con, date, p["code"])
            if p.get("fresh_entry"):
                return cnt < 2            # 区外→区内：当日 ≤2 次
            return cnt == 0               # 区内未变：当日只报第一次
        in_zone = [p for p in in_zone if _zone_allowed(p)]
        watch_zone_hits = _fresh(KIND_WZONE, watch_zone_hits, _prev)
        _new = len(in_zone) + len(watch_zone_hits) + len(breaks)
        if _new == 0 and not sell_hits and not watch_stop_hits \
                and not hold_hits:
            out["reason"] = "live：无新事件（已报过的不再重复）"
            print(f"[intraday] {out['reason']} → 静默")
            return out
        print(f"[intraday] live 新事件：买区{len(in_zone)}"
              f"+自选{len(watch_zone_hits)}（原 {_n0[0]}+{_n0[1]}）")

    # ---- 打扰纪律：只有下列情形才推 ----
    groups = []
    if watch_zone_hits:
        _rows = [((w["code"], w["name"], f'{w["price"]:.2f}',
                   f'{w["pct"]:+.1f}%' if w["pct"] is not None else "—",
                   f'{w["lo"]:.2f}~{w["hi"]:.2f}'),
                  [_TXT, _TXT, _HL, _HL, _HL]) for w in watch_zone_hits]
        _rows, _hint = _cap_rows(
            _rows, "自选票回落到关注区间；按各自止损纪律执行")
        groups.append({
            "title": "★ 自选进入买区（可下单）", "hint": _hint, "rows": _rows})
    breaks = [b for b in breaks if b.get("action") in _LIVE_BUYABLE]
    if breaks:
        _rows = [((b["code"], b["name"], f'{b["price"]:.2f}',
                   f'{b["pct"]:+.1f}%' if b["pct"] is not None else "—",
                   f'{b["bo"]:.2f}'),
                  [_TXT, _TXT, _UP, _UP, _HL]) for b in breaks]
        _rows, _hint = _cap_rows(
            _rows, "越过突破确认价且强而不板（3-7%）；突破轨半仓试探，止损照旧")
        groups.append({
            "title": f"🔥 突破确认（{len(breaks)} 只越过确认价）",
            "hint": _hint, "rows": _rows})
    if hold_hits:
        groups.append({
            "title": "⚠ 持仓触及止损", "hint": "按纪律处置，勿临场改判",
            "rows": [((h["code"], h["name"], f'{h["price"]:.2f}',
                       f'{h["pct"]:+.1f}%' if h["pct"] is not None else "—",
                       f'止损 {h["stop"]:.2f}'), [_TXT, _TXT, _UP, _UP, _UP])
                     for h in hold_hits]})
    in_zone = [p for p in in_zone if p.get("action") in _LIVE_BUYABLE]
    if in_zone:
        _enrich(con, date, in_zone)
        _zh = {"am": "早盘", "pm": "尾盘"}.get(slot, "现价")
        _rows = [((p["code"],
                   p["name"] + (f'（{p["action"]}）' if _live
                                and p.get("action") else ""),
                   f'{p["price"]:.2f}',
                   f'{p["pct"]:+.1f}%' if p["pct"] is not None else "—",
                   f'{p["lo"]:.2f}~{p["hi"]:.2f}'),
                  [_TXT, _TXT, _HL, _HL, _HL]) for p in in_zone]
        if _live:
            # 09-30 用户口径：买点提示参照竞价**一票一卡**（render_card），
            # 卡内自带板块热度/强度/位置/确认。上限 8 张（卡片较大）。
            _cards = []
            for _i, p in enumerate(in_zone[:8]):
                cd = {"code": p["code"], "name": p.get("name", ""),
                      "zone": [p.get("lo"), p.get("hi")],
                      "close": p.get("price"), "dist_pct": p.get("pct_dist"),
                      "stop": p.get("stop"), "sector": p.get("sector", ""),
                      "sector_pct": p.get("sector_pct"),
                      "pos_label": p.get("pos_label"),
                      "decisive": p.get("decisive"),
                      "confirms": p.get("confirms") or 0,
                      "score": p.get("score"),
                      "action": p.get("action", ""),
                      "invalid_if": (f"收盘跌破止损 {p['stop']:.2f}"
                                     if p.get("stop") else "条件破坏即失效"),
                      "valid_until": date}
                _cards.append(render_card(
                    cd, first=(_i == 0),
                    head=f"【{p.get('action') or '买点触发'}】"))
            if len(in_zone) > 8:
                _cards.append(
                    f'<div style="font-size:12px;color:#9aa0a6">'
                    f'另有 {len(in_zone) - 8} 只见网页版完整详情</div>')
            groups.append({
                "title": f"● 现在可以买入（{len(in_zone)} 只在买区内）",
                "hint": "现价在买区内，照价下单即可；次日可卖",
                "html": "".join(_cards)})
        else:
            _rows, _hint = _cap_rows(
                _rows, "现价已在计划买区内；收盘前有效，次日可卖")
            groups.append({
                "title": f"● {_zh}进入买区（可当日下单）", "hint": _hint,
                "rows": _rows})
    if slot == "am" and plans and len(below) * 2 >= len(plans):
        groups.append({
            "title": "○ 盘前计划转差", "hint": "多数标的已跌破买区下沿，当日不宜按计划挂单",
            "rows": [((p["code"], p["name"],
                       f'{p["price"]:.2f}' if p["price"] else "—",
                       f'{p["pct"]:+.1f}%' if p["pct"] is not None else "—",
                       f'下沿 {p["lo"]:.2f}' if p["lo"] else "—"),
                      [_TXT, _TXT, _DN, _DN, _DN]) for p in below]})
    if slot == "pm" and above:
        # 09-30 用户口径：未到买点的票不单独推，并入尾盘一起总结；
        # 09-30 晚二次修：只列**偏离买区 ≤10%** 的——6 天前的旧买区，
        # 股票已涨离 20% 还列"等回踩"就是"没根据实盘"（用户实测指正）。
        # pct_dist 在分类循环里登记（正=高于上沿 %）。
        _near_ab = [p for p in above
                    if 0 < (p.get("pct_dist") or 0) <= 10]
        if _near_ab:
            groups.append({
                "title": f"○ 未到买点 · {len(_near_ab)} 只"
                         f"（高于买区 ≤10%，等回踩）",
                "hint": "现价高于买区上沿，回踩到位再买；明日继续监控",
                "rows": [((p["code"], p["name"],
                           f'{p["price"]:.2f}' if p["price"] else "—",
                           f'{p["pct"]:+.1f}%' if p["pct"] is not None else "—",
                           f'上沿 {p["hi"]:.2f}' if p["hi"] else "—"),
                          [_TXT, _TXT, _DN, _DN, _DN])
                         for p in _near_ab[:14]]})
    if slot == "pm" and not in_zone:
        # 跌破 ≤10% 的才有"等回升"意义；跌穿太远的已是破位票，不列。
        # 09-30 深夜修：过滤必须发生在建组**之前**——否则全部跌破 >10%
        # 时会推出一张空表（有标题无内容）。
        below = [p for p in below if (p.get("pct_dist") is not None
                                      and -10 <= p["pct_dist"] < 0)]
    if slot == "pm" and not in_zone and below:
        groups.append({
            "title": "○ 计划整体走弱", "hint": "尾盘无一进入买区，跌破者已标注",
            "rows": [((p["code"], p["name"],
                       f'{p["price"]:.2f}' if p["price"] else "—",
                       f'{p["pct"]:+.1f}%' if p["pct"] is not None else "—",
                       f'下沿 {p["lo"]:.2f}' if p["lo"] else "—"),
                      [_TXT, _TXT, _DN, _DN, _DN]) for p in below]})

    out["_groups"] = groups
    if not groups and not sell_hits and not watch_stop_hits:
        out["reason"] = "无实质变化（静默）"
        print("[intraday] 无实质变化 → 静默不发（不占推送额度）")
        return out
    if dry:
        out["reason"] = "dry-run（未推送）"
        return out

    from . import notifier
    _ts = f"{now:%H:%M:%S}"
    # 真实持仓卖出信号：独立紧急推送（force + 单独日熔丝），确保一定送达，
    # 不与计划组互相吃掉额度；用户需求③「尤其要卖出的时候」优先保障。
    # 2026-09-26 起 force 之上叠加事件账本：同一只当天只报一次，
    # 否则 live 高频轮询会把同一信号连发 20 遍。
    if sell_hits:
        sgroup = [{
            "title": "🚨 持仓建议卖出（盘中）",
            "hint": "系统判定需减仓/离场，请尽快处理；已破止损者优先",
            "rows": [((s["code"], s["name"],
                       f'{s["price"]:.2f}' if s["price"] else "—",
                       f'{s["pct"]:+.1f}%' if s["pct"] is not None else "—",
                       (s["verdict"] + "·已破止损" if s["live_broke"]
                        else s["verdict"])),
                      [_TXT, _TXT, _UP, _UP, _UP]) for s in sell_hits]}]
        # 09-30 用户口径：「不要建议卖出结果后续的什么都没有」——
        # 卖出信号必须同时给出资金去向：现价可换入的票，或明说持币观望。
        if in_zone:
            _enrich(con, date, in_zone[:5])
            sgroup.append({
                "title": "🔁 卖出资金去向（现价可换入）",
                "hint": "以下为当下在买区内、可照价下单的标的，自行挑选",
                "rows": [((p["code"], p["name"],
                           f'{p["price"]:.2f}' if p["price"] else "—",
                           f'{p["lo"]:.2f}~{p["hi"]:.2f}',
                           (p.get("sector") or "—")
                           + (f' 🔥{p["sector_pct"]:+.1f}%'
                              if p.get("sector_pct") is not None else "")),
                          [_HL, _HL, _HL, _HL, _MUT]) for p in in_zone[:5]]})
        else:
            sgroup.append({
                "title": "🔁 卖出资金去向",
                "hint": "",
                "rows": [(("—", "当前无可换入标的（候选均未到买点或溢价过高）",
                           "建议：卖出后持币观望", "勿强行换股"),
                          [_MUT, _TXT, _DN, _MUT])]})
        shtml = render_html(date, slot, now, sgroup, len(plans))
        sr = notifier.push("holding_intraday", f"持仓卖出信号 {date[5:]}",
                           shtml, date=date, con=con, force=True)
        out["sell_pushed"] = bool(sr.get("sent"))
        if out["sell_pushed"]:
            _mark_alerted(con, date, KIND_SELL, sell_hits, _ts)
        print(f"[intraday] holding sell push={sr}")
    # 自选破止损：同为确定性事故级信号，独立 force 推送（不被日熔丝吞掉）
    if watch_stop_hits:
        wgroup = [{
            "title": "🚨 自选跌破止损（盘中）",
            "hint": "关注票已破位——放弃买入计划；已持有者按止损纪律处理",
            "rows": [((w["code"], w["name"], f'{w["price"]:.2f}',
                       f'{w["pct"]:+.1f}%' if w["pct"] is not None else "—",
                       f'止损 {w["stop"]:.2f}'), [_TXT, _TXT, _UP, _UP, _UP])
                     for w in watch_stop_hits]}]
        whtml = render_html(date, slot, now, wgroup, len(plans))
        wr = notifier.push("watch_intraday", f"自选破止损 {date[5:]}",
                           whtml, date=date, con=con, force=True)
        out["watch_stop_pushed"] = bool(wr.get("sent"))
        if out["watch_stop_pushed"]:
            _mark_alerted(con, date, KIND_WSTOP, watch_stop_hits, _ts)
        print(f"[intraday] watch stop push={wr}")
    if _live and not groups:
        out["reason"] = "live：新事件均已单独推送（买区无变化）"
        print(f"[intraday] {out['reason']}")
        return out
    head = {"am": "早盘校验", "pm": "尾盘机会"}.get(slot, "买点巡检")
    title = (f"⚡ 买点触发 {date[5:]} {now:%H:%M}" if _live
             else f"盘中{head} {date[5:]}")
    html = render_html(date, slot, now, groups, len(plans))
    # live 的去重已经由事件账本完成（同票同事件一天一次）⇒ 必须 force
    # 绕过"每 mode 一天一条"的日熔丝，否则当天第二个新事件永远发不出。
    # am/pm 维持日熔丝语义（一天一条摘要）不变。
    # ASTOCK_FORCE_PUSH=1（演练/补发）时三种盘中格式都可强制重发
    _live_head = ""
    if _live and in_zone:
        _names = "、".join(p.get("name") or p["code"]
                           for p in in_zone[:2])
        _live_head = (f"{_names}" + (f"等{len(in_zone)}只"
                                     if len(in_zone) > 2 else ""))
    # ★ 2026-10-05 用户需求：「行情差提示我观望/空仓，不要让我高位被套」——
    # 到价照报（告知不缺席），但差裁决日标题必须带纪律，防误读成追高许可。
    try:
        _vdrow = con.execute(
            "SELECT verdict FROM day_meta WHERE date=?", (date,)).fetchone()
        if _vdrow and _vdrow[0] in ("离场为主", "观望为主"):
            title = (f"⚠️{_vdrow[0]}·到价仅提示 "
                     f"{date[5:]} {now:%H:%M}" if _live
                     else f"⚠️今日{_vdrow[0]} · 盘中{head} {date[5:]}")
    except Exception:                        # noqa: BLE001 — 读不到不阻断
        pass
    r = notifier.push(f"intraday_{slot}", title, html, date=date, con=con,
                      force=_live or os.environ.get("ASTOCK_FORCE_PUSH") == "1",
                      headline=_live_head)
    if r.get("sent"):
        # 主推送送达后记账（所有 slot）：早盘/尾盘摘要报过的买区票也要登记，
        # 否则 5 分钟后的 live 轮次会把同一只再报一遍。
        if hold_hits:
            _mark_alerted(con, date, KIND_STOP, hold_hits, _ts)
        if in_zone:
            _mark_alerted(con, date, KIND_ZONE, in_zone, _ts)
        if watch_zone_hits:
            _mark_alerted(con, date, KIND_WZONE, watch_zone_hits, _ts)
    out["pushed"] = bool(r.get("sent"))
    out["push"] = r
    out["reason"] = ("已推送" if r.get("sent")
                     else ("去重拦截" if r.get("dedup") else "推送未送达"))
    print(f"[intraday] push={r}")
    return out


def run_cli():
    import argparse
    ap = argparse.ArgumentParser(description="盘中计划校验（M41）")
    ap.add_argument("--slot", default="pm", choices=["am", "pm", "live"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    run(slot=a.slot, date=a.date, dry=a.dry)


if __name__ == "__main__":
    run_cli()
