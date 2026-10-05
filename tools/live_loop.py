# -*- coding: utf-8 -*-
"""盘中买点巡检·长驻循环（2026-09-26 新增，替代触发器）。

背景：盘中买点告警需要「每 10 分钟」的触发源，权威来源是 cron-job.org 的
astock-intraday-live 定时器（tools/timer_live.py 负责创建）。但仓库 Secret
CRONJOB_API_KEY 已实证无效（GET /jobs 404，同样卡住旧定时器清理），
在用户提供有效 key 之前，改用 **GH Actions 单 job 长驻循环**顶班：

    auction(09:25) 主链末尾 dispatch 本 workflow → 一个 runner 常驻整个
    交易时段 → 每 10 分钟（对齐 :00/:10/… 刻度）跑一轮：
      · pipeline.intraday.run(slot="live")  —— 事件级去重推送（核心）
      · pipeline.executor.run(task="auto", slot="live") —— 模拟盘同步建仓
    收盘（15:00 后）退出，由 workflow 的 cache/save 把状态交还主链。

为什么安全：
  · 推送去重是**事件级**（live_alerts 账本）——即使将来 cron-job 定时器与
    本循环并存，同一事件也只会推一次；
  · 非交易日 / 非交易时段由 trade_calendar + intraday.in_window 守门，
    空转轮次不抓数据不推送；
  · 与 am/pm 摘要（09:45/14:40，日熔丝一天一条）语义互不影响；
  · 单轮异常不退出循环（下一刻度重试），runner 崩溃的最坏退化 =
    回到每天 2 条盘中摘要（而非彻底静默）。

时长账：09:28 起跑到 15:00 ≈ 5.5 小时 < GH 单 job 上限 6 小时；
08:50 前误触发会先睡到 09:28（若早于 08:50 dispatch 则超过 job 上限，
本模块在开头打印警告——auction 顺链触发保证不会发生）。

用法：python -m tools.live_loop         # 在 workflow 内跑整个时段
      python -m tools.live_loop --once  # 只跑一轮（本地/冒烟测试）
"""
import argparse
import datetime as _dt
import os
import sys
import time

_CST = _dt.timezone(_dt.timedelta(hours=8))
CLOSE_MIN = 15 * 60           # 15:00 收盘，之后退出
MORNING_START = 9 * 60 + 28   # 09:28（竞价 09:25 结束后即可开始轮询）
STEP = 1                      # 触发间隔（分钟）——2026-10-05 用户需求
                              # 「所有到买点的票第一时间提醒」：
                              # 10 分钟对齐刻度改为**每分钟**一查，
                              # 端到端最坏延迟 ~11 分钟 → ~1-2 分钟
                              # （免费行情源只有 HTTP 轮询，1 分钟已贴物理上限）


def _bj_now():
    """北京时间。runner 是 UTC，绝不能用 datetime.now() 直接判断盘中。"""
    return _dt.datetime.now(_CST)


def _minutes(now):
    return now.hour * 60 + now.minute


