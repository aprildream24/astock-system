# -*- coding: utf-8 -*-
"""全市场覆盖率体检（只读盘点，不推不写）。

用法：python tools/coverage_audit.py

回答三个问题：
  1) 最新交易日是哪天、全市场快照有多少只、其中实盘可买的有多少只；
  2) 可买标的中几只拿到了当日K线 → 真正的扫描覆盖率；
  3) 剩下的都是些什么（退市老代码 / 未上市新股 / 停牌）——它们不是数据缺口。

历史教训：覆盖率一度长期显示 93% 并反复告警"请补齐"，实际是把 340 只
已退市老代码和 4 只未上市新股算进了分母，真实缺口为 0（见 build.split_universe）。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.core import DB_PATH  # noqa: E402
from pipeline import mktfilter  # noqa: E402


def main():
    con = sqlite3.connect(DB_PATH)
    latest_kl = con.execute("SELECT MAX(date) FROM klines").fetchone()[0]
    latest_snap = con.execute("SELECT MAX(date) FROM snapshot").fetchone()[0]
    snap = {r[0]: r for r in con.execute(
        "SELECT code, name, amt FROM snapshot WHERE date=?",
        (latest_snap,)).fetchall()}
    have_bar = {r[0] for r in con.execute(
        "SELECT DISTINCT code FROM klines WHERE date=? AND code!='sh000001'",
        (latest_kl,)).fetchall()}
    last_bar = dict(con.execute(
        "SELECT code, MAX(date) FROM klines WHERE code!='sh000001' "
        "GROUP BY code").fetchall())

    tradable = [c for c in snap if mktfilter.tradable(c[2:])]
    alive, dead = [], []
    for c in tradable:
        amt = snap[c][2]
        last = last_bar.get(c)
        if (not amt or amt <= 0) and (last is None or last < latest_kl):
            dead.append(c)
        else:
            alive.append(c)
    missing = [c for c in alive if c not in have_bar]
    listed = [c for c in dead if last_bar.get(c)]
    unlisted = [c for c in dead if not last_bar.get(c)]

    print(f"最新K线交易日 : {latest_kl}   快照日期: {latest_snap}")
    print(f"快照全市场    : {len(snap)} 只   实盘可买阈内: {len(tradable)} 只")
    print(f"有效标的(分母): {len(alive)} 只   当日K线齐全: "
          f"{len(alive) - len(missing)} 只")
    print(f"扫描覆盖率    : "
          f"{(len(alive) - len(missing)) / max(1, len(alive)) * 100:.2f}%")
    print(f"不可交易(剔除): {len(dead)} 只 "
          f"（已退市/停牌 {len(listed)} · 未上市 {len(unlisted)}）")
    if missing:
        print(f"[!] 真实数据缺口 {len(missing)} 只："
              f"{[ (c, snap[c][1]) for c in missing[:10] ]}")
        print("    → 跑 tools/fetch_all.py 补齐")
    else:
        print("[OK] 有效标的无数据缺口")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
