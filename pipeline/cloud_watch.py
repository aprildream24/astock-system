# -*- coding: utf-8 -*-
"""云端自选管理网关（2026-09-15）。

用户诉求（原话）：
    「我能够在网络上单独添加自选的版本」
    「后续会发送到其他人，所以需要加入自选、观察、购入的特定用户特定访问」
    「不要命令行，要可视化入口」「我自己会进行添加删除」

## 为什么不能只做前端

站点是 GitHub Pages **纯静态**，无法写文件；且 `config/watch.json` 在
.gitignore 里（持仓名单是隐私红线，绝不入库）。所以「网页上加自选」
必须有一个**能落地的持久层**。三条可选路径：

  ① 提交 config/watch.json 回仓库 —— ❌ 破隐私红线（公开仓库会暴露自选）
  ② 更新仓库 Secret WATCH_CODES  —— ✅ 不公开、CI 已在读、可版本化追踪
  ③ 外部 KV（Workers/D1）        —— 需要额外账号，运维面变大

本模块实现 **路径 ②**：一个本地/内网跑的微型 HTTP 服务，
暴露 JSON API；前端（站点 owner 视图）调它完成增删。
服务持有 GitHub PAT（**只在服务端，绝不下发到浏览器**），
用 GitHub REST 的 libsodium sealed box 加密后写回 Secret。

## 用法

    python -m pipeline.cloud_watch --port 8771 --open
    # 或双击 tools/云端自选面板.bat

环境变量：
    GH_PAT         有 repo + secrets 写权限的 PAT（必需）
    GH_REPO        默认 aprildream24/astock-system
    WATCH_SECRET   默认 WATCH_CODES（写入哪个 Secret）

安全：
  · 默认只监听 127.0.0.1；要对外必须显式 --host 0.0.0.0 且务必设
    CLOUD_WATCH_TOKEN（否则任何人可改你的自选）。
  · PAT 永不出现在响应体里。
  · 股票代码严格校验 `(sh|sz)\\d{6}`。
"""
import argparse
import json
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core

CODE_RE = re.compile(r"^(sh|sz)\d{6}$")
DEFAULT_REPO = "aprildream24/astock-system"
DEFAULT_SECRET = "WATCH_CODES"

STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# GitHub Secrets 读写（libsodium sealed box）
# ---------------------------------------------------------------------------

def _gh(method, path, token, body=None, raw=False):
    """调 GitHub REST。raw=True 时返回 bytes（取公钥）。"""
    import urllib.request
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "astock-cloud-watch",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        b = r.read()
    return b if raw else (json.loads(b) if b else {})


def _seal(public_key_b64, secret_value):
    """用仓库公钥做 sealed box 加密（GitHub Secrets 要求）。

    公钥来源可能是 str（REST 返回）或 bytes（调用方自行编码），两种都收，
    避免调用点因为类型小事炸掉。
    """
    from nacl import encoding, public
    if isinstance(public_key_b64, bytes):
        public_key_b64 = public_key_b64.decode()
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    box = public.SealedBox(pk)
    if isinstance(secret_value, bytes):
        secret_value = secret_value.decode()
    return encoding.Base64Encoder().encode(
        box.encrypt(secret_value.encode())).decode()


def read_watch_secret(token, repo, secret=DEFAULT_SECRET):
    """读不到 Secret 值（GitHub 设计上不可读回）。

    所以真正的自选清单**以本机 config/watch.json 为准**（它由 CI 的
    WATCH_CODES 与本地合并而来），Secret 只负责「云端运行时也能拿到」。
    本函数仅探测 Secret 是否存在，供面板显示状态。
    """
    try:
        ks = _gh("GET", f"/repos/{repo}/actions/secrets/{secret}", token)
        return {"exists": True, "updated_at": ks.get("updated_at")}
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        if code == 404:
            return {"exists": False, "updated_at": None}
        raise


def write_watch_secret(codes, token, repo, secret=DEFAULT_SECRET):
    """把 codes 写入仓库 Secret（加密后 PUT）。返回更新后的 updated_at。"""
    pk = _gh("GET", f"/repos/{repo}/actions/secrets/public-key", token)
    sealed = _seal(pk["key"], json.dumps(codes, ensure_ascii=False))
    _gh("PUT", f"/repos/{repo}/actions/secrets/{secret}", token,
        {"encrypted_value": sealed, "key_id": pk["key_id"]})
    return read_watch_secret(token, repo, secret)


# ---------------------------------------------------------------------------
# 自选清单本地镜像（权威副本，面板直接读写）
# ---------------------------------------------------------------------------

def _watch_path():
    return os.path.join(core.CONFIG_DIR, "watch.json")


