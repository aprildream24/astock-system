# -*- coding: utf-8 -*-
"""调度守护（2026-09-14 重构：主链已上 CI，改为查 GitHub Actions 公开 API）。

架构变更背景：fetch/build/推送的主战场在 GitHub Actions（stock workflow），
本地文件（cache/fetch_stats.json、push_ledger）不再反映主链状态——
旧版看本地文件必然天天误报"fetch_daily 从未跑过 / build 未跑"。

新逻辑（无需 token——仓库 public，actions runs 接口匿名可读）：
  ① 今日 stock workflow 有 success run → 一切正常
  ② 有 in_progress/queued → 尚在跑，不告警（GitHub cron 有延迟史）
  ③ 今日全 failure / 零 run → 告警（真出事了）
本地 cache 检查降级为附加信息（本地兜底链路的自检），不再决定告警。

用法：python tools/watchdog.py   （建议 16:00 与 21:00 各跑一次）
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import trade_calendar  # noqa: E402
from pipeline.core import today_str  # noqa: E402

API_RUNS = ("https://api.github.com/repos/aprildream24/astock-system"
            "/actions/workflows/stock.yml/runs?per_page=20")
CST = timezone(timedelta(hours=8))


def _today_ci_runs():
    """拉今日（北京时间）的 stock workflow runs。返回 [] 表示接口不可达。"""
    try:
        req = urllib.request.Request(
            API_RUNS, headers={"User-Agent": "astock-watchdog",
                               "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            runs = json.load(r).get("workflow_runs", [])
    except Exception:  # noqa: BLE001 — 网络问题不误报
        return None
    today = datetime.now(CST).strftime("%Y-%m-%d")
    out = []
    for it in runs:
        created = datetime.fromisoformat(
            it["created_at"].replace("Z", "+00:00")).astimezone(CST)
        if created.strftime("%Y-%m-%d") == today:
            out.append(it)
    return out


def main():
    today = today_str()
    if not trade_calendar.is_trade_day(today):
        print(f"[watchdog] {today} 非交易日（{trade_calendar.why_closed(today)}），跳过")
        return 0
    # 判断运行模式：本地链路今天有产出（fetch_stats==today）→ 离线自检模式；
    # 否则查 CI 主链（公开 API）。
    stats_path = os.path.join(ROOT, "cache", "fetch_stats.json")
    local_fresh = False
    if os.path.exists(stats_path):
        try:
            with open(stats_path, encoding="utf-8") as f:
                stats = json.load(f)
            local_fresh = (stats.get("date") == today and stats.get("fetched"))
        except Exception:  # noqa: BLE001
            pass
    problems = []
    if local_fresh:
        # 离线模式：本地链路今天已跑 → 校验推送产出
        print(f"[watchdog] {today} 本地链路模式（fetch 今日已跑）")
        try:
            ledger = os.path.join(ROOT, "dist", "push_ledger.json")
            today_push = False
            if os.path.exists(ledger):
                with open(ledger, encoding="utf-8") as f:
                    for k, v in (json.load(f) or {}).items():
                        if str(v.get("ts", "")).startswith(today) \
                                and v.get("status") in ("sent", "uncertain"):
                            today_push = True
                            break
            if not today_push:
                problems.append("本地 fetch 已跑但今日无成功推送（build 挂了？）")
        except Exception as e:  # noqa: BLE001
            problems.append(f"账本不可读：{e!r}")
    else:
        # CI 模式：查 GitHub Actions
        runs = _today_ci_runs()
        if runs is None:
            print("[watchdog] GitHub API 不可达且本地无今日数据——"
                  "跳过检查（不误报）")
            return 0
        ok = [r for r in runs if r.get("conclusion") == "success"]
        running = [r for r in runs
                   if r.get("status") in ("in_progress", "queued")]
        if ok:
            print(f"[watchdog] {today} CI 主链正常（{len(ok)} 个成功 run）")
            return 0
        if running:
            print(f"[watchdog] {today} CI 仍在跑（{len(running)} 个），暂不告警")
            return 0
        if runs:
            problems.append(f"今日 CI {len(runs)} 个 run 全部失败（回归/构建挂了）")
        else:
            problems.append("今日 CI 零 run 且本地链路无产出（双通道全哑？）")
        problems.append(f"（本地 fetch_stats 停在 "
                        f"{_stats_date(stats_path) or '缺失'}）")
    if problems:
        print("[watchdog] 发现异常：")
        for p in problems:
            print("  -", p)
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


def _stats_date(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("date")
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    sys.exit(main())
