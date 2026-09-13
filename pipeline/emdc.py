# -*- coding: utf-8 -*-
"""东方财富数据中心统一封装（吸收自原项目 emdc.py）。

- 盘后 CI/本地有网即用；无网时由调用方 try/except 兜底为 None，不阻断主流程。
- 数据中心强制 Referer=https://data.eastmoney.com/，缺失返回 403。
- 返回结构：resp["result"]["data"] 为列表。
"""
import urllib.parse

from .core import fetch_text

HOST = "datacenter-web.eastmoney.com"
REFERER = "https://data.eastmoney.com/"


def get(report, columns=None, flt=None, page_size=200, sort=None,
        extra=None, page=1):
    """通用数据中心 GET。flt 自动 URL 编码（不编码会 HTTP 400）。"""
    parts = [
        "/api/data/v1/get?reportName=%s" % report,
        "pageSize=%d" % page_size,
        "pageNumber=%d" % max(1, int(page)),
        "source=WEB&client=WEB",
    ]
    if sort:
        parts.append("sortColumns=%s&sortTypes=-1" % sort)
    if columns:
        parts.append("columns=%s" % columns)
    if flt:
        parts.append("filter=%s" % urllib.parse.quote(flt))
    if extra:
        for k, v in extra.items():
            parts.append("%s=%s" % (k, v))
    import json
    txt = fetch_text("https://%s%s" % (HOST, "&".join(parts)),
                     timeout=15, referer=REFERER)
    j = json.loads(txt)
    if not isinstance(j, dict):
        return []
    data = (j.get("result") or {}).get("data")
    if data is None:
        data = j.get("data")
    if isinstance(data, dict):
        data = data.get("list") or data.get("items") or []
    return data or []


def extract(rows, mapping):
    """按 mapping={目标键: 原始键列表(取第一个非空)} 投影成干净 dict 列表。"""
    out = []
    for r in (rows or []):
        item = {}
        for k, keys in mapping.items():
            if isinstance(keys, str):
                keys = [keys]
            val = None
            for kk in keys:
                if r.get(kk) is not None:
                    val = r[kk]
                    break
            item[k] = val
        out.append(item)
    return out