def load_watch():
    p = _watch_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            v = json.load(f)
        return [c for c in v if isinstance(c, str) and CODE_RE.match(c)] \
            if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def save_watch(codes):
    p = _watch_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(codes), f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, p)      # 原子替换


def normalize_code(raw):
    """接受 600519 / sh600519 / SH600519 / 000001 → sh600519 / sz000001。

    规则：6 开头=沪、0/3 开头=深；已是带前缀形态则规范化小写。
    """
    s = str(raw or "").strip().lower().replace(" ", "")
    if CODE_RE.match(s):
        return s
    if re.match(r"^\d{6}$", s):
        return ("sh" if s.startswith("6") else "sz") + s
    return None


# ---------------------------------------------------------------------------
# HTTP 面板
# ---------------------------------------------------------------------------

def _token_ok(headers):
    """对外暴露时校验访问令牌（仅监听本地时可省略）。"""
    need = os.environ.get("CLOUD_WATCH_TOKEN", "").strip()
    if not need:
        return True
    got = (headers.get("X-Auth-Token") or "").strip()
    return got == need


PANEL_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Astra 云端自选管理</title>
<style>
:root{--bg:#15181e;--card:#1d222b;--bd:#2b313d;--fg:#e6ebf5;--dim:#8a94a6;
--buy:#ff6b5e;--sell:#4ecf8e;--hold:#6ab0ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;padding:24px}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;
padding:16px 18px;margin-bottom:16px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input{flex:1;min-width:180px;background:#12151b;border:1px solid var(--bd);
color:var(--fg);border-radius:8px;padding:10px 12px;font-size:15px}
button{background:var(--buy);border:0;color:#fff;border-radius:8px;
padding:10px 18px;font-size:15px;cursor:pointer;font-weight:600}
button.ghost{background:#2b313d}
button:active{opacity:.8}
ul{list-style:none;margin:0;padding:0}
li{display:flex;justify-content:space-between;align-items:center;
padding:10px 0;border-bottom:1px solid var(--bd)}
li:last-child{border-bottom:0}
.code{font-family:Consolas,monospace;font-size:15px;letter-spacing:.5px}
.del{background:transparent;color:var(--sell);border:1px solid var(--sell);
padding:5px 12px;font-size:13px;border-radius:6px}
.hint{color:var(--dim);font-size:12.5px;margin-top:10px}
.ok{color:var(--sell)}.bad{color:var(--buy)}
.empty{color:var(--dim);padding:14px 0}
</style></head><body><div class="wrap">
<h1>Astra 云端自选管理</h1>
<div class="sub">增删后自动同步到云端（GitHub Secret），下一个交易时点生效</div>

<div class="card">
  <div class="row">
    <input id="inp" placeholder="输入股票代码，如 600519 或 sh600519">
    <button id="add">加入自选</button>
  </div>
  <div class="hint" id="msg"></div>
</div>

<div class="card">
  <div class="row" style="justify-content:space-between">
    <b>当前自选 <span id="cnt">0</span> 只</b>
    <button class="ghost" id="sync">立即同步云端</button>
  </div>
  <ul id="list"></ul>
</div>

<div class="card">
  <b>云端状态</b>
  <div id="cloud" class="hint"></div>
</div>

<script>
const $=s=>document.querySelector(s);
function msg(t,cls){const m=$("#msg");m.textContent=t;m.className="hint "+(cls||"");}
async function call(path,body){
  const r=await fetch(path,{method:body?"POST":"GET",
    headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});
  return await r.json();
}
function render(codes){
  $("#cnt").textContent=codes.length;
  const ul=$("#list");ul.innerHTML="";
  if(!codes.length){ul.innerHTML='<div class="empty">还没有自选股，输入代码添加</div>';return;}
  codes.forEach(c=>{
    const li=document.createElement("li");
    li.innerHTML=`<span class="code">${c}</span>`;
    const b=document.createElement("button");
    b.className="del";b.textContent="删除";
    b.onclick=async()=>{const j=await call("/api/watch/remove",{code:c});
      render(j.codes);msg(j.message,j.ok?"ok":"bad");};
    li.appendChild(b);ul.appendChild(li);
  });
}
async function refresh(){
  const j=await call("/api/watch");
  render(j.codes);
  $("#cloud").innerHTML = j.cloud
    ? `Secret <b>${j.secret}</b>：${j.cloud.exists?"已配置，更新于 "+j.cloud.updated_at:"<span class=bad>未配置</span>"}`
    : "未配置 GH_PAT，仅本地生效（无法同步云端）";
}
$("#add").onclick=async()=>{
  const v=$("#inp").value.trim(); if(!v) return;
  const j=await call("/api/watch/add",{code:v});
  if(j.ok){$("#inp").value="";render(j.codes);}
  msg(j.message,j.ok?"ok":"bad");
};
$("#inp").addEventListener("keydown",e=>{if(e.key==="Enter")$("#add").click();});
$("#sync").onclick=async()=>{const j=await call("/api/watch/sync",{});
  msg(j.message,j.ok?"ok":"bad");refresh();};
refresh();
</script></div></body></html>"""


def make_handler(repo, secret_name):
    class H(BaseHTTPRequestHandler):
        server_version = "AstockCloudWatch/1.0"

        def log_message(self, *a):      # 安静模式
            pass

        def _send(self, obj, code=200):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type,X-Auth-Token")
            self.end_headers()
            self.wfile.write(b)

        def _html(self, text):
            b = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:  # noqa: BLE001
                return {}

        def _cloud(self):
            tok = os.environ.get("GH_PAT", "").strip()
            if not tok:
                return None
            try:
                return read_watch_secret(tok, repo, secret_name)
            except Exception as e:  # noqa: BLE001
                return {"exists": None, "error": f"{type(e).__name__} {e}"}

        def do_OPTIONS(self):
            self._send({})

        def do_GET(self):
            if self.path.startswith("/api/watch"):
                with STATE_LOCK:
                    codes = load_watch()
                self._send({"codes": codes, "cloud": self._cloud(),
                            "secret": secret_name, "repo": repo})
                return
            self._html(PANEL_HTML)

        def do_POST(self):
            if not _token_ok(self.headers):
                self._send({"ok": False, "message": "未授权"}, 401)
                return
            body = self._body()
            path = self.path.rstrip("/")
            with STATE_LOCK:
                codes = load_watch()
                if path.endswith("/add"):
                    code = normalize_code(body.get("code"))
                    if not code:
                        self._send({"ok": False, "codes": codes,
                                    "message": "代码格式不对，应为 6 位数字"
                                               "（如 600519）或 sh/sz+6位"})
                        return
                    if code in codes:
                        self._send({"ok": False, "codes": codes,
                                    "message": f"{code} 已在自选中"})
                        return
                    codes.append(code)
                    save_watch(codes)
                    out = {"ok": True, "codes": codes,
                           "message": f"已加入 {code}（{len(codes)} 只）"}
                elif path.endswith("/remove"):
                    code = normalize_code(body.get("code"))
                    if code not in codes:
                        self._send({"ok": False, "codes": codes,
                                    "message": f"{code} 不在自选中"})
                        return
                    codes = [c for c in codes if c != code]
                    save_watch(codes)
                    out = {"ok": True, "codes": codes,
                           "message": f"已删除 {code}（剩 {len(codes)} 只）"}
                elif path.endswith("/sync"):
                    tok = os.environ.get("GH_PAT", "").strip()
                    if not tok:
                        self._send({"ok": False, "codes": codes,
                                    "message": "未配置 GH_PAT，无法同步云端"})
                        return
                    try:
                        st = write_watch_secret(codes, tok, repo, secret_name)
                        out = {"ok": True, "codes": codes,
                               "message": f"已同步 {len(codes)} 只到云端 "
                                          f"Secret {secret_name}",
                               "cloud": st}
                    except Exception as e:  # noqa: BLE001
                        self._send({"ok": False, "codes": codes,
                                    "message": f"同步失败：{type(e).__name__} {e}"})
                        return
                else:
                    self._send({"ok": False, "message": "未知接口"}, 404)
                    return
            # 增删后自动尝试同步（失败不阻断本地生效）
            if "cloud" not in out:
                tok = os.environ.get("GH_PAT", "").strip()
                if tok:
                    try:
                        write_watch_secret(codes, tok, repo, secret_name)
                        out["message"] += "，已同步云端"
                    except Exception as e:  # noqa: BLE001
                        out["message"] += f"（云端同步失败：{type(e).__name__}）"
                else:
                    out["message"] += "（本地生效；未配 GH_PAT 无法同步云端）"
            self._send(out)

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--repo", default=os.environ.get("GH_REPO", DEFAULT_REPO))
    ap.add_argument("--secret",
                    default=os.environ.get("WATCH_SECRET", DEFAULT_SECRET))
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()

    if a.host not in ("127.0.0.1", "localhost") \
            and not os.environ.get("CLOUD_WATCH_TOKEN"):
        print("!! 拒绝启动：对外监听必须设置 CLOUD_WATCH_TOKEN，"
              "否则任何人都能改你的自选。")
        raise SystemExit(2)

    srv = ThreadingHTTPServer((a.host, a.port), make_handler(a.repo, a.secret))
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0',) else a.host}:{a.port}/"
    print(f"云端自选管理面板: {url}")
    print(f"  仓库 = {a.repo}    Secret = {a.secret}")
    print(f"  GH_PAT = {'已配置' if os.environ.get('GH_PAT') else '未配置（仅本地）'}")
    if a.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
