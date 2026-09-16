# -*- coding: utf-8 -*-
"""基础设施守门（云端，2026-09-16）：守两件事——① 定时器活着 ② 主链没卡死。

## 为什么必须有它

主链的**权威触发**全部来自 cron-job.org（GitHub 自带 cron 已删）。也就是说：

    定时器没了 / 被停用  ⇒  没有任何 run  ⇒  没有任何推送  ⇒  **彻底静默**

而这类故障**恰恰是云端 watchdog 自己抓不到的**——watchdog 也是靠同一个
cron-job.org 触发的，定时器死了它自己也不会跑（"守夜人睡着了"）。

原先这层保障在本机的一条每日 21:30 Automation 里（读取 /jobs 核对 enabled）。
但**用户电脑常年不开机** ⇒ 这层保障等于不存在。搬到云端才算闭环。

## 第二件事：主链卡死（2026-09-16 一天内实测发生两次）

「代码/测试坏了 ⇒ `回归自检` 失败 ⇒ 构建与推送整步 skipped ⇒ 全天零推送」。
这个故障有个恶劣性质：**自动补发救不了它**（补发用的是同一份坏代码，
第二次照样挂在回归自检），会变成"每轮审计都失败了，但没有任何人知道"。

实测两例（同一处潜伏 bug）：site run 35067193010、收盘 run 35068160760
都挂在 `test_intraday_scope.py` 的 2 条断言上。故本守门要能**明确报出
"卡在回归自检、补发无用、需修代码"**，而不是让大家等着补发。

## 判据（宁可漏报，不可误报——误报会变成新的打扰源）

- 非交易日 → 跳过。
- 未配置 `CRONJOB_API_KEY` → 定时器部分跳过（不改行为）。
- 网络不可达 → 只打印，**不告警**（网络抖动不该半夜吵醒人）。
- HTTP 401 / 403（key 失效）→ **告警**（确定性故障）。
- 应存在的 `astock-*` 定时器缺失 / `enabled != true` → **告警**。
- 最近 `CHAIN_LOOK` 个 stock run **全部 failure** → **告警**（并指出失败步骤）。

用法：python -m pipeline.timer_guard [--dry]
环境变量：CRONJOB_API_KEY（cron-job.org API key）、GH_PAT（可选，提高 API 限额）
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CRONJOB_API = "https://api.cron-job.org/jobs"
GH_API = "https://api.github.com"
UA = "astock-timer-guard"
CST = timezone(timedelta(hours=8))
DEFAULT_REPO = "aprildream24/astock-system"
STOCK_WF = "stock.yml"
# 连续多少个 stock run 全失败就判"链路卡死"。取 3：单次失败可能只是
# 一次性的源抖动（当天稍后就会成功），连续 3 次才说明是代码/配置级问题。
CHAIN_LOOK = 3

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
    """→ {"missing": [...], "disabled": [...], "titles": [...],
         "other": n, "mine": n, "known_other": n}

    - `mine`         = 本项目 `astock-*` 定时器数量（含被停用的）
    - `known_other`  = 已知另一套项目前缀的任务数（正常共存，不算异常）
    - `other`        = 既不属于本项目、也不匹配任何已知前缀 ⇒ **无法归类**，
                       仅作提示（可能是我方新加/改名，也可能是别人的新任务）
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
    mine = sum(1 for t in titles if t.startswith("astock-"))
    known_other = sum(1 for t in titles
                      if not t.startswith("astock-")
                      and t.startswith(OTHER_PREFIX))
    other = sum(1 for t in titles
                if not t.startswith("astock-")
                and not t.startswith(OTHER_PREFIX))
    return {"missing": missing, "disabled": disabled,
            "titles": titles, "other": other,
            "mine": mine, "known_other": known_other}


