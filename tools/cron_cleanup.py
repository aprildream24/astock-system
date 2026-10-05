# -*- coding: utf-8 -*-
"""cron-job.org 定时器核验（CI 侧执行，2026-10-05 改为**只读核验**）。

## ⚠️ 历史教训（必须永久记住）
本脚本 2026-09-25 的版本会**删除** title 以 stock- / exec- 开头的定时器
（当时误判为「旧系统遗留」）。用户 2026-10-05 明确澄清：
**stock-* / exec-* 是另一个在用系统的定时器，绝对不能删。**
删除逻辑已整体拆除——本脚本现在只做只读核验并输出报告，
对任何任务都不做修改/删除/启停。

职责：
  · 列出全部定时器，核对 astock-* 前缀的任务存在且 enabled=True；
  · stock-* / exec-*（另一系统）只读不碰；
  · astock-* 缺失/停用 → 标红输出（人工处理，绝不自动写）。
"""
import json
import os
import time
import urllib.request
import urllib.error

API = "https://api.cron-job.org/jobs"

# 本项目应有的定时器（astock-* 前缀）。新增/下线定时器时同步此清单。
EXPECTED_ASTOCK = [
    "astock-pre", "astock-auction", "astock-close", "astock-review",
    "astock-intraday-am", "astock-intraday-pm", "astock-intraday-live",
    "astock-day-morning", "astock-day-evening",
]


def _req(method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + key,
                                          "Accept": "application/json",
                                          "Content-Type": "application/json",
                                          "User-Agent": "astra-cleanup"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                b = r.read()
            return r.status, (json.loads(b) if b else {})
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 20 * (attempt + 1)
                print(f"    429 退避 {wait}s…", flush=True)
                time.sleep(wait)
                continue
            return e.code, {}
        except Exception as e:  # noqa: BLE001
            print(f"    网络异常 {type(e).__name__}，5s 重试", flush=True)
            time.sleep(5)
    return 0, {}


def main():
    key = (os.environ.get("CRONJOB_API_KEY")
           or os.environ.get("CRONJOB_API_KEY_2") or "").strip()
    if not key:
        print("[verify-timers] 无 CRONJOB_API_KEY → 无法核验（跳过，不报错）")
        return 0
    st, data = _req("GET", "/jobs", key)
    if st != 200:
        print(f"[verify-timers] 列表失败 {st} → 本轮放弃"
              "（密钥疑似失效或接口变更；定时器本身不受影响，"
              "核验恢复前请到 cron-job.org 后台人工确认）")
        return 0
    jobs = data.get("jobs", [])
    mine = {j["title"]: j for j in jobs
            if j.get("title", "").startswith("astock-")}
    others = sorted(j["title"] for j in jobs
                    if not j.get("title", "").startswith("astock-"))
    print(f"[verify-timers] astock-* {len(mine)} 个 | 其他系统 "
          f"{len(others)} 个（stock-*/exec-* 等——只读，绝不触碰）",
          flush=True)
    bad = []
    for title in EXPECTED_ASTOCK:
        j = mine.get(title)
        if j is None:
            bad.append(f"{title} 缺失")
            print(f"  🔴 {title}: 缺失", flush=True)
        elif not j.get("enabled"):
            bad.append(f"{title} 停用")
            print(f"  🔴 {title}: 已停用", flush=True)
        else:
            print(f"  ✅ {title}: enabled", flush=True)
    extra = [t for t in mine if t not in EXPECTED_ASTOCK]
    if extra:
        print(f"  ℹ astock-* 清单外多出（仅提示）：{extra}", flush=True)
    if bad:
        print(f"[verify-timers] ⚠ 需人工处理 {len(bad)} 项：{bad}"
              "（本脚本不自动修复——写入类操作只允许人到后台做）", flush=True)
    else:
        print("[verify-timers] ✅ astock-* 定时器全部就绪", flush=True)
    return 0


if __name__ == "__main__":
    main()
