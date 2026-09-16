# -*- coding: utf-8 -*-
"""定时器守门（云端，2026-09-16）：验 cron-job.org 上的 astock-* 定时器活着。

## 为什么必须有它

主链的**权威触发**全部来自 cron-job.org（GitHub 自带 cron 已删）。也就是说：

    定时器没了 / 被停用  ⇒  没有任何 run  ⇒  没有任何推送  ⇒  **彻底静默**

而这类故障**恰恰是云端 watchdog 自己抓不到的**——watchdog 也是靠同一个
cron-job.org 触发的，定时器死了它自己也不会跑（"守夜人睡着了"）。

原先这层保障在本机的一条每日 21:30 Automation 里（读取 /jobs 核对 4 个
定时器 enabled）。但**用户电脑常年不开机** ⇒ 这层保障等于不存在。
把它搬到云端，才算真正闭环。

## 判据（宁可漏报，不可误报——误报会变成新的打扰源）

- 未配置 `CRONJOB_API_KEY` → 跳过（不改行为）。
- 网络不可达 → 只打印，**不告警**（网络抖动不该半夜吵醒人）。
- HTTP 401 / 403（key 失效）→ **告警**（确定性故障）。
- 应存在的 `astock-*` 定时器缺失 / `enabled != true` → **告警**（确定性故障）。
- 其余情况 → 静默（只写运行日志）。

用法：python -m pipeline.timer_guard [--dry]
环境变量：CRONJOB_API_KEY（cron-job.org 的 API key）
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CRONJOB_API = "https://api.cron-job.org/jobs"
UA = "astock-timer-guard"
CST = timezone(timedelta(hours=8))

# 本项目在 cron-job.org 上的定时器 title（唯一权威触发源）。
# ⚠️ setup 脚本按 **title** 匹配做"先删旧再建新" ⇒ 这些名字不可随意改。
REQUIRED = (
    "astock-pre",           # 08:50 盘前计划
    "astock-auction",       # 09:25 竞价裁决
    "astock-close",         # 15:22 收盘构建 + Pages
    "astock-review",        # 20:02 复盘 + AI 叙事
    "astock-intraday-am",   # 09:45 盘中校验（早盘）
    "astock-intraday-pm",   # 14:40 盘中校验（尾盘机会）
    "astock-audit-am",      # 10:00 云端验收（盘前+竞价）
    "astock-audit-close",   # 15:45 云端验收（收盘）
    "astock-audit-review",  # 20:20 云端验收（连带复核收盘）
)
# 另一套项目（fisk9r/stock-analysis）也在同一个账号下，前缀不同，
# 绝不能被本守门判定为"多余"或"缺失"。
OTHER_PREFIX = ("stock-", "exec-", "返利", "Buddy")


def fetch_jobs(key, timeout=25):
    """GET /jobs → 任务列表。返回 (jobs, err)：err 为 None 表示成功。"""
    req = urllib.request.Request(
        CRONJOB_API, headers={"Authorization": "Bearer " + key,
                              "User-Agent": UA,
                              "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("jobs", []), None
    except urllib.error.HTTPError as e:
        return [], f"HTTP {e.code}"
    except Exception as e:                      # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"


def audit_jobs(jobs):
    """→ {"missing": [...], "disabled": [...], "titles": [...], "other": n}

    `other` = 不属于本项目、也不属于已知另一套前缀的任务数（仅作提示）。
    """
    by_title = {}
    for j in jobs or []:
        t = (j.get("title") or "").strip()
        if t:
            by_title[t] = j
    missing = [t for t in REQUIRED if t not in by_title]
    disabled = [t for t in REQUIRED
                if t in by_title and by_title[t].get("enabled") is not True]
    titles = sorted(by_title)
    other = sum(1 for t in titles
                if not t.startswith("astock-")
                and not t.startswith(OTHER_PREFIX))
    return {"missing": missing, "disabled": disabled,
            "titles": titles, "other": other}


def main(argv=None):
    ap = argparse.ArgumentParser(description="cron-job.org astock 定时器守门")
    ap.add_argument("--dry", action="store_true", help="只报告，不推送告警")
    a = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    key = (os.environ.get("CRONJOB_API_KEY") or "").strip()
    if not key:
        print("[timers] 未配置 CRONJOB_API_KEY，跳过（不改行为）")
        return 0

    jobs, err = fetch_jobs(key)
    if err and not err.startswith("HTTP 4"):
        # 网络抖动 ≠ 定时器故障：不告警，避免半夜误吵。
        print(f"[timers] cron-job.org 不可达（{err}）—— 不告警")
        return 0
    if err:
        # key 失效（401/403）是确定性故障：守门自己瞎了，必须让人知道。
        return _alert(f"cron-job.org 鉴权失败（{err}）—— 定时器守门已失效，"
                      f"主链处于无人看守状态。请更新 CRONJOB_API_KEY。", a.dry)

    r = audit_jobs(jobs)
    print(f"[timers] cron-job.org 共 {len(r['titles'])} 个任务"
          f"（其中 {r['other']} 个非本项目）")
    bad = []
    if r["missing"]:
        bad.append(f"缺失定时器：{', '.join(r['missing'])}")
    if r["disabled"]:
        bad.append(f"被停用的定时器：{', '.join(r['disabled'])}")
    if not bad:
        print(f"[timers] 9 个 astock-* 定时器全部 enabled ✓")
        return 0
    for b in bad:
        print("[timers] ✗ " + b)
    return _alert("定时器守门告警 · " + datetime.now(CST).strftime("%m-%d %H:%M")
                  + "\n\n" + "\n".join("- " + b for b in bad)
                  + "\n\n后果：对应时点**不会有任何 run**，也就不会有推送。",
                  a.dry)


def _alert(md, dry):
    print("[timers] 需要告警：" + md.splitlines()[0])
    if dry:
        print("[timers] dry-run：不推送")
        return 0
    try:
        from pipeline import notifier
        r = notifier.push("watchdog_alert", "定时器守门告警",
                          notifier.md2html(md), force=True)
        print("[timers] 告警推送:", r.get("status", r))
    except Exception as e:                      # noqa: BLE001
        print(f"[timers] 告警推送失败（忽略）：{type(e).__name__} {e}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