def _get_json(url, token=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json",
         "Accept-Encoding": "identity"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def failed_steps(repo, run_id, token=None):
    """该 run 里 conclusion == failure 的步骤名列表（用于指出"卡在哪"）。"""
    try:
        jobs = _get_json(f"{GH_API}/repos/{repo}/actions/runs/{run_id}/jobs",
                         token).get("jobs", [])
    except Exception:                           # noqa: BLE001
        return []
    out = []
    for jb in jobs:
        for s in jb.get("steps", []) or []:
            if s.get("conclusion") == "failure":
                out.append(s.get("name") or "?")
    return out


def chain_status(repo=DEFAULT_REPO, token=None, look=CHAIN_LOOK):
    """最近 look 个已完成的 stock run 是否**全部失败**。

    → (state, detail)，state ∈ {"ok", "blocked", "unknown"}
    - unknown：接口不可达 / 没有已完成的 run（绝不据此告警）
    - blocked：连续 look 次失败；detail 里点明失败步骤，便于区分
      「回归自检挂了（代码坏，补发无用）」与「抓取/构建挂了（可重试）」
    """
    try:
        runs = _get_json(f"{GH_API}/repos/{repo}/actions/workflows/{STOCK_WF}"
                         f"/runs?per_page={max(look * 2, 6)}", token
                         ).get("workflow_runs", [])
    except Exception:                           # noqa: BLE001
        return "unknown", "Actions API 不可达"
    done = [r for r in runs if r.get("status") == "completed"]
    if len(done) < look:
        return "unknown", f"已完成 run 不足 {look} 个"
    recent = done[:look]
    if not all(r.get("conclusion") == "failure" for r in recent):
        return "ok", ""
    steps = failed_steps(repo, recent[0]["id"], token)
    if any("回归自检" in s for s in steps):
        return "blocked", (
            f"最近 {look} 个 run 全部失败，且都卡在「回归自检」"
            f"（{recent[0]['id']}）⇒ **补发救不了**（同一份坏代码照样挂），"
            f"需修复 HEAD 后重新推送。")
    return "blocked", (f"最近 {look} 个 run 全部失败，失败步骤："
                      f"{steps or '未知'}（{recent[0]['id']}）")


def main(argv=None):
    ap = argparse.ArgumentParser(description="cron-job.org 定时器 + 主链守门")
    ap.add_argument("--dry", action="store_true", help="只报告，不推送告警")
    a = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from pipeline import trade_calendar            # noqa: E402

    date = datetime.now(CST).strftime("%Y-%m-%d")
    if not trade_calendar.is_trade_day(date):
        print(f"[guard] {date} 非交易日（{trade_calendar.why_closed(date)}），跳过")
        return 0

    problems = []
    _check_timers(problems)
    _check_chain(problems)
    if not problems:
        print("[guard] 定时器与主链均正常")
        return 0
    for b in problems:
        print("[guard] ✗ " + b)
    return _alert("守门告警 · " + datetime.now(CST).strftime("%m-%d %H:%M")
                  + "\n\n" + "\n".join("- " + b for b in problems), a.dry)


def _check_timers(problems):
    key = (os.environ.get("CRONJOB_API_KEY") or "").strip()
    if not key:
        print("[guard] 未配置 CRONJOB_API_KEY，跳过定时器检查（不改行为）")
        return
    jobs, err = fetch_jobs(key)
    if err and not err.startswith("HTTP 4"):
        # 网络抖动 ≠ 定时器故障：不告警，避免半夜误吵。
        print(f"[guard] cron-job.org 不可达（{err}）—— 不告警")
        return
    if err:
        problems.append(f"cron-job.org 鉴权失败（{err}）—— 定时器守门已失效，"
                        f"主链处于无人看守状态。请更新 CRONJOB_API_KEY。")
        return
    r = audit_jobs(jobs)
    print(f"[guard] cron-job.org 共 {len(r['titles'])} 个任务"
          f"（本项目 {r['mine']} · 已知另一套 {r['known_other']}"
          f" · 未归类 {r['other']}）")
    if r["missing"]:
        problems.append(f"缺失定时器：{', '.join(r['missing'])}")
    if r["disabled"]:
        problems.append(f"被停用的定时器：{', '.join(r['disabled'])}")
    if not r["missing"] and not r["disabled"]:
        print(f"[guard] {len(REQUIRED)} 个 astock-* 定时器全部 enabled ✓")


def _check_chain(problems):
    tok = (os.environ.get("GH_PAT") or "").strip()
    repo = os.environ.get("GH_REPO", DEFAULT_REPO)
    state, detail = chain_status(repo, tok)
    if state == "ok":
        print("[guard] 主链近期有成功 run ✓")
    elif state == "unknown":
        print(f"[guard] 主链状态未知（{detail}）—— 不告警")
    else:
        problems.append(detail)
    return state


def _alert(md, dry):
    print("[guard] 需要告警：" + md.splitlines()[0])
    if dry:
        print("[guard] dry-run：不推送")
        return 0
    try:
        from pipeline import notifier
        r = notifier.push("watchdog_alert", "守门告警",
                          notifier.md2html(md), force=True)
        print("[guard] 告警推送:", r.get("status", r))
    except Exception as e:                      # noqa: BLE001
        print(f"[guard] 告警推送失败（忽略）：{type(e).__name__} {e}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
