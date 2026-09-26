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
import sys
import time

_CST = _dt.timezone(_dt.timedelta(hours=8))
CLOSE_MIN = 15 * 60           # 15:00 收盘，之后退出
MORNING_START = 9 * 60 + 28   # 09:28（竞价 09:25 结束后即可开始轮询）
STEP = 10                     # 触发间隔（分钟）


def _bj_now():
    """北京时间。runner 是 UTC，绝不能用 datetime.now() 直接判断盘中。"""
    return _dt.datetime.now(_CST)


def _minutes(now):
    return now.hour * 60 + now.minute


def _sleep_to_next_mark(now):
    """对齐到下一个 :0/:10/:20… 刻度的等待秒数（≥5s，防 0 间隔死转）。"""
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
