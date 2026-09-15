# -*- coding: utf-8 -*-
"""一键部署到 GitHub（供本地/离线环境使用）。

场景：用户电脑不开机、在离线状态完成改动，之后需要把改动同步到线上 CI。
本脚本用 GitHub REST API（不依赖 git 协议，内网被墙也能用）做：
  ① 全量同步代码（幂等，排除密钥/缓存/构建产物）
  ② （可选）配置 Actions Secrets
  ③ （可选）触发 workflow_dispatch 并等待结果

用法：
    python tools/deploy.py --token ghp_xxx
    python tools/deploy.py --token ghp_xxx --dispatch close --wait
    python tools/deploy.py --token ghp_xxx --secrets-from config/
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = "aprildream24"
REPO = "astock-system"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"

# ⚠️ .github/workflows 必须同步（stock.yml 是修复的一部分：75min 超时 +
# continue-on-error）。全量 tree commit 时未被列出的文件会被删除，
# 因此绝不能把 workflows 排除在外。
EXCLUDE_DIRS = {"__pycache__", ".git", "cache", "dist", "site", ".zcode",
                ".workbuddy", ".pytest_cache", "node_modules"}
EXCLUDE_FILES = {"notify.json", "users.json", "holdings.json", "watch.json",
                 "models.json", "gh_sync.py"}
EXCLUDE_EXT = {".db", ".bin", ".pyc", ".log", ".zip"}

# 调试/回归产物：可能临时出现在根目录（_reg.txt 等），一律不上线。
JUNK_PREFIXES = ("_reg", "_e2e", "_probe", "_dbg", "_tmp")


def _req(method, url, token, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={
        "Authorization": f"token {token}", "User-Agent": "astra-deploy",
        "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            payload = r.read()
            return r.status, (payload if raw else json.loads(payload or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, {"msg": e.read().decode("utf-8", "replace")[:400]}


def collect_files():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 注意：只剪 .git 本身，不能用 startswith(".git")——那会连 .github 一起剪掉。
        dirnames[:] = [d for d in dirnames
                       if d not in EXCLUDE_DIRS and d != ".git"]
        rel_dir = os.path.relpath(dirpath, ROOT)
        for f in sorted(filenames):
            if f in EXCLUDE_FILES or os.path.splitext(f)[1] in EXCLUDE_EXT:
                continue
            if rel_dir == "." and f.startswith(JUNK_PREFIXES):
                continue          # 根目录调试/回归产物不上线
            rel = f if rel_dir == "." else os.path.join(rel_dir, f)
            rel = rel.replace("\\", "/")
            if rel.startswith(("cache/", "dist/", "site/", ".workbuddy/")):
                continue
            files.append(rel)
    return files


def sync(token):
    """把工作区全量推成一个 commit（base_tree 模式，增量高效）。"""
    st, cur = _req("GET", f"{API}/contents/README.md?ref=main", token)
    if st != 200:
        print("读取 README 失败：", st, cur)
        return False
    body = {"message": "deploy: 修复全天零推送 + 分用户分级权限",
            "content": base64.b64encode(b"placeholder").decode(),
            "sha": cur["sha"], "branch": "main"}
    with open(os.path.join(ROOT, "README.md"), "rb") as f:
        body["content"] = base64.b64encode(f.read()).decode()
    st, put = _req("PUT", f"{API}/contents/README.md", token, body)
    if st not in (200, 201):
        print("基点提交失败：", st, put)
        return False
    base_tree = put["commit"]["tree"]["sha"]
    print(f"基点 OK tree={base_tree[:10]}")

    files = collect_files()
    print(f"待同步 {len(files)} 个文件")
    tree, fails = [], []
    for i, rel in enumerate(files):
        with open(os.path.join(ROOT, rel), "rb") as f:
            content = f.read()
        st, blob = _req("POST", f"{API}/git/blobs", token,
                        {"content": base64.b64encode(content).decode(),
                         "encoding": "base64"})
        if st not in (200, 201):
            fails.append(rel)
            continue
        tree.append({"path": rel, "mode": "100644", "type": "blob",
                     "sha": blob["sha"]})
        if (i + 1) % 30 == 0:
            print(f"  blobs {i + 1}/{len(files)}")
    if fails:
        print("FAIL blobs:", fails)
        return False
    st, tree_obj = _req("POST", f"{API}/git/trees", token,
                        {"base_tree": base_tree, "tree": tree})
    if st not in (200, 201):
        print("建 tree 失败：", st, tree_obj)
        return False
    st, commit = _req("POST", f"{API}/git/commits", token,
                      {"message": "deploy: 修复全天零推送 + 分用户分级权限",
                       "tree": tree_obj["sha"],
                       "parents": [put["commit"]["sha"]]})
    if st not in (200, 201):
        print("建 commit 失败：", st, commit)
        return False
    st, ref = _req("PATCH", f"{API}/git/refs/heads/main", token,
                   {"sha": commit["sha"]})
    if st not in (200, 201):
        print("更新 ref 失败：", st, ref)
        return False
    print(f"同步完成 commit={commit['sha'][:10]}")
    return True


def dispatch(token, task, wait=False):
    st, r = _req("POST", f"{API}/actions/workflows/stock.yml/dispatches", token,
                 {"ref": "main", "inputs": {"task": task}})
    if st not in (204, 200, 201):
        print("触发失败：", st, r)
        return False
    print(f"已触发 task={task}")
    if not wait:
        return True
    print("等待运行结束（最多 20 分钟）…")
    for _ in range(80):
        time.sleep(15)
        st, d = _req("GET", f"{API}/actions/runs?per_page=3", token)
        if st != 200:
            continue
        for run in d.get("workflow_runs", []):
            if run["event"] == "workflow_dispatch" and \
                    run["status"] == "completed":
                print(f"  最近一次：{run['conclusion']} "
                      f"{run['created_at']} {run['html_url']}")
                return run["conclusion"] == "success"
    print("等待超时，请到 Actions 页面查看")
    return False


def set_secret(token, name, value):
    """写入 Actions Secret（需 libsodium —— 未安装则给出提示）。"""
    try:
        from nacl import encoding, public  # type: ignore
    except ImportError:
        print(f"[跳过] {name}：未安装 PyNaCl（pip install pynacl）")
        return False
    st, key = _req("GET", f"{API}/actions/secrets/public-key", token)
    if st != 200:
        print("取公钥失败：", st, key)
        return False
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    box = public.SecretBox(public.Box(public.PrivateKey.generate(), pk),
                           encoding.RawEncoder())
    sealed = base64.b64encode(bytes(box)).decode()
    st, r = _req("PUT", f"{API}/actions/secrets/{name}", token,
                 {"encrypted_value": sealed, "key_id": key["key_id"]})
    print(f"{'OK' if st in (201, 204) else 'FAIL'} {name} {st}")
    return st in (201, 204)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--dispatch", default=None,
                    choices=["pre", "auction", "close", "review", "site"])
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--secrets-from", default=None,
                    help="从该目录读取 notify.json/users.json 并写入 Secrets")
    ap.add_argument("--no-sync", action="store_true")
    a = ap.parse_args()

    ok = True
    if not a.no_sync:
        ok = sync(a.token)
    if a.secrets_from:
        d = a.secrets_from
        p = os.path.join(d, "notify.json")
        if os.path.exists(p):
            cfg = json.load(open(p, encoding="utf-8"))
            if cfg.get("pushplus_token"):
                set_secret(a.token, "PUSHPLUS_TOKEN",
                           cfg["pushplus_token"])
            if cfg.get("serverchan_key"):
                set_secret(a.token, "SERVERCHAN_KEY", cfg["serverchan_key"])
            if cfg.get("glm_api_key"):
                set_secret(a.token, "GLM_API_KEY", cfg["glm_api_key"])
            if cfg.get("glm_model"):
                set_secret(a.token, "GLM_MODEL", cfg["glm_model"])
        u = os.path.join(d, "users.json")
        if os.path.exists(u):
            raw = json.load(open(u, encoding="utf-8"))
            if isinstance(raw.get("users"), list):
                # 站点口令 JSON（id → pass，含 owner）
                site = {it["id"]: it["pass"] for it in raw["users"]
                        if it.get("id") and it.get("pass")}
            else:
                site = {k: v for k, v in raw.items()
                        if isinstance(v, str) and not k.startswith("_")}
            set_secret(a.token, "SITE_USERS", json.dumps(site,
                                                        ensure_ascii=False))
        w = os.path.join(d, "watch.json")
        if os.path.exists(w):
            set_secret(a.token, "WATCH_CODES",
                       json.dumps(json.load(open(w, encoding="utf-8"))))
    if a.dispatch:
        ok = dispatch(a.token, a.dispatch, a.wait) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
