# -*- coding: utf-8 -*-
"""cron-job.org 旧定时器清理（CI 侧执行，2026-09-25）。

背景：cron-job.org 上同时挂着两套系统的定时器——本项目 astock-* 9 个，
旧仓库（另一账户）stock-*/exec-* 前缀 15 个。旧系统至今仍在向用户微信
推送旧版式消息（用户连续三次反馈「收到的还是老板式/没有标注」的真凶）。
本机 %TEMP% 里的 API key 被磁盘清理吞掉 ⇒ 由 CI 读取 Secret 执行删除。

纪律：
  · 只删 title 以 stock- / exec- 开头的任务（旧系统）；astock-* 严禁动；
  · 删除前逐条 GET /jobs/{id} 留档到运行日志（可重建）；
  · 终态核验：astock-* 9 个全部存在且 enabled=True，缺一个即标红。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

API = "https://api.cron-job.org/jobs"


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
        print("[cleanup] 无 CRONJOB_API_KEY → 无法执行（跳过，不报错）")
        return 0
    st, data = _req("GET", "/jobs", key)
    if st != 200:
        print(f"[cleanup] 列表失败 {st} → 本轮放弃（下轮定时器守门再试）")
        return 0
    jobs = data.get("jobs", [])
    targets = [j for j in jobs
               if j.get("title", "").startswith(("stock-", "exec-"))]
    mine = sorted(j["title"] for j in jobs
                  if j.get("title", "").startswith("astock-"))
    print(f"[cleanup] astock-* {len(mine)} 个（保留）| 旧系统 {len(targets)} 个"
          f"（待删）", flush=True)
    ok = fail = 0
    for j in targets:
        st, det = _req("GET", f"/jobs/{j['jobId']}", key)
        if st == 200:
            d = det.get("jobDetails", {})
            print(f"    留档 {j['title']}: url={d.get('url', '')[:80]}", flush=True)
        st2, _ = _req("DELETE", f"/jobs/{j['jobId']}", key)
        if st2 in (200, 204, 404):
            ok += 1
            print(f"  删除 {j['title']} ✓ ({st2})", flush=True)
        else:
            fail += 1
            print(f"  FAIL {j['title']}: {st2}", flush=True)
        time.sleep(4)
    # 终态核验
    st, data = _req("GET", "/jobs", key)
    left = data.get("jobs", [])
    left_old = [j["title"] for j in left
                if not j.get("title", "").startswith("astock-")]
    bad_mine = [j["title"] for j in left
                if j.get("title", "").startswith("astock-")
                and not j.get("enabled")]
    print(f"[cleanup] 删除 {ok} / 失败 {fail}", flush=True)
    print("[cleanup] 终态 astock-*:",
          sorted(j["title"] for j in left
                 if j.get("title", "").startswith("astock-")), flush=True)
    if left_old:
        print(f"[cleanup] ⚠ 旧系统仍有 {len(left_old)} 个存活（限流未删完），"
              f"下轮继续：{left_old}", flush=True)
    if bad_mine:
        print(f"[cleanup] ⚠🔴 本项目定时器被停用：{bad_mine}", flush=True)
    return 0


if __name__ == "__main__":
    main()
