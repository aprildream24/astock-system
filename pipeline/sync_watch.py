# -*- coding: utf-8 -*-
"""云端自选同步接收端（2026-09-15）。

## 为什么需要它

用户诉求原话：「我能够在网络上单独添加自选的版本」——**在网页上加自选**。
站点是 GitHub Pages 纯静态，写不了文件；`config/watch.json` 在 .gitignore
（自选名单是隐私红线，绝不入库）。

最初设计让浏览器端用 libsodium-wrappers 在客户端做 `crypto_box_seal`
加密再 PUT GitHub Secrets。实测此路不通：
  · npm 的 libsodium-wrappers 只有 CommonJS 形态，裸 <script> 引不进浏览器；
  · 手工拼接 libsodium.js + libsodium-wrappers.js 后 wasm 内嵌 basE64
    解码路径报 `Aborted(Error: Converting base64 string to bytes failed.)`。
浏览器端做 GitHub Secret 加密属于**把服务端职责塞进前端**，脆弱且没必要。

## 本方案（零前端加密）

  浏览器 owner 视图 → `POST /repos/{repo}/actions/workflows/stock.yml/dispatches`
      body: {"ref":"main","inputs":{"task":"watch-sync","codes":"sh600519,sz000001"}}
  → GitHub Actions 起一个 run
  → 本脚本在 runner 内用 **Python PyNaCl** 把 codes 写进 Secret WATCH_CODES
     （PyNaCl 已在依赖里，CI 侧 100% 可靠）
  → 下一个交易时点，stock.yml 的 fetch/build 从 WATCH_CODES 读到新自选

浏览器只发一个带文本 inputs 的 HTTPS 请求——**不需要任何加密库**。
PAT 仍只存在于 owner 的密文数据包里（`_admin.token`），Guest 包里为 None。

## 用法

    # CI 里（stock.yml task=watch-sync 步骤）
    python -m pipeline.sync_watch --codes "sh600519,sz000001"

    # 也支持从环境变量读（dispatch input 同名注入）
    WATCH_CODES_IN="sh600519,sz000001" python -m pipeline.sync_watch

环境变量：
    GH_PAT / GITHUB_TOKEN   有 secrets 写权限的令牌（CI 默认提供 GITHUB_TOKEN，
                            但**写 Secrets 需要 PAT**，故用 secret GH_PAT）
    GH_REPO                 默认 aprildream24/astock-system
    WATCH_SECRET            默认 WATCH_CODES
"""
import argparse
import json
import os
import re
import sys

CODE_RE = re.compile(r"^(sh|sz)\d{6}$")
DEFAULT_REPO = "aprildream24/astock-system"
DEFAULT_SECRET = "WATCH_CODES"

# 上限：自选不是股票池，防止误传整表把 Secret 撑爆
MAX_CODES = 200


def normalize_code(raw):
    """600519 / sh600519 / SH600519 / 000001 → sh600519 / sz000001。"""
    s = str(raw or "").strip().lower().replace(" ", "")
    if CODE_RE.match(s):
        return s
    if re.match(r"^\d{6}$", s):
        return ("sh" if s.startswith("6") else "sz") + s
    return None


def parse_codes(text):
    """把逗号/空格/换行/JSON 数组统一成规范化去重列表（保序）。"""
    if not text:
        return []
    items = []
    t = text.strip()
    if t.startswith("["):
        try:
            v = json.loads(t)
            if isinstance(v, list):
                items = v
        except Exception:  # noqa: BLE001
            items = []
    if not items:
        items = re.split(r"[,;\s]+", t)
    out, seen = [], set()
    for it in items:
        c = normalize_code(it)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:MAX_CODES]


def _gh(method, path, token, body=None):
    import urllib.request
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "astock-sync-watch",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        b = r.read()
    return json.loads(b) if b else {}


def seal(public_key_b64, secret_value):
    """libsodium sealed box（GitHub Secrets 要求）。PyNaCl 已在依赖中。"""
    from nacl import encoding, public
    if isinstance(public_key_b64, bytes):
        public_key_b64 = public_key_b64.decode()
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    box = public.SealedBox(pk)
    if isinstance(secret_value, bytes):
        secret_value = secret_value.decode()
    return encoding.Base64Encoder().encode(
        box.encrypt(secret_value.encode())).decode()


def write_secret(codes, token, repo, secret=DEFAULT_SECRET):
    pk = _gh("GET", f"/repos/{repo}/actions/secrets/public-key", token)
    sealed = seal(pk["key"], json.dumps(codes, ensure_ascii=False))
    _gh("PUT", f"/repos/{repo}/actions/secrets/{secret}", token,
        {"encrypted_value": sealed, "key_id": pk["key_id"]})
    st = _gh("GET", f"/repos/{repo}/actions/secrets/{secret}", token)
    return st


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="",
                    help="自选代码，逗号/空格分隔；缺省读 WATCH_CODES_IN env")
    ap.add_argument("--repo", default=os.environ.get("GH_REPO", DEFAULT_REPO))
    ap.add_argument("--secret",
                    default=os.environ.get("WATCH_SECRET", DEFAULT_SECRET))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    raw = a.codes or os.environ.get("WATCH_CODES_IN", "")
    codes = parse_codes(raw)
    if not codes:
        print("[sync_watch] 未解析出任何合法代码，"
              f"原始输入={raw[:200]!r}", file=sys.stderr)
        return 2

    print(f"[sync_watch] 目标自选 {len(codes)} 只：{','.join(codes)}")
    if a.dry_run:
        print("[sync_watch] dry-run，不写 Secret")
        return 0

    tok = (os.environ.get("GH_PAT") or os.environ.get("GITHUB_PAT")
           or os.environ.get("SYNC_PAT") or "").strip()
    if not tok:
        print("[sync_watch] 缺少 GH_PAT（写 Secrets 必须 PAT，"
              "GITHUB_TOKEN 无此权限）", file=sys.stderr)
        return 3

    st = write_secret(codes, tok, a.repo, a.secret)
    print(f"[sync_watch] ✓ 已写入 {a.repo} 的 Secret {a.secret}"
          f"（updated_at={st.get('updated_at')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
