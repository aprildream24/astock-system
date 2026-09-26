# -*- coding: utf-8 -*-
"""盘中买点巡检定时器 astock-intraday-live 安装/校验（CI 侧执行，2026-09-26）。

背景（用户 2026-09-24「为什么又要等到看盘？我要尽可能快速地告诉我
可以买入的股票」）：盘中买点监控原定 09:45/14:40 两个时点——10:20 进
买区的票要么等到 14:40 才报、要么早已涨飞。本脚本在 cron-job.org 上
**克隆 astock-intraday-am 的任务配置**（同一 URL/鉴权/报文骨架，与主链
触发方式完全一致），新建 astock-intraday-live：

    触发节奏  每 10 分钟（北京时间 09:00-11:50 / 13:00-14:50，工作日）
    覆盖窗口  09:30-11:35 / 13:00-15:00（pipeline.intraday.in_window 守门，
              窗口外的触发静默跳过，不推送不写账本）
    推送纪律  事件级去重（live_alerts 账本：同票同事件当天只报一次）
              → 新进买区/触发止损/卖出信号 首次出现即刻推送，延迟 ≤10 分钟

幂等：定时器已存在则核对 enabled 与调度，不一致就修（PUT），绝不重复建。
时间语义：cron-job.org 的 schedule 数组按任务自身时区解释。这里不猜
时区名，而是**从 am 定时器反推**——am 实际在 09:45（北京）触发：
  · 若 am 存的是 hours=[9]/minutes=[45] ⇒ 存储口径=北京时间；
  · 若存的是 hours=[1]/minutes=[45]     ⇒ 存储口径=UTC。
两种口径都换算成对应的"每 10 分钟"组合，避免建出 01:00 触发的笑话。

用法（CI）：
    env CRONJOB_API_KEY=... python tools/timer_live.py
本脚本只操作 title 以 astock- 开头的任务；旧系统 stock-*/exec-* 一律不碰。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.cron-job.org/jobs"
UA = "astra-timer-live"

LIVE_TITLE = "astock-intraday-live"
CLONE_FROM = "astock-intraday-am"

# 目标节奏（北京时间）：09:00-11:50 + 13:00-14:50 每 10 分钟。
# 09:00-09:20 / 11:40-11:50 三两轮会因 in_window 守门静默跳过，
# 属可接受的浪费（换定时器数组的简单与可核对性）。
BJ_HOURS = [9, 10, 11, 13, 14]
BJ_MINUTES = [0, 10, 20, 30, 40, 50]


def _req(method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + key,
                                          "Accept": "application/json",
                                          "Content-Type": "application/json",
                                          "User-Agent": UA})
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


def _sched_for(details):
    """按克隆源的时区口径换算出"每 10 分钟"的 schedule 数组。

    返回 None 表示无法从克隆源反推口径（不应发生：am 固定 09:45 触发）。
    """
    sch = (details or {}).get("schedule") or {}
    hours, minutes = sch.get("hours") or [], sch.get("minutes") or []
    if 9 in hours and 45 in minutes:
        base = BJ_HOURS                       # 存储口径 = 北京时间
    elif 1 in hours and 45 in minutes:
        base = [h - 8 for h in BJ_HOURS]      # 存储口径 = UTC
    else:
        return None
    out = dict(sch)
    out["hours"] = sorted(base)
    out["minutes"] = list(BJ_MINUTES)
    out["wdays"] = sch.get("wdays") or [1, 2, 3, 4, 5]
    return out


def _retitle_body(body):
    """把克隆报文里的 slot 改为 live。报文是 GH dispatches 的 JSON：
    {"ref": ..., "inputs": {"task": "intraday", "slot": "am"}}。
    递归找任意层的 slot 键改写；找不到返回 None（调用方报错留档）。"""
    def walk(o):
        if isinstance(o, dict):
            if "slot" in o:
                o["slot"] = "live"
                return True
            return any(walk(v) for v in o.values())
        if isinstance(o, list):
            return any(walk(v) for v in o)
        return False
    try:
        obj = json.loads(body) if isinstance(body, str) else dict(body or {})
    except Exception:                           # noqa: BLE001
        return None
    return json.dumps(obj, ensure_ascii=False) if walk(obj) else None


def _payload_from(details, sched):
    """克隆源 jobDetails → 新建/更新 payload（只改 title/schedule/body）。"""
    payload = dict(details or {})
    payload.pop("jobId", None)
    payload["title"] = LIVE_TITLE
    payload["enabled"] = True
    payload["schedule"] = sched
    return payload


def main():
    key = (os.environ.get("CRONJOB_API_KEY")
           or os.environ.get("CRONJOB_API_KEY_2") or "").strip()
    if not key:
        print("[timer-live] 无 CRONJOB_API_KEY → 无法执行（跳过，不报错）")
        return 0
    st, data = _req("GET", "/jobs", key)
    if st != 200:
        print(f"[timer-live] 列表失败 {st} → 本轮放弃（守门下轮再试）")
        return 0
    jobs = {j.get("title"): j for j in data.get("jobs", [])}

    live = jobs.get(LIVE_TITLE)
    am = jobs.get(CLONE_FROM)
    if not am:
        print(f"[timer-live] ✗ 克隆源 {CLONE_FROM} 不存在——先修主链定时器")
        return 1

    # --- 克隆源详情（schedule 口径 + url/method/body/headers 骨架）---
    st, det = _req("GET", f"/jobs/{am['jobId']}", key)
    if st != 200:
        print(f"[timer-live] 读 {CLONE_FROM} 详情失败 {st}")
        return 0
    am_details = det.get("jobDetails") or {}
    sched = _sched_for(am_details)
    if not sched:
        print(f"[timer-live] ✗ 无法从 {CLONE_FROM} 反推时区口径："
              f"schedule={am_details.get('schedule')}")
        return 1
    body_live = _retitle_body(am_details.get("body"))
    if not body_live:
        print(f"[timer-live] ✗ 报文中找不到 slot 键，body="
              f"{str(am_details.get('body'))[:200]}")
        return 1
    print(f"[timer-live] schedule 口径反推成功：hours={sched['hours']} "
          f"minutes={sched['minutes']} tz={sched.get('timezone')}", flush=True)

    if live:
        # --- 幂等：已存在 → 核对并修正 ---
        st, det = _req("GET", f"/jobs/{live['jobId']}", key)
        cur = (det.get("jobDetails") or {}) if st == 200 else {}
        need = []
        if not live.get("enabled"):
            need.append("enabled=false → 开启")
        cur_sched = cur.get("schedule") or {}
        if (cur_sched.get("hours"), cur_sched.get("minutes")) != \
                (sched["hours"], sched["minutes"]):
            need.append(f"调度偏差 {cur_sched.get('hours')}"
                        f"/{cur_sched.get('minutes')} → 修正")
        if _retitle_body(cur.get("body")) is None and cur.get("body"):
            need.append("报文 slot 非 live → 修正")
        if not need:
            print(f"[timer-live] {LIVE_TITLE} 已存在且配置正确 ✓")
            return 0
        print("[timer-live] 修正：" + "；".join(need), flush=True)
        payload = _payload_from(cur, sched)
        payload["body"] = body_live
        st2, _ = _req("PUT", f"/jobs/{live['jobId']}", key,
                      {"job": payload})
        if st2 not in (200, 201, 204):
            st2b, _ = _req("PUT", f"/jobs/{live['jobId']}", key, payload)
            st2 = st2b
        print(f"[timer-live] PUT {LIVE_TITLE}: {st2} "
              f"{'✓' if st2 in (200, 201, 204) else '✗'}")
        return 0 if st2 in (200, 201, 204) else 1

    # --- 新建：克隆 am 全量配置，改 title/schedule/body ---
    payload = _payload_from(am_details, sched)
    payload["body"] = body_live
    st1, _ = _req("POST", "/jobs", key, {"job": payload})
    if st1 not in (200, 201):
        # cron-job.org 的包装习惯可能随版本变化：裸 payload 再试一次
        st1b, err = _req("POST", "/jobs", key, payload)
        st1 = st1b
    print(f"[timer-live] POST {LIVE_TITLE}: {st1} "
          f"{'✓ 已创建' if st1 in (200, 201) else '✗ 失败（下轮守门重试）'}",
          flush=True)
    # 终态核验
    st, data = _req("GET", "/jobs", key)
    ok = any(j.get("title") == LIVE_TITLE for j in data.get("jobs", []))
    print(f"[timer-live] 终态核验：{LIVE_TITLE} "
          f"{'存在 ✓' if ok else '未出现 ✗'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
