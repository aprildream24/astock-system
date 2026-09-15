# -*- coding: utf-8 -*-
"""实盘级全链路演练（E2E）——把「CI 会跑的每个任务」都在本机真跑一遍。

为什么要这个脚本（2026-09-15 用户质疑「每天说没问题，实盘就出问题」）：
过去我只跑单元回归，那只能证明「函数逻辑对」。它证明不了：
  · CI 环境下（无 config 私密文件、只有 Secrets）配置能否正确加载；
  · 四个 task（pre/auction/close/review）是否**真的能走完并出东西**；
  · 推送是否真的发出（而不是 dry-run / 被日级保险丝拦 / 假 sent）；
  · 站点是否真的重建成功、密文是否可分角色解开。

本脚本对每个 task 独立子进程运行，捕获真实 stdout/stderr + 退出码，
最后给一张总表。任一 task 非零退出或产物缺失 → 整体 FAIL。

用法：
    python tools/e2e_drill.py                 # 全部 task
    python tools/e2e_drill.py --task close    # 单个
    python tools/e2e_drill.py --no-push       # 不真发（用 dry-run 环境）
"""
import argparse
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TASKS = ("pre", "auction", "close", "review")

# CI Secrets 的形状（值用本地真实配置，保证真发能验证通）
SECRET_FILES = ("notify.json", "users.json", "watch.json", "holdings.json")


def _load_local_secrets():
    """把本机 config 私密文件内容读成环境变量（模拟 CI Secrets）。"""
    cfgdir = os.path.join(ROOT, "config")
    env = {}
    notify = os.path.join(cfgdir, "notify.json")
    if os.path.exists(notify):
        c = json.load(open(notify, encoding="utf-8"))
        if c.get("pushplus_token"):
            env["PUSHPLUS_TOKEN"] = c["pushplus_token"]
        if c.get("serverchan_key"):
            env["SERVERCHAN_KEY"] = c["serverchan_key"]
        if c.get("glm_api_key"):
            env["GLM_API_KEY"] = c["glm_api_key"]
        if c.get("glm_model"):
            env["GLM_MODEL"] = c["glm_model"]
    uw = os.path.join(cfgdir, "users.json")
    if os.path.exists(uw):
        env["SITE_USERS"] = open(uw, encoding="utf-8").read()
    ww = os.path.join(cfgdir, "watch.json")
    if os.path.exists(ww):
        env["WATCH_CODES"] = open(ww, encoding="utf-8").read()
    return env


def _hide_private(active):
    """把 config 私密文件藏起来（模拟 CI runner）。返回恢复映射。

    ⚠️ 2026-09-15 血案：旧版用 `<file>.e2e_bak` 做暂存名，且恢复时有一条
    `not os.path.exists(p)` 守卫——只要演练过程中**任何代码重建了 p**
    （`_prepare_site_users` 会写 users.json；pipeline 也可能写），
    真配置就被永久遗弃在 `.e2e_bak` 里，而生效的是演练造的假配置。
    后果：notify.json 丢失 → 本地 task 全部退化成「无凭据 dry-run」，
    我还拿这个自己造成的破坏当「没推送是正常的」证据，循环自欺。

    现在改为两条硬约束：
      ① 暂存名带 pid + 唯一后缀，且**绝不**落在 config 目录里（隔离区），
         避免被 deploy / glob / 用户误当配置读走；
      ② 恢复永远**覆盖**回原位（先删 p 再 move），不做「已存在就跳过」。
    """
    cfgdir = os.path.join(ROOT, "config")
    staging = os.path.join(ROOT, ".e2e_staging")
    os.makedirs(staging, exist_ok=True)
    moved = {}
    for f in SECRET_FILES:
        p = os.path.join(cfgdir, f)
        if os.path.exists(p):
            b = os.path.join(staging, f"{f}.{os.getpid()}.bak")
            shutil.move(p, b)
            moved[p] = b
    return moved


def _restore(moved):
    """无条件把暂存文件搬回原位（覆盖演练期间被重建的文件）。

    不做「目标已存在就跳过」——那正是真配置被弃的根因。
    """
    for p, b in moved.items():
        try:
            if os.path.exists(b):
                if os.path.exists(p):
                    os.remove(p)
                shutil.move(b, p)
        except OSError as ex:  # noqa: BLE001
            print(f"  !! 恢复配置失败 {p}: {ex}")