def _sleep_to_next_mark(now):
    """对齐到下一分钟刻度（STEP=1 → 每 :00 一轮；≥5s 防 0 间隔死转）。"""
    m = _minutes(now)
    nxt = (m // STEP + 1) * STEP
    delta = (nxt - m) * 60 - now.second - now.microsecond / 1e6
    return max(delta, 5.0)


def _one_cycle(date, now):
    """跑一轮：盘中巡检推送（事件级去重）+ 模拟盘建仓。单轮异常不上抛。"""
    from pipeline import intraday
    if not intraday.in_window("live", now):
        return False
    try:
        intraday.run(slot="live", date=date)
    except Exception as e:                      # noqa: BLE001
        print(f"[live-loop] 巡检异常（下一刻度重试）: "
              f"{type(e).__name__} {e}", flush=True)
    try:
        from pipeline import executor as sim
        sim.run(task="auto", slot="live")
    except Exception as e:                      # noqa: BLE001
        print(f"[live-loop] 模拟盘异常（下一刻度重试）: "
              f"{type(e).__name__} {e}", flush=True)
    return True


def _already_running(token, repo, my_run_id):
    """今天（北京日）是否已有**别的** live 循环实例在跑。

    触发路径有三条（auction 顺链 / intraday am 顺链备份 / 手动 dispatch），
    并发两份循环会双份抓快照、双份 cache 存档互相覆盖。GH API 查本
    workflow 当日非失败 run 即可判重；查不到（无 token/网络抖动）放行
    ——宁可信其无：事件级推送去重兜底，最坏是多抓几份快照。"""
    if not token:
        return False
    import json
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/actions/workflows/"
            f"intraday-live.yml/runs?per_page=10",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "astra-live-loop"})
        runs = json.loads(urllib.request.urlopen(req, timeout=25).read()) \
            .get("workflow_runs", [])
        today = _bj_now().strftime("%Y-%m-%d")
        for r in runs:
            if r.get("id") == my_run_id:
                continue
            created = _dt.datetime.fromisoformat(
                r["created_at"].replace("Z", "+00:00")).astimezone(_CST)
            if created.strftime("%Y-%m-%d") != today:
                continue
            # 09-30 二次修：只挡**真正在跑**的并发实例（queued/in_progress）。
            # 原判定把"当日已 success"也算占用——结果一次 30 秒的守门退出
            # 自身就是 success，把当天后续所有派发全部挡死（实测踩坑）。
            # 正常跑完（15:00 success）之后新触发的实例会因收盘闸自然退出，
            # 无需守门代劳。
            if r.get("status") in ("queued", "in_progress"):
                return True
    except Exception as e:                      # noqa: BLE001
        print(f"[live-loop] 并发检查失败（放行）: {type(e).__name__} {e}",
              flush=True)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="盘中买点巡检长循环")
    ap.add_argument("--once", action="store_true", help="只跑一轮（测试用）")
    a = ap.parse_args(argv)

    from pipeline import trade_calendar
    now = _bj_now()
    date = now.strftime("%Y-%m-%d")
    if not trade_calendar.is_trade_day(date):
        print(f"[live-loop] {date} 非交易日"
              f"（{trade_calendar.why_closed(date)}）→ 退出")
        return 0
    _tok = (os.environ.get("GH_PAT") or "").strip()
    _repo = os.environ.get("GH_REPO", "aprildream24/astock-system")
    _rid = os.environ.get("RUN_ID", "")
    if _tok and _rid and _already_running(_tok, _repo, int(_rid)):
        print("[live-loop] 今日已有实例在跑/已成功 → 本次触发退出（并发守门）")
        return 0
    if _minutes(now) < MORNING_START:
        wait = (MORNING_START - _minutes(now)) * 60 - now.second
        if wait > (MORNING_START - 8 * 60) * 60:      # 早于 08:50 dispatch
            print(f"[live-loop] ⚠ 开盘前 {wait/3600:.1f} 小时即被触发，"
                  f"将超过 GH 单 job 6h 上限——请改由 auction 主链顺链触发")
        print(f"[live-loop] 开盘前，休眠 {wait/60:.0f} 分钟至 09:28", flush=True)
        time.sleep(max(wait, 0))
        now = _bj_now()
    print(f"[live-loop] 开始巡检循环（间隔 {STEP} 分钟，"
          f"收盘 {CLOSE_MIN // 60:02d}:00 退出）", flush=True)
    while True:
        now = _bj_now()
        if now.strftime("%Y-%m-%d") != date or _minutes(now) >= CLOSE_MIN:
            print(f"[live-loop] {now:%H:%M} 收盘/跨日 → 退出", flush=True)
            return 0
        _one_cycle(date, now)
        if a.once:
            return 0
        time.sleep(_sleep_to_next_mark(now))


if __name__ == "__main__":
    raise SystemExit(main())
