# -*- coding: utf-8 -*-
"""把本地 PAT 写进仓库 Secrets（补齐 CI 写 Secret 所需的 GH_PAT）。

网页加自选的链路是：
    浏览器 → dispatch → CI → pipeline.sync_watch → 写 WATCH_CODES
最后一步需要 PAT（GITHUB_TOKEN 无 actions:secrets 写权限），
所以仓库必须有 GH_PAT 这个 Secret。本脚本负责配好它。

用法：
    python tools/setup_gh_pat_secret.py --probe
    python tools/setup_gh_pat_secret.py --write
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REPO = "aprildream24/astock-system"
TOKEN_FILE = os.path.join(os.environ.get("TEMP", "/tmp"), "astock_gh_token.txt")

# 需要配置的 Secret：名字 → 值来源
NEED = {
    "GH_PAT": "本地 PAT（写 WATCH_CODES 用）",
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

    exist = {}
    for name in NEED:
        r = gh("GET", f"/repos/{REPO}/actions/secrets/{name}", tok)
        exist[name] = {"exists": "_err" not in r,
                       "updated_at": r.get("updated_at")}
        print(f"  {name:12s} exists={exist[name]['exists']} "
              f"updated_at={exist[name]['updated_at'] or '-'}")

    if a.probe and not a.write:
        cmds = [n for n, v in exist.items() if not v["exists"]]
        print(f"\n缺 {len(cmds)} 个：{cmds}" if cmds else "\n全部已配置 ✓")
        return 0

    if not a.write:
        print("\n加 --write 执行写入。")
        return 0

    lt = local_token()
    for name, desc in NEED.items():
        if exist[name]["exists"]:
            print(f"  {name} 已存在，跳过（{desc}）")
            continue
        pk = gh("GET", f"/repos/{REPO}/actions/secrets/public-key", tok)
        sealed = sw.seal(pk["key"], lt)
        r = gh("PUT", f"/repos/{REPO}/actions/secrets/{name}", tok,
               {"encrypted_value": sealed, "key_id": pk["key_id"]})
        print(f"  写 {name} → HTTP {r.get('_status')} {r.get('_err') or ''}")
        chk = gh("GET", f"/repos/{REPO}/actions/secrets/{name}", tok)
        print(f"  核验 {name}: updated_at={chk.get('updated_at')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