def _recover_orphans():
    """启动自检：把历史遗留的 `.e2e_bak` 孤儿复原。

    2026-09-15 之前被遗弃的真配置就躺在 config/*.e2e_bak。
    规则：只有当**正式文件缺失**时才复原（缺失 = 那一定是被弃的真身）；
    正式文件存在则说明它是演练重写的产物，保留孤儿待人工确认，不自动覆盖。
    """
    cfgdir = os.path.join(ROOT, "config")
    fixed, kept = [], []
    for name in SECRET_FILES:
        p = os.path.join(cfgdir, name)
        orphan = p + ".e2e_bak"
        if not os.path.exists(orphan):
            continue
        if os.path.exists(p):
            # 正式文件在 → 对比内容，不同则保留孤儿并告警
            try:
                with open(p, encoding="utf-8") as fh:
                    a = fh.read()
                with open(orphan, encoding="utf-8") as fh:
                    b = fh.read()
            except OSError:
                a, b = "", ""
            if a != b:
                kept.append((orphan, p))
            continue
        shutil.move(orphan, p)
        fixed.append((orphan, p))
    return fixed, kept


def _prepare_site_users(env):
    """CI 的「构建加密站点」步骤会把 SITE_USERS 落成 config/users.json。"""
    cfgdir = os.path.join(ROOT, "config")
    os.makedirs(cfgdir, exist_ok=True)
    su = env.get("SITE_USERS")
    if su:
        with open(os.path.join(cfgdir, "users.json"), "w",
                  encoding="utf-8") as f:
            f.write(su)


# CI 的步骤序列（stock.yml 实证）：先抓数据，再构建。
# 演练若跳过抓取，本地库停在上一交易日 → pre/auction 必然「无快照拒绝构建」，
# 那 rc=0 会被误读成 PASS（2026-09-15 踩过）。所以抓取必须照做。
FETCH_CMD = {
    "close":  [PY, "-X", "utf8", "-m", "pipeline.fetch_daily"],
    "site":   [PY, "-X", "utf8", "-m", "pipeline.fetch_daily"],
    "pre":     [PY, "-X", "utf8", "-m", "pipeline.fetch_daily", "--days", "20"],
    "auction": [PY, "-X", "utf8", "-m", "pipeline.fetch_daily", "--days", "20"],
    "review":  [PY, "-X", "utf8", "-m", "pipeline.fetch_daily", "--days", "20"],
}


def run_fetch(task, env, timeout=3000):
    cmd = FETCH_CMD.get(task)
    if not cmd:
        return None
    t0 = time.time()
    e = dict(os.environ)
    e.update(env)
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=e, timeout=timeout)
        rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        rc, out, err = -9, "", f"TIMEOUT > {timeout}s"
    return {"task": task, "rc": rc, "secs": round(time.time() - t0, 1),
            "out": out, "err": err}


def run_task(task, env, timeout=1800):
    t0 = time.time()
    e = dict(os.environ)
    e.update(env)
    e.pop("ASTOCK_FORCE_PUSH", None)
    cmd = [PY, "-X", "utf8", "-m", "pipeline.build", "--task", task]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=e, timeout=timeout)
        rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        rc, out, err = -9, "", f"TIMEOUT > {timeout}s"
    return {"task": task, "rc": rc, "secs": round(time.time() - t0, 1),
            "out": out, "err": err}


def _ledger_tail(n=8):
    p = os.path.join(ROOT, "dist", "push_ledger.json")
    if not os.path.exists(p):
        return []
    d = json.load(open(p, encoding="utf-8"))
    rows = [(v.get("ts", ""), v.get("mode", ""), v.get("status", ""),
             ",".join(v.get("channels", {}).values()))
            for v in d.values()]
    rows.sort()
    return rows[-n:]


def _db_dates():
    p = os.path.join(ROOT, "cache", "market.db")
    if not os.path.exists(p):
        return {}
    con = sqlite3.connect(p)
    cur = con.cursor()
    out = {}
    for t, col in (("klines", "date"), ("candidate_snapshots", "date"),
                   ("zt_pool", "date")):
        try:
            cur.execute(f"select max({col}) from {t}")
            out[t] = cur.fetchone()[0]
        except Exception:  # noqa: BLE001
            out[t] = None
    con.close()
    return out


def _judge(r, task):
    """判定一次 task 运行是否**真的产出了东西**。

    ⚠️ 关键教训（2026-09-15）：不能只看 rc。`build()` 在数据未就绪时
    会打印「拒绝构建」+ 发告警，然后 **return None → rc 仍是 0**。
    我早先把 rc=0 当 PASS，所以"每天报没问题、实盘就不推送"。
    现在必须同时满足：
      ① rc == 0
      ② stdout 不含「拒绝构建」
      ③ 确有成功标志（构建完成 / 已发推送 / 站点已写）
    """
    out = r.get("out") or ""
    reasons = []
    if r["rc"] != 0:
        reasons.append(f"退出码 {r['rc']}")
    if "拒绝构建" in out:
        reasons.append("数据未就绪被拒绝构建")
    ok_marks = ("构建完成", "已发", "站点", "推送", "写入 site",
                "build done", "pushed")
    if not any(m in out for m in ok_marks):
        reasons.append("无成功产出标志")
    return (not reasons), reasons


