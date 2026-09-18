# -*- coding: utf-8 -*-
"""云端持仓同步接收端（2026-09-18，用户需求④：手机端持仓增减最简方式）。

## 思路（零前端加密，复用 sync_watch 范式）
用户在手机网页填好持仓（代码/名称/买入价/股数/日期），页面只发一个带文本
inputs 的 HTTPS 请求到本 workflow 的 dispatches：

    POST /repos/{repo}/actions/workflows/stock.yml/dispatches
      body: {"ref":"main","inputs":{"task":"holdings-sync",
                                     "codes":"<持仓 JSON>"}}
  → GitHub Actions 起一个 run
  → 本脚本在 runner 内用 PyNaCl 把 JSON 写进 Secret HOLDINGS_CONF
    （load_holdings 已支持从该 Secret 读取，云端自动生效）
  → 下一个交易时点，build / intraday 从 HOLDINGS_CONF 读到新持仓

浏览器只发一个带文本 inputs 的 HTTPS 请求——不需要任何加密库/后端。
PAT 仅存在于 owner 的密文数据包里（页面从站点 owner 视图注入）。
"""
import argparse
import json
import os
import re
import sys

DEFAULT_REPO = "aprildream24/astock-system"
DEFAULT_SECRET = "HOLDINGS_CONF"
CODE_RE = re.compile(r"^(sh|sz)\d{6}$")
MAX_ITEMS = 200


def normalize_code(raw):
    s = str(raw or "").strip().lower().replace(" ", "")
    if CODE_RE.match(s):
        return s
    if re.match(r"^\d{6}$", s):
        return ("sh" if s.startswith("6") else "sz") + s
    return None


def parse_holdings(text):
    """把页面提交的 JSON（或 JSON 数组 / 逗号串）解析成规范化持仓列表。"""
    if not text:
        return []
    t = text.strip()
    items = []
    if t.startswith("["):
        try:
            items = json.loads(t)
        except Exception:  # noqa: BLE001
            items = []
    if not items and t.startswith("{"):
        try:
            obj = json.loads(t)
            items = obj if isinstance(obj, list) else obj.get("holdings", [])
        except Exception:  # noqa: BLE001
            items = []
    if not items:
        # 退化：逗号/换行分隔的「代码:买价:股数」最简格式
        for seg in re.split(r"[,;\n]+", t):
            seg = seg.strip()
            if not seg:
                continue
            parts = re.split(r"[:\s]+", seg)
            code = normalize_code(parts[0])
            if not code:
                continue
            it = {"code": code}
            if len(parts) > 1 and re.match(r"^\d+(\.\d+)?$", parts[1]):
                it["buy_price"] = float(parts[1])
            if len(parts) > 2 and re.match(r"^\d+$", parts[2]):
                it["shares"] = int(parts[2])
            items.append(it)
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        code = normalize_code(it.get("code") or it.get("symbol") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        rec = {"code": code}
        if it.get("name"):
            rec["name"] = str(it["name"])
        if it.get("buy_price") is not None:
            try:
                rec["buy_price"] = float(it["buy_price"])
            except (TypeError, ValueError):
                pass
        if it.get("shares") is not None:
            try:
                rec["shares"] = int(it["shares"])
            except (TypeError, ValueError):
                pass
        if it.get("buy_date"):
            rec["buy_date"] = str(it["buy_date"])
        out.append(rec)
    return out[:MAX_ITEMS]


def _gh(method, path, token, body=None):
    import urllib.request
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "astock-sync-holdings",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        b = r.read()
    return json.loads(b) if b else {}


def seal(public_key_b64, secret_value):
    from nacl import encoding, public
    if isinstance(public_key_b64, bytes):
        public_key_b64 = public_key_b64.decode()
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    box = public.SealedBox(pk)
    if isinstance(secret_value, bytes):
        secret_value = secret_value.decode()
    return encoding.Base64Encoder().encode(
        box.encrypt(secret_value.encode())).decode()


def write_secret(payload, token, repo, secret=DEFAULT_SECRET):
    pk = _gh("GET", f"/repos/{repo}/actions/secrets/public-key", token)
    sealed = seal(pk["key"], json.dumps(payload, ensure_ascii=False))
    _gh("PUT", f"/repos/{repo}/actions/secrets/{secret}", token,
        {"encrypted_value": sealed, "key_id": pk["key_id"]})
    return _gh("GET", f"/repos/{repo}/actions/secrets/{secret}", token)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdings", default="",
                    help="持仓 JSON；缺省读 HOLDINGS_IN env")
    ap.add_argument("--repo", default=os.environ.get("GH_REPO", DEFAULT_REPO))
    ap.add_argument("--secret",
                    default=os.environ.get("HOLDINGS_SECRET", DEFAULT_SECRET))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    raw = a.holdings or os.environ.get("HOLDINGS_IN", "")
    items = parse_holdings(raw)
    if not items:
        print("[sync_holdings] 未解析出任何合法持仓，"
              f"原始输入={raw[:200]!r}", file=sys.stderr)
        return 2
    print(f"[sync_holdings] 目标持仓 {len(items)} 只："
          + ", ".join(f"{i['code']}@{i.get('buy_price','?')}"
                      for i in items))
    if a.dry_run:
        print("[sync_holdings] dry-run，不写 Secret")
        return 0
    tok = (os.environ.get("GH_PAT") or os.environ.get("GITHUB_PAT")
           or os.environ.get("SYNC_PAT") or "").strip()
    if not tok:
        print("[sync_holdings] 缺少 GH_PAT（写 Secrets 必须 PAT，"
              "GITHUB_TOKEN 无此权限）", file=sys.stderr)
        return 3
    st = write_secret(items, tok, a.repo, a.secret)
    print(f"[sync_holdings] ✓ 已写入 {a.repo} 的 Secret {a.secret}"
          f"（updated_at={st.get('updated_at')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
