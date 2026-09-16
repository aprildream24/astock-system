# -*- coding: utf-8 -*-
"""推送验收 + 自动补发（2026-09-16）：把"验收"从本机搬进云端。

## 为什么必须上云

原来验收/补发跑在用户本机的两条 Automation 上，判据是"本机看远端账本"。
但**用户电脑常年不开机** ⇒ 等于没有验收；而且 09-16 上午暴露了一类
**静默失败**：CI 每一步 conclusion 都是 success，但推送实际没送达
（`build_pre` 状态 `uncertain`，用户端什么都没收到）。这类问题只有
"读**远端**账本 + 按 mode 判 status"才看得出来，光看步骤结论必然漏。

## 两条用法（同一个模块）

1. **跑完即自检**（在 stock.yml 里，随每次构建执行）
       python -m pipeline.push_audit --task close
   只检查"本次任务该发的 mode 今天有没有真发出"，不发任何东西。

2. **定时验收 + 自动补发**（在 watchdog.yml 里，由 cron-job.org 触发）
       python -m pipeline.push_audit --slot am|close|review
   列出"该发但没发"的 mode → dispatch 对应 task 补发（**不传 force**：
   本来就缺，日级去重不会拦）。全部到位则静默退出，不产生任何推送。

## 安全边界（防止自己变成新的重复推送源）

- 非交易日 / 节假日：直接跳过。
- **未到点不补**：`TASK_DUE` 里记每个任务的计划时刻，提前不动作。
- **有在跑的 run 就不补**：close 冷库可能跑 50 分钟，此时再 dispatch
  只会排队空转（推送还有日级去重兜住，纯属浪费额度）。
- 同一 task 一次运行只 dispatch 一次（两个 mode 共用一个 task 时去重）。
- 显式告警优先：若今天已发过 `data_blocked_{task}` / `data_holiday`，
  说明系统**已经主动告知用户了**，不算静默失败 → 判 WARN，不补发。

## 凭据

- 读远端账本：仓库是 public ⇒ **匿名可读**（有 token 就走 token，速率更高）。
- dispatch：需要 `GH_PAT`（有 actions:write 的令牌）。
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_REPO = "aprildream24/astock-system"
LEDGER_REL = "dist/push_ledger.json"
STOCK_WF = "stock.yml"
API = "https://api.github.com"
CST = timezone(timedelta(hours=8))
UA = "astock-push-audit"

# 每个任务"应发出去的 mode"。不出现 / 非 sent = 静默失败。
TASK_EXPECT = {
    "pre": ["build_pre"],
    "auction": ["build_auction"],
    "close": ["build_close"],
    "review": ["build_review", "narrative"],
}
# 定时验收时点 → 该时点应已完成的 task
SLOT_TASKS = {
    "am": ["pre", "auction"],
    "close": ["close"],
    # ★ review 时点**连带复核 close**：15:45 那次审计很可能撞上"收盘 run 还在跑"
    # （冷库全量 ~50 分钟）而被 busy_runs 保守跳过；到 20:20 它必然已结束，
    # 此时再验一次，才算真正兜住「收盘推送丢了」这个最严重的场景。
    "review": ["close", "review"],
}
# mode → 用哪个 task 补发
MODE_TASK = {"build_pre": "pre", "build_auction": "auction",
             "build_close": "close", "build_review": "review",
             "narrative": "review"}
# 计划时刻（北京时间）。未到点绝不补发，避免与正常定时触发打架。
TASK_DUE = {"pre": "08:55", "auction": "09:30",
            "close": "15:27", "review": "20:07"}
# 显式告警类 mode 前缀：出现即代表"系统已主动告知用户"，不算静默失败。
ALERT_PREFIX = ("data_blocked_", "data_holiday")
# 判定"还有 run 在跑"的回看窗口（分钟）——close 冷库最坏 ~55 分钟。
BUSY_WINDOW_MIN = 70


# ---------------------------------------------------------------------------
# 时间 / 网络薄封装（测试全部 mock 掉）
# ---------------------------------------------------------------------------

def bj_now():
    return datetime.now(CST)


def _get_json(url, token=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json",
         "Accept-Encoding": "identity"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def remote_ledger(repo=DEFAULT_REPO, token=None):
    """读**远端**账本（权威）。失败返回 None（调用方须与"空账本"区分）。"""
    try:
        j = _get_json(f"{API}/repos/{repo}/contents/{LEDGER_REL}", token)
        return json.loads(base64.b64decode(j["content"]).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}          # 账本还不存在：视为空，不算读失败
    except Exception:          # noqa: BLE001
        pass
    # 退路：raw 直连（匿名、无速率限制烦恼）
    try:
        raw = (f"https://raw.githubusercontent.com/{repo}/main/{LEDGER_REL}")
        req = urllib.request.Request(raw, headers={"User-Agent": UA,
                                                   "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:          # noqa: BLE001
        return None


def busy_runs(repo=DEFAULT_REPO, token=None, minutes=BUSY_WINDOW_MIN):
    """近期是否还有 stock workflow 在跑/排队（有则本次不补发）。"""
    try:
        j = _get_json(f"{API}/repos/{repo}/actions/workflows/{STOCK_WF}"
                      f"/runs?per_page=20", token)
    except Exception:          # noqa: BLE001
        return []
    cut = bj_now() - timedelta(minutes=minutes)
    out = []
    for r in j.get("workflow_runs", []):
        if r.get("status") not in ("in_progress", "queued"):
            continue
        try:
            cr = datetime.fromisoformat(
                r["created_at"].replace("Z", "+00:00")).astimezone(CST)
        except Exception:      # noqa: BLE001
            continue
        if cr >= cut:
            out.append(r)
    return out


def dispatch(task, repo=DEFAULT_REPO, token=None):
    """触发 stock.yml（POST /dispatches 成功返 204 空体，只看状态码）。"""
    if not token:
        return False, "缺 GH_PAT"
    body = json.dumps({"ref": "main", "inputs": {"task": task}}).encode()
    req = urllib.request.Request(
        f"{API}/repos/{repo}/actions/workflows/{STOCK_WF}/dispatches",
        data=body, method="POST",
        headers={"Authorization": "Bearer " + token, "User-Agent": UA,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status in (200, 201, 204), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:     # noqa: BLE001
        return False, f"{type(e).__name__} {e}"


# ---------------------------------------------------------------------------
# 账本分析（纯函数，好测）
# ---------------------------------------------------------------------------

def rows_today(ledger, date):
    """今天（交易日 date）的所有账本行，按 ts 升序。"""
    out = []
    for k, v in (ledger or {}).items():
        if not isinstance(v, dict):
            continue
        if not str(v.get("ts", "")).startswith(date):
            continue
        out.append({"key": k, "mode": v.get("mode"), "ts": v.get("ts"),
                    "status": v.get("status"), "channels": v.get("channels")})
    return sorted(out, key=lambda x: str(x["ts"]))


def sent_modes(ledger, date):
    rows = rows_today(ledger, date)
    return {r["mode"] for r in rows if r["status"] == "sent"}


def alerted_modes(ledger, date):
    """今天已发过的"显式告知"类 mode（数据未就绪/休市提示）。"""
    return {r["mode"] for r in rows_today(ledger, date)
            if r["mode"] and str(r["mode"]).startswith(ALERT_PREFIX)}


def due(task, now=None):
    """该任务今天是否已过计划时刻。"""
    hm = TASK_DUE.get(task)
    if not hm:
        return True
    now = now or bj_now()
    return now.strftime("%H:%M") >= hm


def analyze(ledger, date, tasks, now=None):
    """→ {ok:[], warn:[], missing:[{task,modes}], pending:[]}

    ok      : 期望 mode 全部已 sent
    warn    : mode 缺失，但今天已发过显式告警（= 已告知用户，非静默失败）
    pending : mode 缺失且**未到点**（正常，先不动）
    missing : 真问题 —— 已过点、无告警、就是没发出去
    """
    sent = sent_modes(ledger, date)
    alerts = alerted_modes(ledger, date)
    res = {"ok": [], "warn": [], "missing": [], "pending": []}
    for t in tasks:
        exp = TASK_EXPECT.get(t) or []
        lack = [m for m in exp if m not in sent]
        if not lack:
            res["ok"].append({"task": t, "modes": exp})
            continue
        if not due(t, now):
            res["pending"].append({"task": t, "modes": lack})
            continue
        if alerts:
            res["warn"].append({"task": t, "modes": lack,
                                "alerts": sorted(alerts)})
            continue
        res["missing"].append({"task": t, "modes": lack})
    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _plan_dispatches(missing, already=None):
    """missing 项 → 去重后的 task 列表（narrative/build_review 共用 review）。"""
    seen = set(already or ())
    out = []
    for it in missing:
        for m in it["modes"]:
            t = MODE_TASK.get(m)
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="推送验收 / 自动补发")
    ap.add_argument("--task", default="", help="只验收本任务（跑完即自检）")
    ap.add_argument("--slot", default="", choices=["", "am", "close", "review"],
                    help="定时验收时点（会按需补发）")
    ap.add_argument("--date", default="", help="覆盖日期 YYYY-MM-DD")
    ap.add_argument("--repo", default=os.environ.get("GH_REPO", DEFAULT_REPO))
    ap.add_argument("--now", default="",
                    help="覆盖当前时刻 HH:MM（北京时间，排障/演练用："
                         "可提前看某时点会怎么判）")
    ap.add_argument("--dry", action="store_true", help="只报告，不 dispatch")
    a = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from pipeline import trade_calendar            # noqa: E402

    date = a.date or bj_now().strftime("%Y-%m-%d")
    tok = (os.environ.get("GH_PAT") or os.environ.get("GH_TOKEN") or "").strip()

    now = None
    if a.now:
        try:
            now = datetime.strptime(f"{date} {a.now}", "%Y-%m-%d %H:%M") \
                .replace(tzinfo=CST)
        except ValueError:
            print(f"[audit] --now 格式应为 HH:MM，收到 {a.now!r}", file=sys.stderr)
            return 2

    if not trade_calendar.is_trade_day(date):
        print(f"[audit] {date} 非交易日（{trade_calendar.why_closed(date)}），跳过")
        return 0

    tasks = ([a.task] if a.task else SLOT_TASKS.get(a.slot, []))
    if not tasks:
        print("[audit] 未指定 --task/--slot，无事可做")
        return 0

    led = remote_ledger(a.repo, tok)
    if led is None:
        # 读不到远端账本 ⇒ 不能证明"没发"，绝不据此补发（否则就是新的重复源）
        print("[audit] 远端账本不可达 —— 无法判定，跳过（不补发）")
        return 0

    res = analyze(led, date, tasks, now=now)
    print(f"[audit] date={date} tasks={','.join(tasks)} "
          f"ledger={len(led)}条 今日={len(rows_today(led, date))}条")
    for it in res["ok"]:
        print(f"[audit]   OK    {it['task']}: {'+'.join(it['modes'])} 已送达")
    for it in res["pending"]:
        print(f"[audit]   WAIT  {it['task']}: {'+'.join(it['modes'])} 未到点，暂不动")
    for it in res["warn"]:
        print(f"[audit]   WARN  {it['task']}: {'+'.join(it['modes'])} 缺失，"
              f"但已发显式告警 {it['alerts']}（非静默失败）")
    for it in res["missing"]:
        print(f"[audit]   MISS  {it['task']}: {'+'.join(it['modes'])} 未送达")

    if not res["missing"]:
        print("[audit] 全部到位")
        return 0

    if a.task:
        # 跑完即自检：只报告，不自己补自己（否则同一 run 里再触发一次，白烧额度）
        print("[audit] 本次任务存在未送达项 —— 已记录，"
              "由定时验收（watchdog）负责补发")
        return 1

    if a.dry:
        print(f"[audit] dry-run：应补发 {_plan_dispatches(res['missing'])}")
        return 0

    busy = busy_runs(a.repo, tok)
    if busy:
        print(f"[audit] 有 {len(busy)} 个 run 仍在跑/排队 —— 等它跑完，本次不补发")
        return 0

    fails = []
    for t in _plan_dispatches(res["missing"]):
        ok, detail = dispatch(t, a.repo, tok)
        print(f"[audit] 补发 dispatch task={t} → {detail}"
              + (" ✓" if ok else " ✗"))
        if not ok:
            fails.append(t)
    if fails:
        print(f"[audit] 补发失败：{fails}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
