# -*- coding: utf-8 -*-
"""部署前自检：填完 key 后跑一遍，全部 PASS 即可上线。

用法：python tools/setup_check.py
退出码：0=可部署；1=存在 FAIL 项。
"""
import json
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACEHOLDER = re.compile(r"<在此填入|<[A-Za-z_ ]+>")


def _check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return ok


def main():
    print("== Astra 收盘观察系统 · 部署自检 ==\n")
    fails = 0
    sys.path.insert(0, BASE)

    # 1. Python 版本
    v = sys.version_info
    ok = v >= (3, 10)
    fails += not _check("Python ≥ 3.10", ok, f"当前 {v.major}.{v.minor}")
    fails += not ok

    # 2. 推送密钥（缺失=WARN 可 dry-run；填了占位符=FAIL）
    cfg_path = os.path.join(BASE, "config", "notify.json")
    if not os.path.exists(cfg_path):
        print("  [WARN] config/notify.json 不存在 → 推送将 dry-run（只记账本不发送）")
    else:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        accts = cfg.get("wxpusher_accounts") or []
        if accts:
            ok = all(a.get("app_token") and not PLACEHOLDER.search(str(a.get("app_token")))
                     and (a.get("uids") or a.get("app_ids"))
                     for a in accts)
            fails += not _check(f"WxPusher 账户 ×{len(accts)} 已填",
                                ok, "、".join(a.get("name", "?") for a in accts))
        else:
            print("  [WARN] 未配置 WxPusher 账户 → dry-run"
                  "（CI 部署可用 Secret WXPUSHER_CONF 注入）")
        primary = cfg.get("primary_channel") or "wxpusher"
        _check(f"主通道 = {primary}（防混淆标识 push_tag = "
               f"{cfg.get('push_tag') or 'Astra'}）", True)
        if primary == "pushplus" and not cfg.get("pushplus_token"):
            print("  [WARN] primary_channel=pushplus 但 pushplus_token 未填 → dry-run")
        if primary == "wxpusher" and not accts \
                and not os.environ.get("WXPUSHER_CONF"):
            print("  [WARN] 主通道 wxpusher 但未配置账户 → dry-run")
        for k in ("serverchan_key", "pushplus_token"):
            val = cfg.get(k, "")
            if not val:
                continue
            if PLACEHOLDER.search(val):
                fails += not _check(f"{k} 已填", False, "还是占位符")
            else:
                _check(f"{k}（备用通道）已填", True)

    # 3. 站点口令
    users_path = os.path.join(BASE, "config", "users.json")
    if not os.path.exists(users_path):
        fails += not _check("config/users.json", False,
                            "缺失：复制 users.example.json 并改口令")
    else:
        with open(users_path, encoding="utf-8") as f:
            users = json.load(f)
        ok = bool(users) and all(not PLACEHOLDER.search(str(p)) for p in users.values())
        fails += not _check("users.json 口令已填（含 owner）", ok,
                            f"用户: {list(users)}")

    # 4. 技巧基线
    r = subprocess.run([sys.executable, "-X", "utf8",
                        os.path.join(BASE, "tools", "check_strategy_lock.py")],
                       capture_output=True, text=True)
    fails += not _check("技巧注册表基线", r.returncode == 0, r.stdout.strip())

    # 5. 回归测试
    r = subprocess.run([sys.executable, "-X", "utf8",
                        os.path.join(BASE, "tests", "run_regression.py")],
                       capture_output=True, text=True, cwd=BASE)
    m = re.search(r"PASS=(\d+) FAIL=(\d+)", r.stdout)
    fails += not _check("回归测试", r.returncode == 0,
                        m.group(0) if m else r.stdout[-200:])

    # 6. 数据库
    db = os.path.join(BASE, "cache", "market.db")
    if os.path.exists(db):
        import sqlite3
        con = sqlite3.connect(db)
        n = con.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
        con.close()
        _check("数据库就绪", True, f"klines {n} 行" + ("" if n else "（空库，先跑 fetch_daily）"))
    else:
        print("  [WARN] cache/market.db 不存在 → 首次运行先执行 "
              "python -m pipeline.fetch_daily")

    print(f"\n结论: {'✅ 可部署' if fails == 0 else '❌ 存在 FAIL 项，先修复'}")
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
