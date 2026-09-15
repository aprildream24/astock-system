# -*- coding: utf-8 -*-
"""每日实盘体检（Daily Health Check）——给用户看的「今天到底行不行」。

设计目标（2026-09-15 用户质疑「每天说没问题，实盘就出问题」）：
不再让用户相信"我测过了"，而是**用产出的物证说话**：
  ① CI 是否真的成功（不是"跑过了"，是 conclusion==success 且关键步骤没 skipped）；
  ② 今天的数据是否真的入库（klines / 快照 / 涨停池的 max(date)）；
  ③ 推送是否真的发出（账本里今天有没有 status=sent，走的哪个通道）；
  ④ 站点是否真的更新（线上 users.json / owner.bin 可访问 + 日期是最新）。

输出：一张人话结论 + 明细。任何一项红灯 → 明确写「今天有什么问题」。

用法：
    python tools/daily_check.py               # 人读
    python tools/daily_check.py --json        # 机读
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SITE = "https://aprildream24.github.io/astock-system/"


def _api(token, path):
    import urllib.error
    url = "https://api.github.com/repos/aprildream24/astock-system" + path
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}", "User-Agent": "health",
        "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"msg": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:  # noqa: BLE001
        return 0, {"msg": f"{type(e).__name__}: {e}"}


def _token():
    p = r"C:\Users\Basshunter-j\AppData\Local\Temp\astock_gh_token.txt"
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    return os.environ.get("GH_TOKEN") or ""


def check_ci(token, today):
    """今天有没有一次**真成功**的 run（关键步骤不许 skipped）。"""
    out = {"ok": False, "detail": "", "runs": []}
    if not token:
        out["detail"] = "无 token，跳过"
        return out
    st, d = _api(token, "/actions/runs?per_page=12")
    if st != 200:
        out["detail"] = f"取 runs 失败 {st}"
        return out
    # ⚠️ 步骤名要**子串匹配**：实际名是「构建+推送（WxPusher/PushPlus 多渠道）」，
    # 精确匹配会静默匹配不到 → 列永远空白（2026-09-15 自己踩过）。
    KEY_STEPS = ("回归自检", "构建+推送", "构建加密站点", "收盘复盘")
    for r in d.get("workflow_runs", []):
        if not r["created_at"].startswith(today):
            continue
        rec = {"id": r["id"], "conclusion": r.get("conclusion"),
               "status": r["status"], "created_at": r["created_at"]}
        st2, j = _api(token, f"/actions/runs/{r['id']}/jobs")
        steps = {}
        if st2 == 200:
            for job in j.get("jobs", []):
                for s in job.get("steps", []):
                    steps[s["name"]] = s.get("conclusion")

        def _find(needle):
            for name, concl in steps.items():
                if needle in name:
                    return concl
            return None

        rec["steps"] = {k: _find(k) for k in KEY_STEPS if _find(k) is not None}
        rec["has_push_step"] = _find("构建+推送") is not None
        rec["push_step"] = _find("构建+推送")
        out["runs"].append(rec)
        # 判定：整个 run success **且**已出现的关键步骤全 success
        # （skipped / failure 都不算；缺失的步骤不计入分母）
        if r.get("conclusion") == "success" and rec["steps"] and \
                all(v == "success" for v in rec["steps"].values()):
            out["ok"] = True
            out["detail"] = f"run {r['id']} 成功，关键步骤全绿"
    if not out["runs"]:
        out["detail"] = "今天还没有 run"
    elif not out["ok"]:
        greens = [x for x in out["runs"] if x["conclusion"] == "success"]
        if greens:
            bad_steps = set()
            for g in greens:
                for k, v in (g.get("steps") or {}).items():
                    if v != "success":
                        bad_steps.add(f"{k}={v}")
            out["detail"] = (f"有 {len(greens)} 次 run 成功，但关键步骤异常："
                             + ("; ".join(sorted(bad_steps)) or "构建+推送未执行"))
        else:
            out["detail"] = f"今天 {len(out['runs'])} 次 run 均未成功"
    return out


def check_data(today):
    """数据是否入库 + **闸门会怎么说**。

    2026-09-15 教训：系统曾把"还没抓到今天的数据"报成「非交易日」，
    误导排查方向。所以这里直接打印闸门的真实 reason。
    """
    p = os.path.join(ROOT, "cache", "market.db")
    out = {"ok": False, "detail": "", "dates": {}, "gate": {}}
    if not os.path.exists(p):
        out["detail"] = "无 cache/market.db"
        return out
    con = sqlite3.connect(p)
    cur = con.cursor()
    for t in ("klines", "candidate_snapshots", "zt_pool"):
        try:
            cur.execute(f"select max(date) from {t}")
            out["dates"][t] = cur.fetchone()[0]
        except Exception as e:  # noqa: BLE001
            out["dates"][t] = f"ERR {e}"
    # 闸门诊断：今天是不是交易日？数据够不够构建？
    try:
        from pipeline import core
        from pipeline import fetch_daily as fd
        real = core.is_real_trade_day(today)
        certain, why = core.is_trading_day_cross(con, today)
        ready, ready_why = fd.data_ready_for(con, today)
        out["gate"] = {"is_trade_day": real, "certain": certain,
                       "why": why, "ready": ready, "ready_why": ready_why}
        # 数据"就绪"= 日历说交易日 且 闸门放行
        out["ok"] = bool(real and certain and ready)
        if real and not ready:
            out["detail"] = f"{ready_why}"
        else:
            out["detail"] = (" / ".join(f"{k}={v}"
                                        for k, v in out["dates"].items()))
    except Exception as e:  # noqa: BLE001
        out["gate"] = {"error": f"{type(e).__name__}: {e}"}
        out["ok"] = any(isinstance(v, str) and v.startswith("20")
                        for v in out["dates"].values())
        out["detail"] = " / ".join(f"{k}={v}" for k, v in out["dates"].items())
    con.close()
    return out


def check_push(today, token=""):
    """推送是否真的发出。

    ⚠️ 只查本地 dist/push_ledger.json 是不够的——用户电脑常常不开机，
    本地镜像可能停在几天前。真正权威的是 **CI 跑完后提交回仓库的账本**
    （dist/push_ledger.json 已入 CI cache 并随构建产物提交）。
    所以这里两处都查：远端优先，本地作补充，并明确标注来源。
    """
    out = {"ok": False, "detail": "", "sent_today": [], "source": ""}

    def _scan(d, src):
        hits = []
        for v in d.values():
            ts = v.get("ts", "")
            if ts.startswith(today) and v.get("status") == "sent":
                hits.append({"mode": v.get("mode"), "ts": ts,
                             "channels": v.get("channels", {}), "src": src})
        return hits

    # ① 远端仓库的账本（权威）
    remote_hits, remote_note = [], ""
    if token:
        st, d = _api(token, "/contents/dist/push_ledger.json")
        if st == 200 and d.get("content"):
            try:
                import base64
                raw = base64.b64decode(d["content"]).decode("utf-8")
                remote_hits = _scan(json.loads(raw), "remote")
                remote_note = "远端账本已读"
            except Exception as e:  # noqa: BLE001
                remote_note = f"远端账本解析失败 {type(e).__name__}"
        else:
            remote_note = f"远端账本取用失败 {st}"

    # ② 本地镜像（补充）
    local_hits = []
    p = os.path.join(ROOT, "dist", "push_ledger.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                local_hits = _scan(json.load(f), "local")
        except Exception:  # noqa: BLE001
            pass

    seen, merged = set(), []
    for h in remote_hits + local_hits:
        k = (h["ts"], h["mode"])
        if k not in seen:
            seen.add(k)
            merged.append(h)

    out["sent_today"] = merged
    # ⚠️ 2026-09-15 教训：`selftest*` / `channel_test*` 是我自己发的测试消息，
    # 绝不是"生产推送成功"的证据。曾把它们计入 sent → 误判 OK（假绿）。
    REAL_PREFIXES = ("build_", "narrative", "watch_advice", "review")
    real = [h for h in merged
            if str(h.get("mode", "")).startswith(REAL_PREFIXES)]
    self_test = [h for h in merged if h not in real]
    out["real"] = real
    out["self_test"] = self_test
    out["ok"] = bool(real)
    srcs = []
    if remote_hits:
        srcs.append("远端")
    if local_hits:
        srcs.append("本地")
    out["source"] = "+".join(srcs) if srcs else "无"
    if real:
        out["detail"] = (f"今天 {len(real)} 条**生产**推送已发"
                         f"（另有 {len(self_test)} 条自测，不计）"
                         f"（来源 {out['source']}）")
    elif self_test:
        out["detail"] = (f"今天**没有任何生产推送**——只有 {len(self_test)} 条"
                         f"自测消息（{','.join(h['mode'] for h in self_test[:3])}）"
                         f"，不能当作推送正常！{remote_note}")
    else:
        out["detail"] = (f"今天无 sent 记录（来源 无；{remote_note}）"
                         if remote_note else "今天无 sent 记录")
    return out


def check_site(owner_pw=""):
    """线上站点：门禁存在、无口令泄漏、密文能解开并取到最新日期。

    口令来源（任一即可）：
      · 环境变量 ASTOCK_OWNER_PW
      · 本地 config/users.json 里 id==owner 的 pass（本机开发时）
    """
    out = {"ok": False, "detail": "", "date": None, "note": ""}
    if not owner_pw:
        try:
            with open(os.path.join(ROOT, "config", "users.json"),
                      encoding="utf-8") as f:
                u = json.load(f)
            for x in u.get("users", []):
                if x.get("id") == "owner" and x.get("pass"):
                    owner_pw = x["pass"]
                    break
        except Exception:  # noqa: BLE001
            pass
    try:
        req = urllib.request.Request(SITE + "users.json",
                                     headers={"User-Agent": "health"})
        with urllib.request.urlopen(req, timeout=40) as r:
            idx = json.loads(r.read())
        users = idx.get("users", [])
        uids = [u.get("id") for u in users]
        leaks = [u.get("id") for u in users if "pass" in u or "password" in u]
        req2 = urllib.request.Request(SITE + "data/owner.bin",
                                      headers={"User-Agent": "health"})
        with urllib.request.urlopen(req2, timeout=60) as r2:
            blob = r2.read()
        from pipeline import publish
        if owner_pw:
            try:
                data = json.loads(
                    publish.decrypt_bytes(blob, owner_pw).decode("utf-8"))
                out["date"] = data.get("date")
            except Exception as e:  # noqa: BLE001
                out["note"] = f"密文解密失败 {type(e).__name__}"
        else:
            out["note"] = "无口令，未验解密"
        # 门禁在 + 无泄漏 + 密文体积像样 + 能解出日期 → 才算真的可用
        out["ok"] = bool(uids) and not leaks and len(blob) > 1000 \
            and bool(out["date"])
        out["detail"] = (f"users={uids} owner.bin={len(blob)}B "
                         f"date={out['date']} "
                         f"口令泄漏={'有!' if leaks else '无'}"
                         + (f" {out['note']}" if out["note"] else ""))
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"{type(e).__name__}: {e}"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    import datetime
    today = a.date or datetime.date.today().isoformat()
    token = _token()

    ci = check_ci(token, today)
    data = check_data(today)
    push = check_push(today, token)
    site = check_site(os.environ.get("ASTOCK_OWNER_PW", ""))
    hard = {"ci": ci, "push": push}
    soft = {"data": data, "site": site}
    all_ok = all(v["ok"] for v in hard.values())
    rep = {"date": today, "verdict": "OK" if all_ok else "PROBLEM",
           "ci": ci, "data": data, "push": push, "site": site}
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if all_ok else 1

    print("=" * 60)
    print(f"每日实盘体检 · {today}")
    print("=" * 60)
    for name, v, hard_ in (("CI 运行", ci, True), ("数据入库", data, False),
                           ("推送发送", push, True), ("线上站点", site, False)):
        mark = "OK " if v["ok"] else ("BAD" if hard_ else "如常")
        print(f"[{mark}] {name:<8} {v['detail']}")
    print()
    if data.get("gate") and "error" not in data["gate"]:
        g = data["gate"]
        print("闸门诊断（今天到底该不该构建）：")
        print(f"   客观交易日     : {g['is_trade_day']}")
        print(f"   交易日交叉确认 : {g['certain']}  ({g['why']})")
        print(f"   数据就绪       : {g['ready']}  ({g['ready_why']})")
        print()
    if ci.get("runs"):
        print("今天各次 run：")
        for r in ci["runs"]:
            steps = " ".join(f"{k}={v}" for k, v in (r.get("steps") or {}).items())
            print(f"   {r['created_at'][11:16]}  {r['conclusion']:<10} {steps}")
    print()
    if push.get("real"):
        print("今天已发出的生产推送：")
        for s in push["real"]:
            print(f"   {s['ts'][11:]}  {s['mode']:<22} {s['channels']}")
    if push.get("self_test"):
        print("（自测消息，不计入生产推送）：")
        for s in push["self_test"]:
            print(f"   {s['ts'][11:]}  {s['mode']:<22} {s['channels']}")
    print()
    print("结论：", "今天一切正常 ✓" if all_ok else "今天存在问题 ✗ —— 见上面 BAD 项")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
