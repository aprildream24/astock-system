#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步自选/持仓名单到 GitHub Secrets（CI 端数据源）。

用法：python tools/sync_secrets.py
- PAT 从 %TEMP%\\astock_gh_token.txt 读取，不进命令行、不进仓库。
- 同步 config/watch.json  -> Secret WATCH_CODES
- 同步 config/holdings.json -> Secret HOLDINGS_CONF
公开仓库不落名单，Secret 是唯一 CI 通道（build._codes_conf 双源合并）。
"""
import base64
import io
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com/repos/aprildream24/astock-system"
PAT_FILE = os.path.expandvars(r"%TEMP%\astock_gh_token.txt")


def _hdr():
    token = io.open(PAT_FILE, encoding="utf-8").read().strip()
    return {"Authorization": "token " + token, "User-Agent": "astock-sync",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json"}


def put_secret(name, value):
    from nacl import encoding, public
    h = _hdr()
    req = urllib.request.Request(f"{API}/actions/secrets/public-key", headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        pk = json.loads(r.read())
    box = public.SealedBox(
        public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(json.dumps(value).encode())).decode()
    req = urllib.request.Request(f"{API}/actions/secrets/{name}", method="PUT",
                                 headers=h,
                                 data=json.dumps({"encrypted_value": enc,
                                                  "key_id": pk["key_id"]}).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main():
    watch = json.load(io.open(os.path.join(ROOT, "config", "watch.json"),
                              encoding="utf-8"))
    hold = json.load(io.open(os.path.join(ROOT, "config", "holdings.json"),
                             encoding="utf-8"))
    print("WATCH_CODES <-", watch, "HTTP", put_secret("WATCH_CODES", watch))
    print("HOLDINGS_CONF <-", hold, "HTTP", put_secret("HOLDINGS_CONF", hold))
    print("done")


if __name__ == "__main__":
    sys.exit(main())
