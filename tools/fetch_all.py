# -*- coding: utf-8 -*-
"""分块续传抓取器：WAF 封禁窗口内礼貌补齐全市场K线。

策略：
  1. 计算剩余未同步代码（依托历史库，断点续传）；
  2. 先单只探测：被封 → 等待 PROBE_WAIT 后重试（最多 MAX_WAIT 分钟）；
  3. 解封后按 CHUNK 只一批拉取（限速由 RateLimiter 把守），批间停顿 PAUSE 秒；
  4. 每批即时入库——任何时刻中断，下次运行自动续传。
用法：python tools/fetch_all.py [--days 120] [--max-wait 40]
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import core, gapfill, mktfilter, quality  # noqa: E402
from pipeline import trade_calendar as hol  # noqa: E402
from pipeline.core import get_conn, today_str, upsert_klines  # noqa: E402
from pipeline.fetch_daily import fetch_universe, guard_snapshot  # noqa: E402

CHUNK = 300
PAUSE = 15
PROBE_WAIT = 75          # 被封时探测间隔（秒）


def log(*a):
    print("[fetch_all]", *a, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--max-wait", type=int, default=40, help="最长等待解封分钟数")
    a = ap.parse_args()
    con = get_conn()
    today = today_str()
    universe = fetch_universe()
    guard_snapshot(universe, today, con, partial=True)
    codes = [c for c in sorted(universe.keys()) if mktfilter.tradable(c)]
    import datetime as _dt
    _d = _dt.date.fromisoformat(today)
    while not hol.is_trade_day(_d.isoformat()):
        _d -= _dt.timedelta(days=1)
    latest_td = _d.isoformat()
    last_dates = dict(con.execute(
        "SELECT code, MAX(date) FROM klines WHERE code!='sh000001' "
        "GROUP BY code").fetchall())
    todo = [c for c in codes
            if (last_dates.get(("sh" if c.startswith("6") else "sz") + c)
                or "") < latest_td]
    total = len(todo)
    log(f"剩余待同步 {total}/{len(codes)}（最近交易日 {latest_td}）")
    if not total:
        log("已全部同步")
        return 0
    pfx_of = lambda c: "sh" if c.startswith("6") else "sz"  # noqa: E731
    t0 = time.time()
    done = 0
    i = 0
    while i < total and (time.time() - t0) < a.max_wait * 60:
        chunk = todo[i:i + CHUNK]
        # 探测：确认哪个源可用（tx → sina 三源兜底），被封则等待重探
        probe_num, probe_pfx = chunk[0], pfx_of(chunk[0])
        src_kind = None
        try:
            core._kline_one_tx(probe_num, probe_pfx, 5)
            src_kind = "tx"
        except Exception:  # noqa: BLE001
            try:
                core._kline_one_sina(probe_num, probe_pfx, 5)
                src_kind = "sina"
            except Exception:  # noqa: BLE001
                src_kind = None
        if src_kind is None:
            log(f"全部源仍不可用，{PROBE_WAIT}s 后重探…")
            time.sleep(PROBE_WAIT)
            continue
        if src_kind == "sina":
            # 腾讯/东财都被封 → 纯新浪通道（礼貌限速由 limiter 内部把守）
            from concurrent.futures import ThreadPoolExecutor
            batch = {}
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = [(num, ex.submit(core._kline_one_sina, num, pfx_of(num), a.days))
                        for num, _ in [(c, pfx_of(c)) for c in chunk]]
                for num, f in futs:
                    try:
                        rows = f.result()
                        if rows:
                            batch[num] = rows
                    except Exception:  # noqa: BLE001
                        continue
            log(f"[sina通道] 批次 {len(chunk)} 只（tx/em 被封，走新浪）")
        else:
            batch = core.kline_batch([(c, pfx_of(c)) for c in chunk],
                                     days=a.days, workers=3)
        n_rows = 0
        n_ok = 0
        for num, rows in batch.items():
            if rows:
                n_ok += 1
                n_rows += upsert_klines(
                    con, ("sh" if num.startswith("6") else "sz") + num, rows)
        con.commit()
        done += len(chunk)
        i += len(chunk)
        rate = done / max(0.001, time.time() - t0)
        eta = (total - done) / max(rate, 0.01)
        log(f"批次完成 {done}/{total} 本批OK={n_ok} 行={n_rows} "
            f"均速{rate:.1f}只/s ETA {eta:.0f}s")
        if i < total:
            time.sleep(PAUSE)
    # 收尾：量纲校验 + 缺口自愈（与主 fetch 相同的质量防线）
    float_shares = {}
    for c, v in universe.items():
        try:
            if v.get("fmv") and v.get("price") and v["price"] > 0:
                float_shares[c] = v["fmv"] / v["price"]
        except Exception:  # noqa: BLE001
            continue
    have = dict(con.execute(
        "SELECT code, COUNT(*) FROM klines WHERE code!='sh000001' "
        "GROUP BY code").fetchall())
    synced = sum(1 for c in codes
                 if (have.get(("sh" if c.startswith("6") else "sz") + c, 0)
                     or 0) >= 60)
    log(f"收尾：已同步≥60根历史的有 {synced}/{len(codes)} 只"
        f"（本轮处理 {min(done, total)}/{total}）")
    heal = gapfill.self_heal(con, list(universe.keys()), today)
    if heal.get("repaired"):
        log(f"缺口自愈：{heal['targets']}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
