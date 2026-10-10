# -*- coding: utf-8 -*-
"""数据抓取入口：全市场日K + 收盘快照 → SQLite（含全部质量防线）。

用法：python -m pipeline.fetch_daily [--days 40] [--limit N]

## 抓取深度（2026-09-16 调整：260 → 40）

「--days N」= 每只票往回拉 N 个交易日的日K（每天 1 根）。
260 根 ≈ 一年历史（A股年均 ~243 个交易日）。

**为什么从 260 降到 40**：全仓代码扫描确认，所有引擎/指标的最大回看是
`[-32:]`（publish.py），次高 `[-30:]`（engines.py），其余都在 20 以内。
即「用 32 根、拉 260 根」—— 解析/写库量白耗 8 倍。降到 40 根仍留
+25% 余量（40 vs 32）。CI 里盘前/竞价任务本就跑 `--days 20`，40 比它宽一倍。

**历史数据不受影响**：库里 1996 年以来的存量 K线是**只增不改**的，
本参数只决定「每次新拉多少根」，不删旧数据。所以要算更长周期
（如 250 日年线）依然有料可用 —— 只是不再每天重复拉旧数据。
"""
import argparse
import json
import os
import re
import time
import urllib.request

from . import core, gapfill, quality
from . import trade_calendar as holiday_cal
from .core import (get_conn, fetch_text, kline_batch, trade_calendar,
                   is_trading_day, today_str, upsert_klines,
                   corp_action_scan, BROWSER_UA)

# 默认回看根数（每票每次新拉的日K数）。见模块 docstring 的调参依据。
DEFAULT_DAYS = 40
# 增量路径的尾巴根数：够覆盖最长回看（32）再留余量。
# 增量票只需补最近几根，不必按 FULL 的量级拉。
INC_DAYS = 20
# 全量路径的硬上限：新票/断档票最多拉这么多，防冷库首拉拖爆 timeout。
MAX_FULL_DAYS = 40


EM_SNAPSHOT = ("https://push2delay.eastmoney.com/api/qt/clist/get?"
               "pn=1&pz=6000&po=1&np=1&fltt=2&invt=2&"
               "fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&"
               "fields=f2,f3,f5,f6,f8,f12,f14,f20,f21")


def _num(v):
    """EM 快照停牌/缺失字段返回 '-'，统一转 float 或 None。"""
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def fetch_universe(max_stocks=None, fallback=False):
    """全市场清单+收盘快照。EM clist 单页上限约 100 条 → 按 pn 分页拉全。

    fallback=True（2026-09-27，盘中巡检专用）：EM 不可用/残缺（<500 只）
    时按 腾讯→新浪 整市场接口兜底。兜底数据的 vol/amt/turn/fmv 口径与
    EM 不同（部分为空），**只服务盘中价校验**（classify 只用 price/pct），
    绝不进主链抓取/质量闸/引擎计算——主链调用不带本参数，行为零变化。"""
    out = _universe_em(max_stocks)

    # 2026-09-29：EM 被限流时有一种**降级形态**——行数正常但 pct 全零
    # （当日收盘构建 data_blocked 的元凶）。与"行数不足"同等对待。
    def _degenerate(u):
        if len(u) < 500:
            return True
        # ⚠️ 盘前集合竞价前 pct 全零是**正常现象**（quality.py M02 有明文），
        # 只有价格也缺失/为零才是 EM 降级形态（09-29 实测）。
        sample = list(u.values())[:200]
        pcts = [v.get("pct") for v in sample]
        prices = [v.get("price") for v in sample]
        return (all(p in (None, 0) for p in pcts)
                and all(pr in (None, 0) for pr in prices))

    if not max_stocks and _degenerate(out):
        # 瞬时限流常有 1~2 分钟窗口：退避后原地重试一次，再不行才走兜底
        print(f"[fetch] EM 异常（{len(out)} 只/pct全零？）→ 45s 后重试一次",
              flush=True)
        time.sleep(45)
        out = _universe_em(max_stocks)
    # 兜底链：EM 失败（0 只/降级/明显残缺 <500 只）时才触发；
    # max_stocks（受限拉取/测试上下文）不兜底，语义保持原样。
    if fallback and not max_stocks and _degenerate(out):
        # 2026-09-27：腾讯排行接口返回 400（疑似改版），新浪实测 5221 只/43s
        # 可用 → 新浪为主兜底，腾讯留作其后（接口恢复即自动生效）。
        for src, fn in (("sina", _universe_sina), ("tx", _universe_tx)):
            try:
                alt = fn()
            except Exception as e:                  # noqa: BLE001
                print(f"[fetch] {src} 兜底失败: {type(e).__name__} {e}")
                alt = {}
            if len(alt) >= 500:
                print(f"[fetch] EM 不可用（{len(out)} 只）→ {src} 兜底 "
                      f"{len(alt)} 只（仅盘中价校验口径）", flush=True)
                return alt
        print(f"[fetch] 全部来源失败：EM {len(out)} 只，兜底亦不可用")
    return out


