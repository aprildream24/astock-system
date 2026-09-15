# -*- coding: utf-8 -*-
"""自选 / 持仓 / 用户权限 管理服务（本地，2026-09-15）。

用户要求「不要命令行、要可视化入口」「我自己会进行添加删除」。
本服务起一个 localhost 网页：增删自选股、持仓票、以及**分用户的访问权限**
（自选/观察/购入 各自授权给特定人）。

用法（双击 tools/管理面板.bat 亦可）：
    python -m pipeline.admin            # 默认 127.0.0.1:8770
    python -m pipeline.admin --port 9000 --open

安全：只监听 127.0.0.1（不对外），写操作限定在 config/ 下的白名单文件。
"""
import argparse
import json
import os
import re
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core, users as users_mod

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = core.CONFIG_DIR
# 仅允许读写这三个文件（白名单，防越权路径）
ALLOWED = {"watch.json", "holdings.json", "users.json"}
CODE_RE = re.compile(r"^(sh|sz)\d{6}$")
HOST = "127.0.0.1"


def _path(name):
    if name not in ALLOWED:
        raise ValueError(f"不允许访问 {name}")
    return os.path.join(CONFIG, name)


def read_json(name, default):
    p = _path(name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def write_json(name, obj):
    p = _path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, p)      # 原子替换：写一半被打断不会毁掉原文件


def state():
    """面板初始状态：自选 / 持仓 / 用户。"""
    return {
        "watch": read_json("watch.json", []),
        "holdings": read_json("holdings.json", []),
        "users": _users_for_ui(),
        "roles": [{"id": r, "label": users_mod.ROLE_LABELS[r]}
                  for r in users_mod.ROLES],
        "config_dir": CONFIG,
    }


def _users_for_ui():
    """用户列表（**不含口令明文**：只回传是否已设口令 + 角色）。"""
    raw = read_json("users.json", {})
    out = []
    if isinstance(raw, dict) and isinstance(raw.get("users"), list):
        for it in raw["users"]:
            out.append({"id": it.get("id"), "name": it.get("name") or it.get("id"),
                        "roles": it.get("roles") or [],
                        "has_pass": bool(it.get("pass"))})
    else:
        # 旧扁平形态 → 转换展示
        for uid, pwd in (raw or {}).items():
            if uid.startswith("_") or not isinstance(pwd, str):
                continue
            out.append({"id": uid, "name": uid,
                        "roles": users_mod.LEGACY_ROLE_MAP.get(uid, ["observe"]),
                        "has_pass": bool(pwd)})
    return out


def _load_users_raw():
    """读原始 users.json 并统一成新形态 dict（便于增删改）。"""
    raw = read_json("users.json", {})
    if isinstance(raw, dict) and isinstance(raw.get("users"), list):
        return {"users": raw["users"]}
    conv = []
    for uid, pwd in (raw or {}).items():
        if uid.startswith("_") or not isinstance(pwd, str) or not pwd:
            continue
        conv.append({"id": uid, "name": uid, "pass": pwd,
                     "roles": users_mod.LEGACY_ROLE_MAP.get(uid, ["observe"])})
    return {"users": conv}


def save_watch(codes):
    clean, bad = [], []
    for c in codes or []:
        c = str(c or "").strip().lower()
        if not c:
            continue
        if CODE_RE.match(c):
            if c not in clean:
                clean.append(c)
        else:
            bad.append(c)
    if bad:
        return {"ok": False, "error": "代码格式错误：" + "、".join(bad)
                + "（应为 sh/sz + 6 位数字）"}
    write_json("watch.json", clean)
    return {"ok": True, "count": len(clean)}


def save_holdings(items):
    clean, bad = [], []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip().lower()
        if not code:
            continue
        if not CODE_RE.match(code):
            bad.append(code)
            continue
        o = {"code": code}
        if it.get("name"):
            o["name"] = str(it["name"])
        if it.get("buy_date"):
            o["buy_date"] = str(it["buy_date"])
        for k in ("buy_price", "shares", "stop"):
            v = it.get(k)
            if v not in (None, ""):
                try:
                    o[k] = float(v)
                except (TypeError, ValueError):
                    pass
        clean.append(o)
    if bad:
        return {"ok": False, "error": "代码格式错误：" + "、".join(bad)}
    write_json("holdings.json", clean)
    return {"ok": True, "count": len(clean)}


def save_users(payload):
    """保存用户：{users:[{id,name,pass,roles}]}。pass 为空时保留原口令。"""
    incoming = (payload or {}).get("users") or []
    old = {u["id"]: u for u in _load_users_raw()["users"]}
    out, seen = [], set()
    for it in incoming:
        uid = str(it.get("id") or "").strip()
        if not uid:
            continue
        if not re.match(r"^[A-Za-z0-9_\-]{2,32}$", uid):
            return {"ok": False, "error": f"用户名不合法：{uid}（2-32 位字母/数字/_/-）"}
        if uid in seen:
            return {"ok": False, "error": f"用户名重复：{uid}"}
        seen.add(uid)
        roles = [r for r in (it.get("roles") or []) if r in users_mod.ROLES]
        if not roles:
            return {"ok": False, "error": f"{uid} 至少需要一个角色"}
        pwd = it.get("pass")
        if not pwd:
            pwd = (old.get(uid) or {}).get("pass")
        if not pwd:
            return {"ok": False, "error": f"{uid} 未设置口令"}
        if len(str(pwd)) < 8:
            return {"ok": False, "error": f"{uid} 口令至少 8 位"}
        out.append({"id": uid, "name": it.get("name") or uid,
                    "pass": str(pwd), "roles": roles})
    if not any("all" in u["roles"] for u in out):
        return {"ok": False, "error": "必须至少保留一个 all 角色用户（管理员）"}
    write_json("users.json", {"users": out})
    return {"ok": True, "count": len(out)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):      # 静音默认访问日志
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = (json.dumps(obj, ensure_ascii=False).encode()
                if ctype == "application/json" else obj)
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            p = os.path.join(os.path.dirname(HERE), "tools",
                             "manage_panel.html")
            if not os.path.exists(p):
                return self._send({"error": "缺少 tools/manage_panel.html"},
                                  404)
            with open(p, "rb") as f:
                return self._send(f.read(), 200, "text/html")
        if path == "/api/state":
            return self._send(state())
        return self._send({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/watch":
                return self._send(save_watch(body.get("codes")))
            if path == "/api/holdings":
                return self._send(save_holdings(body.get("items")))
            if path == "/api/users":
                return self._send(save_users(body))
        except Exception as e:  # noqa: BLE001
            return self._send({"ok": False, "error": f"{type(e).__name__}: {e}"},
                              500)
        return self._send({"error": "not found"}, 404)


def serve(port=8770, open_browser=True):
    srv = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    print(f"[admin] 管理面板已启动：{url}")
    print(f"[admin] 配置目录：{CONFIG}")
    print("[admin] 仅监听本机；关闭此窗口即停止服务。")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[admin] 已停止")
    finally:
        srv.server_close()


def main():
    ap = argparse.ArgumentParser(description="自选/持仓/用户权限 管理面板")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    serve(a.port, not a.no_open)


if __name__ == "__main__":
    main()