def _config_state():
    """返回 config 私密文件的真实在场情况（用于演练前后对账）。"""
    cfgdir = os.path.join(ROOT, "config")
    return {f: os.path.exists(os.path.join(cfgdir, f)) for f in SECRET_FILES}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=None, choices=list(TASKS) + ["all"])
    ap.add_argument("--no-push", action="store_true",
                    help="用 dry-run 运行（不真发消息）")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="跳过抓取（仅调试用；会把 pre/auction 判失败）")
    a = ap.parse_args()
    tasks = TASKS if (a.task in (None, "all")) else (a.task,)

    print("=" * 62)
    print("实盘级全链路演练 (E2E)：模拟 CI runner 环境逐 task 真跑")
    print("=" * 62)

    # —— 启动自检：先修历史遗留，再开始（否则演练的是残缺环境）——
    fixed, kept = _recover_orphans()
    if fixed:
        print("★ 启动自检：复原了历史遗留的真配置")
        for o, p in fixed:
            print(f"    {os.path.basename(o)} -> {os.path.basename(p)}")
    if kept:
        print("★ 启动自检：发现内容不一致的孤儿备份，**未自动覆盖**（需人工确认）")
        for o, p in kept:
            print(f"    ! {o}  vs  {p}")

    before = _config_state()
    env = _load_local_secrets()
    if a.no_push:
        env.pop("PUSHPLUS_TOKEN", None)
        env.pop("SERVERCHAN_KEY", None)
    print(f"演练前 config 在场: {before}")
    print(f"注入 Secrets 键: {sorted(env.keys())}")
    if not env.get("PUSHPLUS_TOKEN") and not env.get("SERVERCHAN_KEY"):
        print("  ⚠️ 无任何推送凭据 → 本次演练推送必为 dry-run，"
              "不能作为「推送可用」的证据！")
    print(f"演练前数据: {_db_dates()}")
    print(f"演练前账本尾: {_ledger_tail(3)}")
    print()

    results = []
    for t in tasks:
        moved = {}
        fetch = None
        try:
            moved = _hide_private(True)
            _prepare_site_users(env)
            if not a.skip_fetch:
                fetch = run_fetch(t, env)
                if fetch:
                    fr = "OK " if fetch["rc"] == 0 else "RC!"
                    print(f"[{fr}] fetch/{t:<8} rc={fetch['rc']:<4} "
                          f"{fetch['secs']:>7.1f}s")
                    if fetch["rc"] != 0:
                        el = [l for l in (fetch["err"] or "").splitlines()
                              if l.strip()][-3:]
                        for l in el:
                            print(f"        ! {l[:110]}")
            r = run_task(t, env)
        finally:
            _restore(moved)
        good, reasons = _judge(r, t)
        r["ok"] = good
        r["reasons"] = reasons
        results.append(r)
        flag = "OK " if good else "BAD"
        print(f"[{flag}] task={t:<8} rc={r['rc']:<4} {r['secs']:>7.1f}s")
        tail = [l for l in (r["out"] or "").splitlines() if l.strip()][-5:]
        for l in tail:
            print(f"        | {l[:110]}")
        if reasons:
            print(f"        !! 判定失败: {'; '.join(reasons)}")
        if r["rc"] != 0 and r["err"]:
            errl = [l for l in r["err"].splitlines() if l.strip()][-6:]
            for l in errl:
                print(f"        ! {l[:110]}")
        print()

    print("-" * 62)
    print("汇总")
    print("-" * 62)
    for r in results:
        print(f"  {r['task']:<8} rc={r['rc']:<4} {r['secs']:>7.1f}s  "
              f"{'PASS' if r['ok'] else 'FAIL'}  "
              f"{'' if r['ok'] else '; '.join(r['reasons'])}")
    print()
    print(f"演练后账本尾:")
    for row in _ledger_tail(8):
        print(f"   {row[0]} | {row[1]:<22} | {row[2]:<10} | {row[3]}")
    print()
    print(f"演练后数据: {_db_dates()}")

    # —— 收尾对账：config 必须与演练前完全一致，否则说明又留了坑 ——
    after = _config_state()
    drifted = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    if drifted:
        print()
        print("!! 配置状态漂移（演练污染了本机环境）:")
        for k, (b, af) in drifted.items():
            print(f"     {k}: {b} -> {af}")
    else:
        print(f"演练后 config 在场: {after}  （与演练前一致 ✓）")

    bad = [r for r in results if not r["ok"]]
    if drifted:
        bad.append({"task": "<config>", "rc": -1, "secs": 0, "ok": False,
                    "reasons": ["演练后配置状态漂移"]})
    print()
    print("E2E 结果:", "ALL_PASS ✓" if not bad else f"FAIL ✗ ({len(bad)} 项未通过)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