def _universe_em(max_stocks=None):
    """EM 主源：push2delay clist 分页（主链唯一口径，质量闸按它校准）。"""
    out = {}
    page = 1
    total = None
    while True:
        url = (f"https://push2delay.eastmoney.com/api/qt/clist/get?"
               f"pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&"
               "fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&"
               "fields=f2,f3,f5,f6,f8,f12,f14,f20,f21")
        try:
            js = json.loads(fetch_text(url))
        except Exception:  # noqa: BLE001
            break
        data = js.get("data") or {}
        total = data.get("total") or total
        diff = data.get("diff") or []
        if not diff:
            break
        for row in diff:
            num = row.get("f12", "")
            if re.match(r"^\d{6}$", num):
                out[num] = {"name": row.get("f14", ""),
                            "price": _num(row.get("f2")),
                            "pct": _num(row.get("f3")),
                            "vol": _num(row.get("f5")),
                            "amt": _num(row.get("f6")),
                            "turn": _num(row.get("f8")),
                            "fmv": _num(row.get("f21"))}
        if max_stocks and len(out) >= max_stocks:
            break
        if total and len(out) >= total:
            break
        page += 1
        if page > 80:   # 安全阀：80 页 = 8000 只
            break
    return out


def _universe_tx():
    """腾讯整市场快照（proxy.finance.qq.com 排行接口，200 只/页）。

    只保证 price/pct/name（盘中 classify 的全部所需）；其余字段留空。"""
    out = {}
    offset, total = 0, None
    while True:
        url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?"
               "board=aStock&sort_type=price&direct=down&"
               f"offset={offset}&count=200")
        js = json.loads(fetch_text(url, timeout=15))
        d = js.get("data") or {}
        total = d.get("total") or total
        rows = d.get("rank_stocks") or []
        if not rows:
            break
        for r in rows:
            code = (r.get("code") or "")
            num = code[2:] if re.match(r"^(sh|sz)\d{6}$", code) else ""
            if not num:
                continue
            out[num] = {"name": r.get("name", ""), "price": _num(r.get("zxj")),
                        "pct": _num(r.get("zdf")), "vol": None, "amt": None,
                        "turn": None, "fmv": None}
        offset += 200
        if total and offset >= int(total):
            break
        if offset > 12000:                          # 安全阀
            break
        time.sleep(0.25)
    return out


# 键名带不带引号都要能吃：接口不同时期返回过两种形态（09-27 实测为带引号）
_SINA_ROW = re.compile(r'"?(\w+)"?\s*:(?:"([^"]*)"|(-?[\d.]+))')


