# -*- coding: utf-8 -*-
"""把本地产出的推送账本提交回仓库（2026-09-15）。

## 为什么需要

`tools/daily_check.py` 的「推送发送」体检项靠读取**远端**
`/contents/dist/push_ledger.json` 判定今天是否真发出过推送（因为用户
电脑常年不开机，本地镜像可能停在几天前，只有 CI 侧账本才是权威）。

但 `gh_sync.py` 的 `EXCLUDE_DIRS` 排除了整个 `dist/`，CI 也从不提交账本
⇒ 远端永远 404 ⇒ 体检永远误报「今天没有任何生产推送」。

这是一个**纯假的告警源**，会让人以为推送坏了（用户抱怨的「每天说没问题、
实盘有问题」正好相反：这里是「明明推了却报没推」）。

## 做法

CI 里跑，把 `dist/push_ledger.json` 用 contents API 写成一次提交。
账本内容只有 {mode, date, ts, channels} 之类元信息，
**不含** 自选/持仓/口令 —— 可安全入公开仓库。

用法：
    python -m pipeline.push_ledger_sync
环境变量：
    GH_PAT    有 contents:write 的令牌
    GH_REPO   默认 aprildream24/astock-system
"""
import base64
import json
import os
import sys

DEFAULT_REPO = "aprildream24/astock-system"
LEDGER_REL = "dist/push_ledger.json"


def _gh(method, path, token, body=None):
    import urllib.error
    import urllib.request
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "astock-ledger-sync",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            b = r.read()
        return r.status, (json.loads(b) if b else {})
    except urllib.error.HTTPError as e:
        return e.code, {"err": e.read().decode("utf-8", "replace")[:300]}


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, LEDGER_REL)
    if not os.path.exists(p):
        print(f"[ledger-sync] 无本地账本 {LEDGER_REL}，跳过")
        return 0
    with open(p, "rb") as f:
        raw = f.read()
    if not raw.strip():
        print("[ledger-sync] 账本为空，跳过")
        return 0
    try:
        n = len(json.loads(raw.decode("utf-8")))
    except Exception as e:  # noqa: BLE001
        print(f"[ledger-sync] 账本不是合法 JSON（{e}），不提交以免污染远端")
        return 1

    tok = (os.environ.get("GH_PAT") or os.environ.get("GITHUB_PAT")
           or os.environ.get("GH_TOKEN") or "").strip()
    if not tok:
        print("[ledger-sync] 缺 GH_PAT，跳过（不影响主流程）")
        return 0
    repo = os.environ.get("GH_REPO", DEFAULT_REPO)

    st, cur = _gh("GET", f"/repos/{repo}/contents/{LEDGER_REL}", tok)
    body = {
        "message": "chore: 回写推送账本（供 daily_check 读取）",
        "content": base64.b64encode(raw).decode(),
        "branch": "main",
    }
    if st == 200 and cur.get("sha"):
        body["sha"] = cur["sha"]
        if cur.get("content"):
            try:
                old = base64.b64decode(cur["content"])
                if old == raw:
                    print("[ledger-sync] 远端账本已是最新，无需提交")
                    return 0
            except Exception:  # noqa: BLE001
                pass
    st2, res = _gh("PUT", f"/repos/{repo}/contents/{LEDGER_REL}", tok, body)
    if st2 in (200, 201):
        print(f"[ledger-sync] ✓ 已提交 {LEDGER_REL}（{n} 条记录）")
        return 0
    print(f"[ledger-sync] 提交失败 HTTP {st2}：{res.get('err')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
