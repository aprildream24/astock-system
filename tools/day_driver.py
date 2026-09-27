# -*- coding: utf-8 -*-
"""纯 GitHub 触发的「日驱动」调度器（2026-09-27 新增）。

## 为什么需要它

主链（stock.yml）的权威触发原先在 cron-job.org（GH 自带 schedule 延迟
1-2h 不可控，2026-09-14 实测 20:02 复盘拖到 22:10）。用户要求「单纯用
GitHub、不再混用两套系统」⇒ 用 GH Actions 自己当调度器：

    GH schedule 只当「点火器」（早晚各一次，允许延迟），
    点着之后 driver 常驻 runner 按**北京时间**精确排队 dispatch
    stock.yml 的各时点——主链逻辑零改动。

    ┌ morning   07:50 点火 → 08:50 pre → 09:25 auction（顺链启动
    │           live 巡检）→ 09:45 intraday am → 10:00 audit-am
    ├ afternoon 13:50 点火 → 14:40 intraday pm → 15:22 close → 15:45 audit-close
    └ evening   19:30 点火 → 20:02 review → 20:20 audit-review

    audit-* 时点 = 重发对应 task 的 dispatch（build 被日熔丝拦掉，
    但每次 run 末尾的「推送验收」步骤会独立核对送达并按需补发）——
    与原 cron-job 定时器的语义完全一致。

## 可靠性设计

  · **双点火 + 幂等**：morning/afternoon 各有两个 schedule（主 + 35/30
    分钟备份）。GH schedule 延迟 ≤35 分钟时备份准时接管；同日已有实例
    在跑/已成功 → 第二个点火直接退出（GH API 查本 workflow 当日 run）。
  · **非交易日零消耗**：点火后先查 trade_calendar，周末/节假日直接退出
    （不 dispatch 任何东西）。
  · **到点未睡够/睡过头都安全**：已过目标时刻 → 立即 dispatch（退化但
    不断链）；未到 → 分段小睡，日志每 10 分钟报一次活。
  · 单个 dispatch 失败重试 3 次；主链 run 本身的成败由其自检步骤负责，
    driver 只负责"准时开火"。

## 分钟账（回答「2000 分钟会不会用超」）

本仓库是 **public** → GH 托管标准 runner **免费不限时长**（2000 分钟/
月限制只适用于私有仓库）。driver 全天睡眠时间也算分钟，但公开仓不计费。

用法：python -m tools.day_driver --part morning
"""
import argparse
import datetime as _dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

_CST = _dt.timezone(_dt.timedelta(hours=8))
GH_API = "https://api.github.com"

# 每个时点的 dispatch 目标（stock.yml）与其北京时间触发点。
# 顺序即执行顺序；wait_until 负责在两个时点之间小睡。
PLAN = {
    "morning": [
        ("08:50", "pre",      {}),
        ("09:25", "auction",  {}),
        ("09:45", "intraday", {"slot": "am"}),
        ("10:00", "pre",      {}),          # audit-am（build 被日熔丝拦，自检仍跑）
    ],
    "afternoon": [
        ("14:40", "intraday", {"slot": "pm"}),
        ("15:22", "close",    {}),
        ("15:45", "close",    {}),          # audit-close
    ],
    "evening": [
        ("20:02", "review",   {}),
        ("20:20", "review",   {}),          # audit-review
    ],
}


def bj_now():
    """北京时间（runner 是 UTC，绝不能直接 datetime.now()）。"""
    return _dt.datetime.now(_CST)