def _parse_sina_page(text, out):
    """新浪分页解析。优先 json.loads（块是合法 JSON，正确解 unicode 转义）；
    键名不带引号的历史形态再退正则（正则会把转义序列留成字面量，
    所以它只是兜底不是主路）。返回原始块数（空页判定用——bj 等被过滤
    代码占满的页 ≠ 空页）。"""
    blocks = re.findall(r"\{[^{}]*\}", text)
    for blk in blocks:
        kv = None
        try:
            got = json.loads(blk)
            if isinstance(got, dict):
                kv = got
        except Exception:                           # noqa: BLE001
            kv = None
        if kv is None:
            kv = {}
            for m in _SINA_ROW.finditer(blk):
                kv[m.group(1)] = m.group(2) if m.group(2) is not None \
                    else m.group(3)
        sym = str(kv.get("symbol", ""))
        num = sym[2:] if re.match(r"^(sh|sz)\d{6}$", sym) else ""
        if not num:
            continue
        out[num] = {"name": str(kv.get("name", "")),
                    "price": _num(kv.get("trade")),
                    "pct": _num(kv.get("changepercent")),
                    "vol": _num(kv.get("volume")),
                    "amt": _num(kv.get("amount")),
                    "turn": _num(kv.get("turnoverratio")), "fmv": None}
    return len(blocks)


def _universe_sina():
    """新浪整市场快照（hs_a 节点，100 只/页）。字段口径：amount=元、
    turnoverratio=%，vol 单位与 EM 不同——同样只服务盘中价校验。
    ⚠️ symbol 升序时第 1 页全是北交所(bj)代码，会被 sh/sz 过滤清零——
    空页判定必须看**原始块数**，不能看过滤后新增数（09-27 实测踩坑）。"""
    out = {}
    page = 1
    while True:
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
               "json_v2.php/Market_Center.getHQNodeData?page="
               f"{page}&num=100&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page")
        try:
            text = urllib.request.urlopen(
                urllib.request.Request(
                    url, headers={"User-Agent": BROWSER_UA,
                                  "Referer": "https://finance.sina.com.cn"}),
                timeout=15).read().decode("gbk", "replace")
        except Exception:                           # noqa: BLE001
            break
        raw_n = _parse_sina_page(text, out)         # 真空页（0 块）才算完
        if raw_n == 0:
            break
        if len(out) >= 6000:                        # 安全阀：全市场 ≈5400
            break
        page += 1
        time.sleep(0.25)
    return out


def guard_snapshot(universe, today, con=None, partial=False, premarket=False):
    """M02 分级守门：单位错误阻断；超历史分布→告警隔离复核（不自动改写）；
    盘中/部分抓取不套用完整交易日阈值。返回 (level, reason)。

    ⚠️ 2026-09-16 修（血案：CI run 34914806060 盘前任务 failure）：
    原逻辑「pct 全 0 ⇒ 疑似休市日 ⇒ 抛异常」对**盘前时段**是**必然误判**——
    08:50 集合竞价还没开始，快照接口返回的涨跌幅本来就是全 0。
    实测当日 08:50 定时任务因此 ValueError → 抓取步骤 failure
    → 后续「构建+推送」整步 skipped → **用户全天收不到盘前推送**。
    修法：全 0 只在**非盘前**时才算休市嫌疑；盘前用 premarket=True 放行，
    且此时**明确跳过成交额分级**（盘前成交额天然极低，分级无意义）。
    """
    pcts = [v.get("pct") or 0 for v in universe.values()]
    all_zero = bool(pcts) and all(abs(p) < 1e-9 for p in pcts)
    if all_zero and not premarket:
        raise ValueError("快照 pct 全 0：疑似休市日，拒绝写库")
    if all_zero and premarket:
        print(f"[quality] 盘前时段快照 pct 全 0（{len(universe)} 只）"
              "→ 正常现象（集合竞价未开始），放行且跳过成交额分级")
        return "ok", "premarket all-zero"
    if partial:
        print(f"[quality] 部分抓取（{len(universe)} 只）→ 跳过全日成交额分级")
        return "ok", "partial fetch"
    total_amt = sum(v.get("amt") or 0 for v in universe.values())
    level, action, reason = quality.grade_total_amount(total_amt)
    if level == "block":
        raise ValueError(f"成交额守门阻断：{reason}")
    print(f"[quality] 成交额 {total_amt:.3e} → {level}/{action}: {reason}")
    return level, reason


