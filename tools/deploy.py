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
                ".workbuddy", ".pytest_cache", "node_modules",
                "Temp", "build_tmp"}
EXCLUDE_FILES = {"notify.json", "users.json", "holdings.json", "watch.json",
                 "models.json", "gh_sync.py"}
EXCLUDE_EXT = {".db", ".bin", ".pyc", ".log", ".zip", ".bak"}

# ★★★ config/ 用**白名单**：只允许样例文件上线。
# ⚠️ 血案（2026-09-16）：原先靠 `EXCLUDE_FILES` 精确文件名排除
# `users.json` —— 但 `users.json.bak` 名字不同，只被 `EXCLUDE_EXT` 的
# `.bak` 兜住（且是**事后**才加的）。那份备份里是明文站点口令
# （`astra-owner-2026`），**已经在公开仓库里躺了数天**。
# 教训与 ROOT_ALLOW 完全同源：**黑名单永远列不全，白名单才可靠**。
CONFIG_DIR = "config"
CONFIG_ALLOW_SUFFIX = ".example.json"

# 根目录只允许这些「已知属于仓库」的文件上线。
# ⚠️ 为什么用白名单而不是「排除 _xxx 前缀」：黑名单永远列不全——曾因
# `_deploy_out.txt` 不在前缀表里而被推上 CI，导致 test_deploy 在 runner 上
# 挂掉、进而全天零推送。根目录的临时产物名字是不可预测的，白名单才可靠。
ROOT_ALLOW = {".gitignore", "README.md", "clear_dedup.py"}
ROOT_ALLOW_EXT = {".py", ".md", ".txt", ".yml", ".yaml", ".json"}


