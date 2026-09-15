# -*- coding: utf-8 -*-
"""端到端真验：网页加自选 → workflow_dispatch → CI → Secret 落库。

不 mock、不 dry-run：真发 dispatch、真等 CI、真读 Secret 的 updated_at。
用完把 WATCH_CODES 恢复原值。

用法：
    python tools/verify_watch_e2e.py --probe      # 只探权限，不改任何东西
    python tools/verify_watch_e2e.py --full       # 完整链路（会改 Secret）
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REPO = "aprildream24/astock-system"
SECRET = "WATCH_CODES"
WF = "stock.yml"
TOKEN_FILE = os.path.join(os.environ.get("TEMP", "/tmp"), "astock_gh_token.txt")


def token():
    t = os.environ.get("GH_PAT", "").strip()
    if t:
        return t
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    raise SystemExit("找不到 PAT")


def gh(method, path, tok, body=None, raw=False):
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + tok,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "astock-verify",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            b = r.read()
            hdr = dict(r.headers)
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_err": e.read().decode()[:400]}
    if raw:
        return {"_status": 200, "_body": b, "_headers": hdr}
    return json.loads(b) if b else {"_status": 200}


def probe(tok):
    print("=" * 62)
    print("① 权限探针")
    print("=" * 62)
    me = gh("GET", "/user", tok)
    print(f"  身份          : {me.get('login')}  (HTTP {me.get('_status')})")

    repo = gh("GET", f"/repos/{REPO}", tok)
    perms = (repo.get("permissions") or {})
    print(f"  仓库可见      : {repo.get('full_name')}  private={repo.get('private')}")
    print(f"  push 权限     : {perms.get('push')}   admin={perms.get('admin')}")

    pk = gh("GET", f"/repos/{REPO}/actions/secrets/public-key", tok)
    print(f"  取 Secret 公钥: {pk.get('key_id') or pk.get('_err')}")

    st = gh("GET", f"/repos/{REPO}/actions/secrets/{SECRET}", tok)
    print(f"  读 {SECRET} : exists={not st.get('_err')} "
          f"updated_at={st.get('updated_at') or '-'}")

    wf = gh("GET", f"/repos/{REPO}/actions/workflows/{WF}", tok)
    print(f"  工作流 {WF} : {wf.get('state') or wf.get('_err')} "
          f"(id={wf.get('id')})")
    return {
        "login": me.get("login"),
        "admin": perms.get("admin"),
        "push": perms.get("push"),
        "key_id": pk.get("key_id"),
        "secret_updated_at": st.get("updated_at"),
        "wf_state": wf.get("state"),
    }


def dispatch(tok, codes, ref="main"):
    print("\n" + "=" * 62)
    print("② 发 workflow_dispatch（模拟网页 owner 点「加入」）")
    print("=" * 62)
    r = gh("POST", f"/repos/{REPO}/actions/workflows/{WF}/dispatches", tok,
           {"ref": ref, "inputs": {"task": "watch-sync", "codes": codes}})
    print(f"  POST /dispatches → HTTP {r.get('_status')} "
          f"{r.get('_err') or '(204 无 body = 已受理)'}")
    return r.get("_status")


def wait_run(tok, before_ids, timeout=360):
    """等最新一次 watch-sync run 出现并完成。"""
    print("\n" + "=" * 62)
    print("③ 等 CI 跑完")
    print("=" * 62)
    t0 = time.time()
    run_id = None
    while time.time() - t0 < 90:
        rs = gh("GET", f"/repos/{REPO}/actions/runs?per_page=10", tok)
        for r in rs.get("workflow_runs", []):
            if r["id"] not in before_ids:
                run_id = r["id"]
                print(f"  发现新 run {run_id}  event={r['event']} "
                      f"status={r['status']}")
                break
        if run_id:
            break
        time.sleep(6)
    if not run_id:
        print("  !! 90s 内未见新 run（dispatch 可能被拒）")
        return None, None

    while time.time() - t0 < timeout:
        r = gh("GET", f"/repos/{REPO}/actions/runs/{run_id}", tok)
        st, cc = r.get("status"), r.get("conclusion")
        print(f"  [{int(time.time() - t0):3d}s] status={st} conclusion={cc}")
        if st == "completed":
            return run_id, cc
        time.sleep(10)
    return run_id, "timeout"


def run_log(tok, run_id):
    import io
    import zipfile
    r = gh("GET", f"/repos/{REPO}/actions/runs/{run_id}/logs", tok, raw=True)
    if r.get("_status") != 200:
        return f"取日志失败：{r.get('_err')}"
    z = zipfile.ZipFile(io.BytesIO(r["_body"]))
    txt = []
    for n in z.namelist():
        if "watch" in n.lower() or "sync" in n.lower():
            try:
                txt.append(f"----- {n} -----\n" +
                           z.read(n).decode("utf-8", "replace")[-3000:])
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(txt) if txt else "（日志包里无 watch/sync 相关步骤）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--codes", default="sh600519,sz000001")
    a = ap.parse_args()
    tok = token()

    info = probe(tok)
    if a.probe and not a.full:
        print("\n探针完成（未改动任何东西）。")
        return 0

    if not a.full:
        print("\n加 --full 跑完整链路。")
        return 0

    before = gh("GET", f"/repos/{REPO}/actions/runs?per_page=15", tok)
    before_ids = {r["id"] for r in before.get("workflow_runs", [])}
    old_updated = info.get("secret_updated_at")

    code = dispatch(tok, a.codes)
    if code not in (204, 200):
        print("\n!! dispatch 被拒，链路到此为止。检查 PAT 是否有 actions:write。")
        return 1

    run_id, cc = wait_run(tok, before_ids)
    if run_id:
        print(f"\n  run 结论：{cc}")
        print(f"  run 地址：https://github.com/{REPO}/actions/runs/{run_id}")
        log = run_log(tok, run_id)
        print("\n" + "=" * 62)
        print("④ 同步步骤日志")
        print("=" * 62)
        print(log[-2500:])

    after = gh("GET", f"/repos/{REPO}/actions/secrets/{SECRET}", tok)
    new_updated = after.get("updated_at")
    print("\n" + "=" * 62)
    print("⑤ Secret 是否被改写")
    print("=" * 62)
    print(f"  之前 updated_at : {old_updated}")
    print(f"  之后 updated_at : {new_updated}")
    ok = bool(new_updated) and new_updated != old_updated
    print(f"  ⇒ {'✅ 已改写，链路通' if ok else '❌ 未变化，链路未通'}")

    # 恢复原自选
    print("\n" + "=" * 62)
    print("⑥ 恢复原 WATCH_CODES")
    print("=" * 62)
    cur = os.path.join(ROOT, "config", "watch.json")
    orig = []
    if os.path.exists(cur):
        with open(cur, encoding="utf-8") as f:
            orig = json.load(f)
    print(f"  本地 watch.json 原值：{orig}")
    r2 = gh("POST", f"/repos/{REPO}/actions/workflows/{WF}/dispatches", tok,
            {"ref": "main", "inputs": {"task": "watch-sync",
                                       "codes": ",".join(orig)}})
    print(f"  恢复 dispatch → HTTP {r2.get('_status')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
