# -*- coding: utf-8 -*-
"""WxPusher 多账户推送（用户指定主通道）。

账户配置（config/notify.json，已 gitignore；CI 用 Secret WXPUSHER_CONF 注入）：
  "wxpusher_accounts": [
    {"name": "主号",  "app_token": "AT_xxx", "uids": ["UID_xxx"]},
    {"name": "家人",  "app_token": "AT_yyy", "uids": ["UID_yyy"], "app_ids": [123]}
  ]
路由（可选，由用户决定发给谁；缺省发给全部账户）：
  "wxpusher_routes": {"*": ["主号", "家人"]}
  键匹配顺序：精确 mode → mode 前缀（build_/exec_/narrative 等）→ "*"。
CI Secret 格式：WXPUSHER_CONF = 上述 wxpusher_accounts 的 JSON 数组字符串
（与本地文件账户合并，env 优先）。

API：POST https://wxpusher.zjiecode.com/api/send/message
  {appToken, content(html), summary(≤100字), contentType:3, uids/appIds}
受理≠送达（M37 同源纪律）：HTTP OK 且 resp.code==1000 → sent；
resp.code!=1000 → failed（明确失败）；网络超时 → uncertain。
"""
import json
import os
import urllib.error
import urllib.request

from .core import redact

WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"
CONTENT_CAP = 19000          # 保守上限（与 PushPlus 同级安全线）
UA = "Mozilla/5.0"


def load_accounts():
    """本地 notify.json 的 wxpusher_accounts + 环境变量 WXPUSHER_CONF 合并。"""
    from .core import load_config
    cfg = load_config()
    accounts = list(cfg.get("wxpusher_accounts") or [])
    env = os.environ.get("WXPUSHER_CONF")
    if env:
        try:
            env_accounts = json.loads(env)
            if isinstance(env_accounts, list):
                by_name = {a.get("name"): a for a in accounts
                           if isinstance(a, dict)}
                for a in env_accounts:
                    if isinstance(a, dict) and a.get("name"):
                        by_name[a["name"]] = a        # env 覆盖同名本地账户
                accounts = list(by_name.values())
        except Exception:  # noqa: BLE001 — Secret 格式错误不阻断，仅记失败
            pass
    return [a for a in accounts
            if isinstance(a, dict) and a.get("app_token")
            and (a.get("uids") or a.get("app_ids"))]


def resolve_targets(mode, accounts=None, cfg=None):
    """按路由决定本 mode 发给哪些账户。键匹配：精确 → 前缀 → '*' → 全部。"""
    from .core import load_config
    cfg = cfg or load_config()
    accounts = accounts if accounts is not None else load_accounts()
    routes = cfg.get("wxpusher_routes") or {}
    names = None
    for key in (mode, mode.split("_")[0], "*"):
        if key in routes:
            names = routes[key]
            break
    if names is None:
        return accounts
    want = set(names)
    return [a for a in accounts if a.get("name") in want]


def send(account, title, content, timeout=12):
    """发单账户。返回 (status, detail)，status ∈ sent/failed/uncertain。"""
    try:
        body = {"appToken": account["app_token"],
                "content": content[:CONTENT_CAP],
                "summary": title[:99],
                "contentType": 3}                     # 3 = html
        if account.get("uids"):
            body["uids"] = account["uids"]
        if account.get("app_ids"):
            body["appIds"] = account["app_ids"]
        req = urllib.request.Request(
            WXPUSHER_API, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8", "replace"))
        if out.get("code") == 1000:
            return "sent", "ok"
        return "failed", redact(f"code={out.get('code')} {out.get('msg')}",
                                account.get("app_token"))
    except urllib.error.HTTPError as e:
        return "failed", redact(str(e), account.get("app_token"))
    except Exception as e:  # noqa: BLE001 — 超时/网络：受理不确定
        return "uncertain", redact(str(e), account.get("app_token"))