def _req(method, url, token, body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "astra-day-driver"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return r.status, (json.loads(raw) if raw else {})


def dispatch(token, repo, task, extra=None, retries=3):
    """dispatch stock.yml；失败退避重试。返回 True/False。"""
    inputs = {"task": task}
    inputs.update(extra or {})
    for attempt in range(retries):
        try:
            st, _ = _req(
                "POST",
                f"{GH_API}/repos/{repo}/actions/workflows/stock.yml/dispatches",
                token, {"ref": "main", "inputs": inputs})
            if st in (204, 200, 201):
                return True
            print(f"[driver] dispatch {task} {extra or ''} → HTTP {st}",
                  flush=True)
        except urllib.error.HTTPError as e:
            print(f"[driver] dispatch {task} → HTTP {e.code}（第"
                  f"{attempt + 1}/{retries} 次）", flush=True)
        except Exception as e:                      # noqa: BLE001
            print(f"[driver] dispatch {task} → {type(e).__name__} {e}（第"
                  f"{attempt + 1}/{retries} 次）", flush=True)
        time.sleep(15 * (attempt + 1))
    return False


def another_alive(token, repo, wf_file, my_run_id):
    """本 workflow 今天（北京日）是否已有**别的**实例在跑或已成功。

    双点火（主 + 备份 schedule）下防止两份 driver 同时排队 dispatch。"""
    try:
        today = bj_now().strftime("%Y-%m-%d")
        st, d = _req(
            "GET",
            f"{GH_API}/repos/{repo}/actions/workflows/{wf_file}"
            f"/runs?per_page=10", token)
        if st != 200:
            return False                            # 查不到 → 宁可放行
        for r in d.get("workflow_runs", []):
            if r.get("id") == my_run_id:
                continue
            created = _dt.datetime.fromisoformat(
                r["created_at"].replace("Z", "+00:00")).astimezone(_CST)
            if created.strftime("%Y-%m-%d") != today:
                continue
            if r.get("status") != "completed" or r.get("conclusion") in (
                    "success",):
                return True
    except Exception as e:                          # noqa: BLE001
        print(f"[driver] 幂等检查失败（放行）: {type(e).__name__} {e}",
              flush=True)
    return False


def wait_until(hhmm, now=None):
    """小睡到北京时间的 hh:mm（到点已过 → 立即返回，不断链）。

    返回 (目标, 实际睡了多少秒)。分段睡，日志每 10 分钟报活——
    GH 日志长时间无输出会被误认为僵尸 job（其实不会，但日志可读性
    对排障至关重要）。"""
    target = _dt.datetime.strptime(hhmm, "%H:%M").time()
    n = now or bj_now()
    tgt = _dt.datetime.combine(n.date(), target, tzinfo=_CST)
    if tgt <= n:
        print(f"[driver] {hhmm} 已过（现在 {n:%H:%M}）→ 立即执行", flush=True)
        return tgt, 0.0
    total = (tgt - n).total_seconds()
    print(f"[driver] 等待至 {hhmm}（{total/60:.0f} 分钟）", flush=True)
    slept = 0.0
    while slept < total:
        chunk = min(600.0, total - slept)           # 10 分钟一段
        time.sleep(chunk)
        slept += chunk
        print(f"[driver] … 已等 {slept/60:.0f}/{total/60:.0f} 分钟"
              f"（{bj_now():%H:%M}）", flush=True)
    return tgt, slept


# ---------------------------------------------------------------------------
# 前序推送核验（2026-09-27 升级：守门盯「日驱动没跑/推送没到」）
#
# 判据 = dist/push_ledger.json（云端唯一权威账本——"步骤 success"≠"送达"），
# 而不是"run 存在"。intraday_am/pm/live 时常**合法静默**（无事件不推），
# 永不列入必达项；只核验 build_* 主链推送。
#   · morning 结束后（~10:01）：pre/auction 已送达？
#   · afternoon 开始（13:50）：pre/auction 已送达？（morning 静默 → 告警）
#   · afternoon 结束（~15:47）：close 已送达？缺失 → **自动补发 close**
#   · evening 开始（19:30）：close 已送达？缺失 → 自动补发（双保险）
#   · evening 结束（~20:21）：review 已送达？+ 主链连续失败检查
# 补发的 close 是真推送（当日额度未占用，日熔丝不拦）；若 15:22 那次只是
# 慢、补发 run 后到，日熔丝会拦掉后者，绝不重复推。
# ---------------------------------------------------------------------------
VERIFY_START = {"morning": (), "afternoon": ("build_pre", "build_auction"),
                "evening": ("build_close",)}
VERIFY_END = {"morning": ("build_pre", "build_auction"),
              "afternoon": ("build_close",), "evening": ("build_review",)}
CATCHUP = {"build_close": "close"}


def ledger_modes_today(token, repo, date):
    """→ (今日已 sent 的 mode 集合, 账本是否可读)。

    账本读不到 → (set(), False)：宁可漏报不可误报（账本读不到≠没推送）。"""
    try:
        st, d = _req("GET", f"{GH_API}/repos/{repo}/contents/"
                     "dist/push_ledger.json?ref=main", token)
        if st != 200:
            return set(), False
        import base64
        raw = base64.b64decode(d.get("content", "")).decode("utf-8", "replace")
        ledger = json.loads(raw) or {}
    except Exception as e:                          # noqa: BLE001
        print(f"[driver] 账本读取失败（跳过核验，不告警）: "
              f"{type(e).__name__} {e}", flush=True)
        return set(), False
    sent = set()
    for item in ledger.values():
        if not isinstance(item, dict):
            continue
        if str(item.get("ts", "")).startswith(date) \
                and item.get("status") == "sent":
            sent.add(item.get("mode"))
    return sent, True


def _alert(text, dry=False):
    """守门告警走真实推送通道（force 绕过去重——守门告警必须到达）。"""
    print("[driver] ⚠ 告警内容：\n" + text, flush=True)
    if dry:
        return
    try:
        from pipeline import notifier
        r = notifier.push("watchdog_alert", "日驱动守门告警",
                          notifier.md2html(text), force=True)
        print(f"[driver] 告警推送: {r}", flush=True)
    except Exception as e:                          # noqa: BLE001
        print(f"[driver] 告警推送失败（忽略）: {type(e).__name__} {e}",
              flush=True)


def verify_prior(part, token, repo, date, when, dry=False):
    """核验必达推送；缺失 → 告警 + 尽力补发（见 CATCHUP）。返回告警条数。"""
    need = (VERIFY_START if when == "start" else VERIFY_END).get(part, ())
    if not need:
        return 0
    sent, ok = ledger_modes_today(token, repo, date)
    if not ok:
        return 0
    missing = [m for m in need if m not in sent]
    if not missing:
        print(f"[driver] 前序核验 ✓ {part}/{when}：{len(need)} 项均已送达",
              flush=True)
        return 0
    for m in list(missing):
        task = CATCHUP.get(m)
        if task:
            print(f"[driver] ⚠ {m} 今日未送达 → 自动补发 {task}", flush=True)
            if dry or dispatch(token, repo, task):
                missing.remove(m)
    lines = [f"- {m} 今日未送达（{bj_now():%H:%M} 核验）" for m in missing]
    if any(m in CATCHUP for m in missing):
        lines.append("- 补发 dispatch 已触发/失败，详见 Actions 运行记录")
    else:
        lines.append("- 时效性推送无法补发，请到 Actions 页查看对应时点运行")
    _alert(f"日驱动守门告警 · {part}/{when}\n\n" + "\n".join(lines), dry=dry)
    return len(missing)


def chain_check(token, repo, dry=False):
    """主链连续失败检查（复用 timer_guard.chain_status，只在晚间跑一次）。"""
    try:
        from pipeline import timer_guard
        state, detail = timer_guard.chain_status(repo, token)
    except Exception as e:                          # noqa: BLE001
        print(f"[driver] 主链检查异常（忽略）: {type(e).__name__} {e}")
        return
    if state == "blocked":
        _alert(f"日驱动守门告警 · 主链卡死\n\n- {detail}", dry=dry)
    elif state == "ok":
        print("[driver] 主链近期有成功 run ✓", flush=True)


def run_part(part, wf_file, token=None, repo=None, now=None, dry=False):
    """执行一个 part 的完整时点序列。返回 (成功数, 应发数)。

    非交易日/幂等退出 → (0, 0)（语义 = 无事可做，**不是失败**）。"""
    token = token or os.environ.get("GH_PAT", "").strip()
    repo = repo or os.environ.get("GH_REPO", "aprildream24/astock-system")
    n = now or bj_now()
    date = n.strftime("%Y-%m-%d")

    from pipeline import trade_calendar
    if not trade_calendar.is_trade_day(date):
        print(f"[driver] {date} 非交易日"
              f"（{trade_calendar.why_closed(date)}）→ 退出，零消耗", flush=True)
        return 0, 0
    if os.environ.get("DRY_RUN") == "1":
        dry = True

    if token:
        rid = os.environ.get("RUN_ID", "")
        if rid and another_alive(token, repo, wf_file, int(rid)):
            print("[driver] 今日已有实例在跑/已成功 → 本次点火退出（幂等）",
                  flush=True)
            return 0, 0
    else:
        print("[driver] ⚠ 无 GH_PAT → 跳过幂等检查（仍会尝试 dispatch）",
              flush=True)

    ok = 0
    if token:
        verify_prior(part, token, repo, date, "start", dry=dry)
    for hhmm, task, extra in PLAN[part]:
        wait_until(hhmm, now=now)
        if dry:
            print(f"[driver] [dry] dispatch {task} {extra or ''}", flush=True)
            ok += 1
            continue
        if dispatch(token, repo, task, extra):
            ok += 1
            print(f"[driver] ✓ dispatch {task} {extra or ''}"
                  f"（{bj_now():%H:%M:%S}）", flush=True)
        else:
            print(f"[driver] ✗ dispatch {task} {extra or ''} 三次均失败"
                  f"——本时点丢失，验收/守门会兜底", flush=True)
    total = len(PLAN[part])
    print(f"[driver] {part} 完成：{ok}/{total} 个时点已触发", flush=True)
    if token:
        verify_prior(part, token, repo, date, "end", dry=dry)
        if part == "evening":
            chain_check(token, repo, dry=dry)
    return ok, total


def main(argv=None):
    ap = argparse.ArgumentParser(description="纯 GitHub 日驱动调度器")
    ap.add_argument("--part", required=True, choices=sorted(PLAN))
    ap.add_argument("--wf-file", default=None,
                    help="本 workflow 文件名（幂等检查用）")
    a = ap.parse_args(argv)
    wf = a.wf_file
    if wf is None:
        # GH 自动注入 GITHUB_WORKFLOW_REF（"…/.github/workflows/x.yml@…"）
        ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
        wf = ref.split("/.github/workflows/")[-1].split("@")[0] \
            if "/.github/workflows/" in ref else "unknown.yml"
    ok, total = run_part(a.part, wf)
    return 0 if total == 0 or ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