def _req(method, url, token, body=None, raw=False, retries=3):
    """带重试的 GitHub API 调用。

    ⚠️ 2026-09-18 新增重试（实测两次部署都在 `POST /git/blobs` 中途抛
    `ConnectionAbortedError [Errno 10053]`——沙箱出口代理在大批量 POST 时
    会掐断已建立的连接）。原实现没有重试 ⇒ 前面已上传的几十个 blob 全白费、
    部署整批失败。GitHub 的 `POST /git/blobs` / `POST /git/trees` /
    `PATCH /git/refs` 都是**幂等**的（同内容重复创建只多一个未被引用的
    对象，不留副作用），所以网络类异常可以安全重试。
    `HTTPError` 不重试：那是语义错误（鉴权/参数），重试无意义。
    """
    data = json.dumps(body).encode() if body is not None else None
    last_err = None
    for i in range(retries + 1):
        req = urllib.request.Request(url, method=method, data=data, headers={
            "Authorization": f"token {token}", "User-Agent": "astra-deploy",
            "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                payload = r.read()
                return r.status, (payload if raw
                                  else json.loads(payload or b"{}"))
        except urllib.error.HTTPError as e:
            if e.code >= 500 and i < retries:
                last_err = e
                time.sleep(2.0 * (i + 1))
                continue
            return e.code, {"msg": e.read().decode("utf-8", "replace")[:400]}
        except Exception as e:  # noqa: BLE001 —— 网络类（含 10053/超时）
            last_err = e
            if i < retries:
                time.sleep(2.0 * (i + 1))
    raise last_err


def collect_files():
    """收集待上线文件。

    重要：本函数的结果**必须**覆盖仓库里所有应存在的文件——sync() 用
    base_tree 全量提交，未列出的已跟踪文件会被删除。所以排除项只允许是
    「本地产物/密钥/临时文件」，绝不能是源码或 CI 配置。
    """
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 注意：只剪 .git 本身，不能用 startswith(".git")——那会连 .github 一起剪掉。
        dirnames[:] = [d for d in dirnames
                       if d not in EXCLUDE_DIRS and d != ".git"]
        rel_dir = os.path.relpath(dirpath, ROOT)
        for f in sorted(filenames):
            if f in EXCLUDE_FILES or os.path.splitext(f)[1] in EXCLUDE_EXT:
                continue
            # 任何目录下 `_` 前缀 = 本机调试产物（`tests/_reg.out.txt`、
            # `_deploy_out.txt`…）。与根目录规则保持一致，避免"只在根目录防住"。
            if f.startswith("_"):
                continue
            rd = rel_dir.replace("\\", "/")
            if rd == CONFIG_DIR and not f.endswith(CONFIG_ALLOW_SUFFIX):
                continue
            if rel_dir == ".":
                # 根目录用白名单：只放行明确属于仓库的文件。
                # 名字以 _ 开头的（_reg.txt/_deploy_out.txt/…）一律视为本地产物。
                if f.startswith("_") or f.startswith("."):
                    if f not in ROOT_ALLOW:
                        continue
                elif f not in ROOT_ALLOW and \
                        os.path.splitext(f)[1] not in ROOT_ALLOW_EXT:
                    continue
            rel = f if rel_dir == "." else os.path.join(rel_dir, f)
            rel = rel.replace("\\", "/")
            if rel.startswith(("cache/", "dist/", "site/", ".workbuddy/",
                               "Temp/", "build_tmp/")):
                continue
            files.append(rel)
    return files


def should_purge(path, local_files=None):
    """远端路径是否属于「按现行排除策略本不该存在」的历史遗留物。

    ★ 为什么必须有这个函数：`sync()` 只「增/改」**从不删除** ⇒ 一个文件一旦
    被推上公开仓库，**即使后来补了排除规则，它也会永久留在远端**。
    已实测两例：`config/users.json.bak`（明文站点口令）、`Temp/` 与
    `tests/_*.txt`（本机调试产物）。⇒ 排除规则只防"未来"，purge 负责清"历史"。

    ⚠️ `dist/` 永不清理：`dist/push_ledger.json` 由 CI 的 `push_ledger_sync`
    维护，是账本权威（本地根本不收集它，盲目对齐会把账本删掉）。

    ★ 2026-09-18 新增 `local_files`：以**本地可部署清单**为唯一真相做对账。
    规则类黑名单有个追不上的盲区 —— **"曾经合法"** 的文件：本机调试脚本
    `gh_check.py` 早期被推上去，后来本地删掉了，远端却永久留痕（实测发现）。
    这类文件名字正常、后缀正常、也不在 `_`/`Temp/`/`build_tmp/` 里，任何
    黑名单都抓不住 ⇒ 只能反向对账：**远端有、本地清单没有 ⇒ 就是残留**。
    ⚠️ 该参数只在 `purge()` 里传入；不传时保持纯规则语义（便于单测）。
    """
    if path.startswith("dist/"):
        return False
    base = os.path.basename(path)
    if base.startswith("_"):
        return True
    if path.startswith(("Temp/", "build_tmp/", ".workbuddy/")):
        return True
    if path.startswith(CONFIG_DIR + "/") and not base.endswith(CONFIG_ALLOW_SUFFIX):
        return True
    if base in EXCLUDE_FILES or os.path.splitext(base)[1].lower() in EXCLUDE_EXT:
        return True
    if local_files is not None and path not in local_files:
        return True
    return False


def purge(token):
    """从远端删除历史遗留物（`sync()` 只增不删的补丁）。"""
    st, tr = _req("GET", f"{API}/git/trees/main?recursive=1", token)
    if st != 200:
        print("purge：读远端 tree 失败", st, tr)
        return False
    paths = [t["path"] for t in tr.get("tree", []) if t["type"] == "blob"]
    # ★ 以本地可部署清单做对账（见 should_purge 的 `local_files` 说明）。
    #   注意必须在 `sync()` 之后调用：此时远端已含全部本地文件，
    #   剩下的"远端有、本地无"就是纯粹的同步残留。
    local = set(collect_files())
    victims = sorted(p for p in paths if should_purge(p, local))
    if not victims:
        print("purge：远端无遗留文件 ✓")
        return True
    print(f"purge：待删除 {len(victims)} 个遗留文件")
    bad = 0
    for p in victims:
        st, cur = _req("GET", f"{API}/contents/{p}?ref=main", token)
        if st != 200:
            print("  skip", p, st)
            bad += 1
            continue
        st2, r2 = _req("DELETE", f"{API}/contents/{p}", token,
                       {"message": f"security: 清理不应公网的历史文件 {p}",
                        "sha": cur["sha"], "branch": "main"})
        print("  del", p, st2)
        if st2 not in (200, 201):
            bad += 1
    return bad == 0


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
    """写入 Actions Secret（需 PyNaCl；用 SealedBox 密封——GitHub 公钥加密惯例）。

    注意：PyNaCl 的 `nacl.public` 里**没有** SecretBox（那在 nacl.secret 里），
    对公钥加密正确做法是 SealedBox(pubkey).encrypt(...)。
    """
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
    sealed = base64.b64encode(public.SealedBox(pk).encrypt(
        value.encode("utf-8"))).decode()
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
