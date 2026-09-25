# -*- coding: utf-8 -*-
"""板块热度与个股行业归属（2026-09-18 新增，用户需求）。

为什么需要这个模块
------------------
seeking「推荐要标注板块热度」时先查了老代码，发现两处**静默失效**：
  1. `scoring.compute_top_picks` 里的 `sector_temp` 优选因子（❄弱×0.90 /
     🔥强×1.03）**从来没有数据源**——全仓库没有任何一处写入该字段，
     所以它恒为 None，板块冷热加成**从未生效过**；
  2. 同板块去重的 `sector_of=lambda c: c.get("sector", c["pool"])` 里
     `sector` 也是空的，于是退化成"按池别去重"——**不同行业的波段票被
     当成同一板块互斥，每次只留一只**（这是推荐数量偏少的隐藏原因之一）。
本模块把数据源补上，两处因子随即复活。

数据源（东财 push2delay，纯 HTTP、零鉴权，与 fetch_daily 同一 host）
-------------------------------------------------------------------
  · 板块行情 `fs=m:90+t:2`（行业板块）→ 涨幅 f3 / 主力净额 f62 / 涨跌家数
  · 个股行业 `f100` 字段（全市场 clist）→ 裸码 → 行业名

纪律（与 intraday 同级）
------------------------
  1. **绝不阻断主链**：任何异常都降级为"未标注"，不抛、不返回空壳；
  2. 落库只在**有数据**时写，绝不用空表覆盖昨日有效数据；
  3. 行业映射带 TTL（默认 7 天）——半年才变一次，不必每天多打几十页请求；
  4. 请求一律走 core.fetch_text（复用限流器与熔断器，不另开裸连接）。
"""
import json
import time

from .core import fetch_text

BOARD_URL = ("https://push2delay.eastmoney.com/api/qt/clist/get?"
             "pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&"
             "fs=m:90+t:2&fields=f12,f14,f3,f62,f104,f105")
INDUSTRY_URL = ("https://push2delay.eastmoney.com/api/qt/clist/get?"
                "pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&"
                "fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f100")

INDUSTRY_TTL_DAYS = 7        # 行业归属缓存有效期（行业分类极少变动）
MAX_PAGES = 80               # 安全阀：80 页 = 8000 只

HOT_PCT = 2.0                # 板块涨幅 ≥2% → 🔥强
COLD_PCT = -1.5              # 板块涨幅 ≤-1.5% → ❄弱


def _num(v):
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def _rows(data):
    """clist 的 diff 可能是 list 或 dict（东财两种都出现过）→ 统一为 list。"""
    diff = (data or {}).get("diff") or []
    if isinstance(diff, dict):
        return [v for v in diff.values() if isinstance(v, dict)]
    return diff


def fetch_board(max_pages=2, timeout=15):
    """行业板块当日行情 → {板块名: {pct, net_yi, up, down}}；失败返回 {}。"""
    out = {}
    for pn in range(1, max_pages + 1):
        try:
            js = json.loads(fetch_text(BOARD_URL.format(pn=pn), timeout=timeout))
        except Exception as e:  # noqa: BLE001 —— 源抖动不得影响主链
            print(f"[sector] 板块榜第 {pn} 页失败：{type(e).__name__} {e}")
            break
        data = js.get("data") or {}
        rows = _rows(data)
        if not rows:
            break
        for r in rows:
            name = (r.get("f14") or "").strip()
            if not name:
                continue
            net = _num(r.get("f62"))
            out[name] = {"pct": _num(r.get("f3")),
                         "net_yi": round(net / 1e8, 2) if net is not None else None,
                         "up": r.get("f104"), "down": r.get("f105")}
        total = data.get("total") or 0
        if total and pn * 100 >= total:
            break
    return out


def fetch_industry(max_pages=MAX_PAGES, timeout=20):
    """全市场个股 → 行业名（f100）。返回 {裸码: 行业名}；失败返回 {}。"""
    out = {}
    for pn in range(1, max_pages + 1):
        try:
            js = json.loads(fetch_text(INDUSTRY_URL.format(pn=pn), timeout=timeout))
        except Exception as e:  # noqa: BLE001
            print(f"[sector] 行业映射第 {pn} 页失败：{type(e).__name__} {e}")
            break
        data = js.get("data") or {}
        rows = _rows(data)
        if not rows:
            break
        for r in rows:
            code = str(r.get("f12") or "")
            sec = (r.get("f100") or "").strip()
            if len(code) == 6 and code.isdigit() and sec and sec != "-":
                out[code] = sec
        total = data.get("total") or 0
        if total and pn * 100 >= total:
            break
    return out


