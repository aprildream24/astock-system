# -*- coding: utf-8 -*-
"""调度守护（吸收自原项目 watchdog 思想）：检测「今天该跑的任务没跑」。

检查项：
  ① fetch_stats.json：date==今日 且 fetched>0（收盘抓取是否完成）
  ② push_ledger / dist 账本：今日是否产出过主链推送（build 是否跑过）
  ③ 情绪日志：今日 emotion_log 是否有行
异常 → 经 PushPlus 告警（force 绕过去重，缺跑告警每天最多一次本身由
fetch_stats 的日期键自然去重）。纯本地、零网络（除告警）。

用法：python tools/watchdog.py   （建议 16:00 与 21:00 各跑一次）
"""
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import trade_calendar  # noqa: E402
from pipeline.core import get_conn, today_str  # noqa: E402


def main():
    today = today_str()
    if not trade_calendar.is_trade_day(today):
        print(f"[watchdog] {today} 非交易日（{trade_calendar.why_closed(today)}），跳过")
        return 0
    problems = []
    # ① 收盘抓取
    stats_path = os.path.join(ROOT, "cache", "fetch_stats.json")
    if not os.path.exists(stats_path):
        problems.append("fetch_stats.json 不存在（fetch_daily 从未跑过）")
    else:
        with open(stats_path, encoding="utf-8") as f:
            stats = json.load(f)
        if stats.get("date") != today:
            problems.append(f"fetch 数据陈旧（{stats.get('date')}，非今日）")
        elif not stats.get("fetched"):
            problems.append("fetch 今日抓取 0 只（接口全挂？）")
    # ② 主链推送
    con = get_conn()
    n_push = con.execute(
        "SELECT COUNT(*) FROM push_ledger WHERE ts LIKE ?", (today + "%",)).fetchone()[0]
    if n_push == 0:
        problems.append("今日 0 条主链推送（build 未跑或全部失败）")
    # ③ 情绪日志
    try:
        n_emo = con.execute(
            "SELECT COUNT(*) FROM emotion_log WHERE date=?", (today,)).fetchone()[0]
        if n_emo == 0 and n_push:
            problems.append("今日已有推送但情绪日志缺失（版本不同步？）")
    except Exception:  # noqa: BLE001
        pass
    con.close()
    if problems:
        print("[watchdog] 发现异常：")
        for p in problems:
            print("  -", p)
        # 告警推送（force：缺跑告警不能被去重吃掉）
        try:
            from pipeline import notifier
            md = "# 调度守护告警 " + today + "\n" + "\n".join(
                "- " + p for p in problems)
            r = notifier.push("watchdog", today,
                              notifier.md2html(md), date=today, force=True)
            print("[watchdog] 告警推送:", r.get("status", r))
        except Exception as e:  # noqa: BLE001
            print("[watchdog] 告警推送失败:", e)
        return 1
    print(f"[watchdog] {today} 全部正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