def fetch_daily(days=DEFAULT_DAYS, limit=None, force=False, premarket=False):
    con = get_conn()
    today = today_str()
    # 法定节假日日历守门（吸收原项目 trade_calendar）：节假日 cron 不白跑；
    # --force 供手动补数（build 侧守门不放松：目标日必须是真实交易日且有K线）
    if not holiday_cal.is_trade_day(today) and not force:
        print(f"[fetch] {today} {holiday_cal.why_closed(today)} → 跳过抓取"
              "（手动补数请加 --force）")
        return {"date": today, "skipped": "holiday"}
    universe = fetch_universe(max_stocks=limit, fallback=True)
    level, _ = guard_snapshot(universe, today, con,
                              partial=bool(limit and len(universe) < 3000),
                              premarket=premarket)
    codes = sorted(universe.keys())
    # 市场准入前置（#486）：科创板/北交所等不可交易代码不发请求——省一半无效抓取
    from . import mktfilter
    codes = [c for c in codes if mktfilter.tradable(c)]
    # ★ 2026-09-25 修（用户「跟进的股票缺乏数据」）：**自选+持仓强制纳入**
    # 抓取集——它们哪怕不在当日快照（新上市/停牌/快照分页遗漏），也必须有
    # K线，否则自选建议永远「数据不足」。已同步的只补尾巴，成本可控。
    try:
        from .build import _codes_conf, load_holdings
        _follow = set(_codes_conf("WATCH_CODES", "watch.json"))
        _follow |= {h.get("code") for h in load_holdings() if h.get("code")}
        _bare = {(c[2:] if c[:2] in ("sh", "sz") else c) for c in _follow}
        for c in sorted(_bare):
            if c not in codes:
                codes.append(c)
        if _bare - set(codes):
            pass
        codes = sorted(set(codes))
        print(f"[fetch] 跟进强制纳入后: {len(codes)} 只")
    except Exception as e:  # noqa: BLE001
        print(f"[fetch] 跟进纳入失败（不影响主流程）: {e}")
    # 增量同步：已同步到最近交易日的只补近端尾巴（INC_DAYS 根），
    # 新票/断档票才全量拉——依托历史库做增量，不每轮重拉全部历史
    #
    # 2026-09-15 修复（全天零推送根因）：原判定锚 latest_td = "今日往前推的
    # 最近交易日"，而当日数据此时尚未入库 → last(09-14) < latest_td(09-15)
    # ⇒ 全市场 4993 只全被判"断档"走 days=260 全量路径，--days 20 的轻量
    # 盘前/竞价任务实际耗时 ≈53 分钟 > timeout 45 分钟被 cancel（或网络异常
    # 抛 failure），第 8 步「构建+推送」整步 skipped ⇒ 用户全天零推送。
    # 正确锚 = 库中已有的最新交易日：库已跟上上一交易日 ⇒ 只补近端尾巴。
    import datetime as _dt
    last_dates = dict(con.execute(
        "SELECT code, MAX(date) FROM klines WHERE code!='sh000001' "
        "GROUP BY code").fetchall())
    # 今日应达交易日（用于文案与就绪判断，不作断档锚）
    _d = _dt.date.fromisoformat(today)
    while not holiday_cal.is_trade_day(_d.isoformat()):
        _d -= _dt.timedelta(days=1)
    due_td = _d.isoformat()
    # 断档锚：库中最新日期与「上一交易日」取较新者——当日未入库不算断档。
    db_latest = max(last_dates.values()) if last_dates else ""
    prev_d = _dt.date.fromisoformat(due_td)
    prev_d -= _dt.timedelta(days=1)
    while not holiday_cal.is_trade_day(prev_d.isoformat()):
        prev_d -= _dt.timedelta(days=1)
    latest_td = max(db_latest, prev_d.isoformat())
    inc_codes, full_codes = [], []
    for c in codes:
        last = last_dates.get(("sh" if c.startswith("6") else "sz") + c)
        (inc_codes if last and last >= latest_td else full_codes).append(c)
    print(f"[fetch] universe={len(codes)} 增量={len(inc_codes)} 全量={len(full_codes)}"
          f" @ {today}（断档锚 {latest_td} / 库最新 {db_latest or '空'}）")
    pfx_of = lambda c: "sh" if c.startswith("6") else "sz"  # noqa: E731
    # 全量路径（新票/断档票）的根数上限。此前写 `min(days, 40) if days <= 20
    # else days` → 默认 days=260 时全量真的拉 260 根，冷库/长假期首拉拖成
    # 53 分钟超时。现在统一封顶 MAX_FULL_DAYS，显式传更大的 --days 也不越过
    # （历史补数请用 tools/ 里的专用脚本，不要靠日常抓取越界）。
    full_days = min(days, MAX_FULL_DAYS)

    # 2026-09-15：**分批流式写库**（此前是一次性囤全市场再写）。
    # 原实现把 4937 只 × 最多 260 根日K 全堆在 `batch` 字典里，实测本机
    # 两次在「增量 3256 只已拉完、全量 1681 只处理中」时段被系统 SIGKILL
    #（等价 CI runner 被杀）—— 峰值 ~300MB 纯数据 + 原始 JSON 解码瞬时副本，
    # 叠加 20 并发线程栈后触顶。改为每 CHUNK 拉一批、立刻入库并释放，
    # 内存占用恒定；且**每批 commit**，中途被杀也能保住已完成部分。
    CHUNK = 400
    written = 0
    all_ok = {}                 # code → 末行（下游量纲修复/覆盖率用，轻量）
    fail_codes = []             # 拉不到的票，留痕不静默

    def _drain(pairs, d):
        """拉一批 → 立即写库 → 释放。返回本批成功票数。"""
        nonlocal written
        got = kline_batch(pairs, days=d, con=con)
        n = 0
        for code, rows in got.items():
            if not rows:
                continue
            flags = corp_action_scan(rows)
            if len(flags) > 50:
                print(f"[warn] {code} 单日>50 只跳变 = 市场级 qfq 基准切换，"
                      "只披露不排除")
            full = ("sh" if code.startswith("6") else "sz") + code
            written += upsert_klines(con, full, rows)
            turn = universe[code].get("turn")
            if turn:
                con.execute(
                    "UPDATE klines SET turn=? WHERE code=? AND date=?",
                    (turn, full, today))
            all_ok[code] = rows[-1]
            n += 1
        con.commit()
        got.clear()
        return n

    for tag, group, d in (("增量", inc_codes, min(days, INC_DAYS)),
                          ("全量", full_codes, full_days)):
        for i in range(0, len(group), CHUNK):
            part = group[i:i + CHUNK]
            ok = _drain([(c, pfx_of(c)) for c in part], d)
            print(f"[fetch] {tag} {i + len(part)}/{len(group)}"
                  f"（本批 {ok}/{len(part)}）", flush=True)
            fail_codes.extend([c for c in part if c not in all_ok])

    # ⚠️ 2026-09-16 修（CI run 35000871359 的 build 崩溃真凶）：
    # 原条件 `if "000001" not in all_ok:` —— `all_ok` 的 key 是**裸码**，
    # 而裸码 `000001` 恰好是**平安银行（sz000001）**，它作为个股每轮都被正常
    # 抓取并写进 `all_ok` ⇒ 该条件**恒为 False** ⇒ **上证指数 sh000001
    # 的补拉被永久跳过**。
    # 后果链：sh000001 停在旧日期（实测 260 行、末位 2026-09-14）⇒
    # `trade_calendar()`（以 sh000001 为权威日历）不含当日 ⇒
    # `build.py` 的 `cal.index(date)` 抛
    # `ValueError: '2026-09-15' is not in list` ⇒ **构建崩溃、推送失败**。
    # 注意：这只影响**指数日历**，个股数据完全正常（同次日志 覆盖 100%）。
    # 修法：用**带前缀的指数标识**判断是否已有指数K线，与裸码空间彻底分开。
    idx_code = "sh000001"
    idx_last = con.execute(
        "SELECT MAX(date) FROM klines WHERE code=?", (idx_code,)).fetchone()
    # ⚠️ `MAX(date)` 在**空库**上返回一行 `(None,)` —— 该元组**truthy**，
    # 直接 `idx_last[0] >= latest_td` 会抛
    # `TypeError: '>=' not supported between instances of 'NoneType' and 'str'`
    # （CI run 35002284192 冷缓存实测踩中）。必须取到**值**再判空。
    idx_last_date = idx_last[0] if idx_last else None
    need_idx = not (idx_last_date and idx_last_date >= latest_td)
    if need_idx:                    # 指数落后于断档锚（或库中不存在）→ 单独补
        idx_batch = kline_batch([("000001", "sh")], days=full_days, con=con)
        if "000001" in idx_batch:
            upsert_klines(con, idx_code, idx_batch["000001"])
            con.commit()            # ← 显式提交：指数是**日历唯一来源**，
            #                          不 commit 会让日历继续缺当日（原代码漏了）
            print(f"[fetch] 指数 {idx_code} 已补至 "
                  f"{idx_batch['000001'][-1][0]}"
                  f"（此前 {idx_last_date or '无'}）")
            # ⚠️ **不写入 all_ok**：它的 key 是裸码，落进去会与
            # 平安银行（sz000001）撞车 → 污染量纲修复（第 251 行取
            # `all_ok[c][5]` 当流通股）与 self_heal 的补数范围。
    if fail_codes:
        print(f"[fetch] 未取到 {len(fail_codes)} 只（留待下轮补）："
              f"{fail_codes[:12]}{' ...' if len(fail_codes) > 12 else ''}")
    idx_codes = list(all_ok.keys())
    # M03 量纲修复：真实流通股本 = 流通市值÷股价（不可用换手率反推）。
    # 腾讯K线量为「手」：q=量/股本 ≈ 换手率/100 <0.01 正常；源异常返「股」→ q>0.01
    float_shares = {}
    for c, v in universe.items():
        try:
            if v.get("fmv") and v.get("price") and v["price"] > 0:
                float_shares[c] = v["fmv"] / v["price"]
        except Exception:  # noqa: BLE001
            continue
    factor, records = quality.repair_volume_units(
        [(c, all_ok[c][5]) for c in idx_codes if all_ok.get(c)],
        float_shares, rule_version=quality.RULE_VERSION)
    if factor != 1.0:
        for c in [c for c in idx_codes if all_ok.get(c)]:
            con.execute("UPDATE klines SET v=v/? WHERE code=? AND date=?",
                        (factor, ("sh" if c.startswith("6") else "sz") + c,
                         today))
        for r in records:
            r["date"] = today
        quality.log_repairs(con, records)
        print(f"[quality] 量纲失灵修复 ÷{factor}，记录 {len(records)} 条")
    # 全市场快照落库（名称/成交额/换手/流通市值——build 前置过滤的数据基础）
    con.executemany(
        "INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
        [(today, ("sh" if c.startswith("6") else "sz") + c,
          v.get("name", ""), v.get("price"), v.get("pct"), v.get("amt"),
          v.get("turn"), v.get("fmv")) for c, v in universe.items()])
    # N02 批次元数据
    rec = quality.batch_record(
        source="push2delay+ifzq/em", trade_date=today,
        caliber="qfq日K,元,股,流通市值", quality=level,
        extra={"universe": len(codes), "fetched": len(idx_codes),
               "incremental": len(inc_codes), "full": len(full_codes)})
    quality.write_batch_meta(con, rec)
    # K线缺口自愈：今日半根 / 历史空洞 → 隔离+删除+腾讯源重补
    heal = gapfill.self_heal(con, idx_codes, today)
    if heal.get("repaired"):
        print(f"[gapfill] 缺口自愈：{heal['targets']} → "
              f"{heal.get('written', {})}")
    con.commit()
    # fetch_stats.json（#605-⑦：跨日残留由消费方校验 date==今日）
    stats = {"date": today, "is_trading_day": len(idx_codes) > 0,
             "universe": len(codes), "fetched": len(idx_codes),
             "rows_written": written, "gap_heal": heal,
             "guards_health": core.guards_health()}
    path = os.path.join(core.CACHE_DIR, "fetch_stats.json")
    os.makedirs(core.CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(f"[fetch] written rows={written} stats={stats['fetched']}/{len(codes)}")
    return stats


def is_trading_day_today(con=None):
    """数据新鲜度护栏（#605-⑦）：fetch_stats 必须校验 date==今日，
    跨日残留回退启发式（今日 ∈ trade_calendar）。"""
    con = con or get_conn()
    path = os.path.join(core.CACHE_DIR, "fetch_stats.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            stats = json.load(f)
        if stats.get("date") == today_str():     # 必须校验，跨日残留不可信
            return bool(stats.get("is_trading_day"))
    # 回退启发式：今日出现在上证指数日历中
    return today_str() in trade_calendar(con)


def data_ready_for(con, date):
    """build 就绪判断（#605-⑦ 的正确语义）：
    ① date 必须是**真实交易日**（用权威节假日日历，见下）；
    ② date 当日K线已入库；
    ③ fetch_stats 不早于 date（周六抓到的周五数据 → 可复盘周五）。
    返回 (ok, reason)。

    ⚠️ 2026-09-15 循环依赖修复：原第①关写 `date not in trade_calendar(con)`
    → "非交易日"，而 `trade_calendar(con)` 完全由 `klines` 表推导，
    **当日指数K线入库前必然不含当天** ⇒ 每天在抓取完成前，闸门都把
    "今天"判成非交易日 ⇒ `close` 拒绝构建（rc=0，静默）⇒ 零推送。
    抓取超时 ⇒ 指数K线不入库 ⇒ 闸门永远拒绝，形成死锁。
    现在第①关改用独立于本地数据的权威日历。
    """
    if not core.is_real_trade_day(date):
        return False, "权威日历：非交易日（法定休市/周末）"
    # 2026-10-11 修（10-09 实战血案）：原判据只数当日K线总行数——
    # 个股抓到而**指数（sh000001）没抓到**时照样放行，结果日历不含当日、
    # 全部价格退化成前一日（10-09 收盘推送发的是 10-08 价格：科德 22.72、
    # 丽珠 -10%）。指数K线是权威日历的基座，必须单独确认入库。
    _idx = con.execute(
        "SELECT COUNT(*) FROM klines WHERE date=? AND code='sh000001'",
        (date,)).fetchone()[0]
    if _idx == 0:
        return False, (f"{date} 指数K线未入库（权威日历缺基座）——"
                       "先重新抓取，绝不以前一日价格冒充当日")
    path = os.path.join(core.CACHE_DIR, "fetch_stats.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            stats = json.load(f)
        if stats.get("date") and stats["date"] < date:
            return False, (f"fetch 数据陈旧（{stats['date']} < {date}），"
                           "先重新抓取")
    return True, "K线就绪"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="非交易日手动补数（build 侧守门不受影响）")
    ap.add_argument("--premarket", action="store_true",
                    help="盘前/竞价时段抓取：允许快照 pct 全 0"
                         "（集合竞价未开始的正常状态），不据此判休市")
    a = ap.parse_args()
    fetch_daily(days=a.days, limit=a.limit, force=a.force,
                premarket=a.premarket)