# ---------------------------------------------------------------------------
# 落库 / 读取
# ---------------------------------------------------------------------------

def save_board(con, date, board):
    """只在有数据时写（空表覆盖会让"昨日还有热度标注"变成"今天没有"）。"""
    if not board:
        return 0
    rows = [(date, name, v.get("pct"), v.get("net_yi"), v.get("up"), v.get("down"))
            for name, v in board.items()]
    con.executemany("INSERT OR REPLACE INTO sector_heat VALUES(?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def save_industry(con, mapping, now=None):
    if not mapping:
        return 0
    ts = now or time.strftime("%Y-%m-%d")
    con.executemany("INSERT OR REPLACE INTO stock_industry VALUES(?,?,?)",
                    [(c, s, ts) for c, s in mapping.items()])
    con.commit()
    return len(mapping)


def load_board(con, date=None):
    """读板块榜。date=None → 取库中最新一天的榜（站点/推送复用）。"""
    try:
        if date is None:
            row = con.execute("SELECT MAX(date) FROM sector_heat").fetchone()
            date = row[0] if row and row[0] else None
        if not date:
            return {}
        rows = con.execute(
            "SELECT sector, pct, net_yi, up, down FROM sector_heat WHERE date=?",
            (date,)).fetchall()
    except Exception:  # noqa: BLE001 —— 老库无此表
        return {}
    return {r[0]: {"pct": r[1], "net_yi": r[2], "up": r[3], "down": r[4]}
            for r in rows}


def load_industry(con, ttl_days=INDUSTRY_TTL_DAYS):
    """读行业映射；超过 TTL 视为过期 → 返回 {}（触发重新拉取）。"""
    try:
        rows = con.execute(
            "SELECT code, sector, updated_at FROM stock_industry").fetchall()
    except Exception:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    latest = max((r[2] or "") for r in rows)
    if latest:
        try:
            from datetime import date as _d, datetime as _dt
            age = (_d.today() - _d(*[int(x) for x in latest.split("-")])).days
            if age > ttl_days:
                return {}
        except Exception:  # noqa: BLE001 —— 日期格式异常按可用处理
            pass
    return {r[0]: r[1] for r in rows if r[1]}


def refresh(con, date, force=False):
    """刷新板块榜 + 行业映射。返回 {sector: {...}}（失败 → 用库中旧数据兜底）。"""
    board = fetch_board()
    if board:
        n = save_board(con, date, board)
        print(f"[sector] 板块榜 {n} 个行业已入库（{date}）")
    else:
        board = load_board(con, date) or load_board(con)
        if board:
            print(f"[sector] 板块榜抓取失败 → 用库中最近一份（{len(board)} 个行业）")
    ind = {} if force else load_industry(con)
    if not ind:
        ind = fetch_industry()
        if ind:
            save_industry(con, ind)
            print(f"[sector] 行业映射 {len(ind)} 只已入库")
    return board


# ---------------------------------------------------------------------------
# 标注
# ---------------------------------------------------------------------------

def temp_of(pct):
    """板块涨幅 → 冷热档位。字符串与 scoring 的判定字面量严格一致。"""
    if pct is None:
        return ""
    if pct >= HOT_PCT:
        return "🔥强"
    if pct <= COLD_PCT:
        return "❄弱"
    return "·平"


def rank_board(board, n=6):
    """涨幅榜前 n（附主力净额）——推送顶部的"今日板块热度"。"""
    items = [(k, v) for k, v in board.items() if v.get("pct") is not None]
    items.sort(key=lambda kv: -kv[1]["pct"])
    return [{"sector": k, "pct": v["pct"], "net_yi": v.get("net_yi"),
             "temp": temp_of(v["pct"])} for k, v in items[: n]]


def annotate(con, date, cands, board=None, industry=None):
    """给候选打板块标注：sector / sector_pct / sector_temp / sector_net_yi。

    返回 (board, hot_list)。任何一步失败都只是"没标注"，不影响候选本身。
    """
    board = board if board is not None else load_board(con, date)
    if not board:
        return board, []
    if industry is None:
        industry = load_industry(con)
    if not industry:
        try:
            rows = con.execute("SELECT code, sector FROM stock_industry").fetchall()
            industry = {r[0]: r[1] for r in rows}
        except Exception:  # noqa: BLE001
            industry = {}
    ranked = {s["sector"]: i for i, s in enumerate(rank_board(board, n=99))}
    for c in cands:
        num = c["code"][2:] if c["code"][:2] in ("sh", "sz", "bj") else c["code"]
        sec = industry.get(num) or c.get("sector") or ""
        if not sec or sec not in board:
            continue
        v = board[sec]
        c["sector"] = sec
        c["sector_pct"] = v.get("pct")
        c["sector_net_yi"] = v.get("net_yi")
        c["sector_temp"] = temp_of(v.get("pct"))
        c["sector_rank"] = ranked.get(sec)
    return board, rank_board(board, n=6)


def sector_tag(c):
    """候选行用的板块短标签：`半导体+3.2%🔥`（无数据 → 空串）。"""
    sec = c.get("sector")
    if not sec:
        return ""
    pct = c.get("sector_pct")
    t = c.get("sector_temp") or ""
    mark = {"🔥强": "🔥", "❄弱": "❄", "·平": ""}.get(t, "")
    if pct is None:
        return f"{sec}{mark}"
    return f"{sec}{pct:+.1f}%{mark}"


def retreat_signal(con, date, sector, lookback=3):
    """板块退潮检测（用户 2026-09-21：「考虑板块更换周期，不要才进去就暴跌」）。

    口径（满足任一即判退潮）：
      ① 最近 lookback 日累计涨幅 ≤ -3%（资金撤离中）；
      ② 连续 ≥2 日下跌 且 最新一日 ≤ -1%（破位下台阶）。
    数据不足（板块历史 <2 日）→ 不判退潮（None）——绝不因数据缺失误杀。
    返回 {"retreat": bool|None, "streak_down": int, "cum": float, "detail": str}。
    """
    rows = con.execute(
        "SELECT pct FROM sector_heat WHERE sector=? AND date<=? "
        "ORDER BY date DESC LIMIT ?", (sector, date, lookback)).fetchall()
    pcts = [r[0] for r in rows][::-1]          # 正序
    if len(pcts) < 2:
        return None
    cum = sum(pcts)
    streak = 0
    for v in reversed(pcts):
        if v < 0:
            streak += 1
        else:
            break
    if cum <= -3.0:
        return {"retreat": True, "streak_down": streak, "cum": round(cum, 2),
                "detail": f"近{len(pcts)}日累计 {cum:+.1f}%，资金撤离"}
    if streak >= 2 and pcts[-1] <= -1.0:
        return {"retreat": True, "streak_down": streak, "cum": round(cum, 2),
                "detail": f"连跌{streak}日（最新 {pcts[-1]:+.1f}%），下台阶"}
    return {"retreat": False, "streak_down": streak, "cum": round(cum, 2),
            "detail": ""}


def sector_state(con, date, sector, temp=None, pct=None):
    """板块所处阶段（用户 2026-09-22：「是不是主线高潮板块、接力板块，
    还是退潮的」）。返回 (state, detail)：
      退潮   —— retreat_signal 命中（资金撤离/下台阶），**禁止高位接力**
      高潮   —— 🔥强 且 当日涨幅 ≥2%（情绪顶部特征，只减不加）
      强势   —— 🔥强 但涨幅温和（主升/接力可参与）
      启动   —— 今日 +1% 以上但还不算强（低位启动，高低切换受益者）
      低温   —— ❄️冷或平（回避）
    数据缺失 → ("未知", "")——不装懂。"""
    ret = retreat_signal(con, date, sector)
    if ret and ret.get("retreat"):
        return "退潮", ret["detail"]
    if temp is None:
        row = con.execute(
            "SELECT pct FROM sector_heat WHERE sector=? AND date=?",
            (sector, date)).fetchone()
        pct = row[0] if row else None
    if pct is None:
        return "未知", ""
    if pct >= 2.0 and (temp or "") == "🔥强":
        return "高潮", f"当日 {pct:+.1f}%，情绪顶部特征——只减不加，严禁高位接力"
    if (temp or "") == "🔥强":
        return "强势", f"当日 {pct:+.1f}%，主升/接力可参与"
    if pct >= 1.0:
        return "启动", f"当日 {pct:+.1f}%，低位启动——高低位切换的受益方向"
    return "低温", f"当日 {pct:+.1f}%，回避"
