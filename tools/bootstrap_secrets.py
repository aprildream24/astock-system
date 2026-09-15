# -*- coding: utf-8 -*-
"""一键补齐「网页加自选」所需的全部仓库 Secrets（2026-09-15）。

## 缺什么、为什么缺

真验（tools/verify_watch_e2e.py --full）实测链路：
    浏览器 → dispatch → CI → pipeline.sync_watch → 写 WATCH_CODES
CI 步骤跑到了，但日志里 `GH_PAT:` 为空 ⇒ sync_watch 退出码 3 ⇒ Secret 未
改写。仓库实际只有 6 个 Secret，缺两个关键项：

  GH_PAT           写 Secrets 必须 PAT（GITHUB_TOKEN 无 actions:secrets 权限）
  SITE_EDIT_TOKEN  注入 owner 密文包的 _admin.token，前端 owner 视图靠它
                   才有权发 dispatch（缺则面板显示「未配置写入令牌」）

本脚本把本地 PAT 同时写入这两个名字，并核验 updated_at 已变。

用法：
    python tools/bootstrap_secrets.py --probe
    python tools/bootstrap_secrets.py --write
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REPO = "aprildream24/astock-system"
TOKEN_FILE = os.path.join(os.environ.get("TEMP", "/tmp"), "astock_gh_token.txt")

NEED = {
    "GH_PAT": "CI 写 WATCH_CODES 用的 PAT",
    "SITE_EDIT_TOKEN": "owner 前端发 dispatch 用的令牌",
}


def local_token():
    t = os.environ.get("GH_PAT", "").strip()
    if t:
        return t
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    raise SystemExit(f"找不到 PAT（{TOKEN_FILE}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    from pipeline import sync_watch as sw
    from tools.verify_watch_e2e import gh, token
    tok = token()

    print("=" * 62)
    print("仓库 Secrets 现状")
    print("=" * 62)
    allsec = gh("GET", f"/repos/{REPO}/actions/secrets?per_page=100", tok)
    have = {s["name"]: s["updated_at"] for s in allsec.get("secrets", [])}
    for n, ts in sorted(have.items()):
        flag = "★待补" if n in NEED else "  ok  "
        print(f"  {flag} {n:18s} {ts}")
    missing = [n for n in NEED if n not in have]
    print(f"\n缺失：{missing or '无'}")

    if a.probe and not a.write:
        return 0
    if not a.write:
        print("\n加 --write 执行写入。")
        return 0

    lt = local_token()
    print("\n" + "=" * 62)
    print("写入")
    print("=" * 62)
    pk = gh("GET", f"/repos/{REPO}/actions/secrets/public-key", tok)
    for n in NEED:
        if n in have:
            print(f"  {n} 已存在，跳过")
            continue
        sealed = sw.seal(pk["key"], lt)
        r = gh("PUT", f"/repos/{REPO}/actions/secrets/{n}", tok,
               {"encrypted_value": sealed, "key_id": pk["key_id"]})
        print(f"  PUT {n:18s} → HTTP {r.get('_status')} {r.get('_err') or ''}")
        chk = gh("GET", f"/repos/{REPO}/actions/secrets/{n}", tok)
        print(f"      核验 updated_at = {chk.get('updated_at')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
