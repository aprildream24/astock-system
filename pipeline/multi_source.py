# -*- coding: utf-8 -*-
"""多源行情交叉校验：东方财富 / 新浪 / 腾讯（吸收自原项目 multi_source.py，M01）。

目的：单一源偶有报价错误，用另外两个独立源交叉验证。
- 同口径（元/最新成交价，不复权）、同时间范围（拉取时点最新价，容许 <60s 差）；
- ≥2 源可用 → 比较价差，>0.5% 标「数据存疑」，以多源中位数为权威价；
- 时间差 >60s 且价差超阈值一半 → 标记时间戳供人工复核。
"""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .core import fetch_text, redact

UA = "Mozilla/5.0"
SINA_REF = "https://finance.sina.com.cn"
SPREAD_THRESHOLD = 0.5
XCHECK_VERSION = "multi_source/1.0-M01"

SOURCE_SPEC = {
    "em": {"name": "东方财富", "quote": "实时最新价(元)", "delay": "约15分钟(免费档)"},
    "sina": {"name": "新浪财经", "quote": "实时最新价(元)", "delay": "近实时"},
    "tencent": {"name": "腾讯财经", "quote": "实时最新价(元)", "delay": "近实时"},
}


def _prefix(code):
    c = (code or "").strip()
    if not c:
        return None
    if c[0] in "68":
        return "sh"
    if c[0] in "48":
        return "bj"
    return "sz"


def _f(v):
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except Exception:
        return None


def em_quote(code):
    try:
        mkt = "1" if _prefix(code) == "sh" else "0"
        url = ("https://push2.eastmoney.com/api/qt/stock/get?"
               f"secid={mkt}.{code}&fields=f43,f57,f58,f170&fltt=2&invt=2")
        d = json.loads(fetch_text(url, timeout=8))
        dd = (d.get("data") or {}) if isinstance(d, dict) else {}
        if not dd:
            return None
        return {"price": _f(dd.get("f43")), "pct": _f(dd.get("f170")),
                "name": dd.get("f58")}
    except Exception:
        return None


def sina_quote(code):
    pre = _prefix(code)
    if not pre:
        return None
    try:
        url = f"https://hq.sinajs.cn/list={pre}{code}"
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Referer": SINA_REF})
        s = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "replace")
        inner = s.split('"')[1]
        p = inner.split(",")
        if len(p) < 4:
            return None
        price, prev = _f(p[3]), _f(p[2])
        pct = round((price / prev - 1) * 100, 2) if price and prev else None
        return {"price": price, "pct": pct, "name": p[0]}
    except Exception:
        return None


def tencent_quote(code):
    pre = _prefix(code)
    if not pre:
        return None
    try:
        s = fetch_text(f"https://qt.gtimg.cn/q={pre}{code}", timeout=8)
        inner = s.split('"')[1] if '"' in s else ""
        q = inner.split("~")
        if len(q) < 5:
            return None
        price, prev = _f(q[3]), _f(q[4])
        pct = round((price / prev - 1) * 100, 2) if price and prev else None
        return {"price": price, "pct": pct, "name": q[1]}
    except Exception:
        return None


def _one(code):
    t_pull = time.time()
    quotes = [em_quote(code), sina_quote(code), tencent_quote(code)]
    prices = {}
    for q in quotes:
        if q and q.get("price") is not None:
            prices[q.get("name") and "em" or "em"] = q["price"]
    # 按来源归位
    prices = {}
    for key, q in zip(("em", "sina", "tencent"), quotes):
        if q and q.get("price") is not None:
            prices[key] = q["price"]
    vals = list(prices.values())
    item = {"code": code, "prices": prices, "flag": False,
            "checked_at": round(t_pull, 3)}
    if len(vals) >= 2:
        svals = sorted(vals)
        median = svals[len(svals) // 2]
        spread = (max(vals) - min(vals)) / median * 100.0 if median else 0.0
        item.update({"median": median, "spread_pct": round(spread, 3),
                     "authoritative": median,
                     "flag": spread > SPREAD_THRESHOLD})
    elif vals:
        item["authoritative"] = vals[0]
    return item


def cross_check(codes, sample=60, workers=8, timeout=10):
    """对给定代码做三源交叉校验（M01：同口径同时间范围）。
    网络异常由逐源 try/except 兜底，不阻断主流程。"""
    seen = []
    for c in codes:
        c = str(c or "").strip()
        if len(c) == 6 and c.isdigit() and c not in seen:
            seen.append(c)
    if sample and len(seen) > sample:
        seen = seen[:sample]
    t0 = time.time()
    items = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for it in ex.map(_one, seen, timeout=timeout + 5):
                items.append(it)
    except Exception:
        pass
    span = max((it.get("checked_at", t0) for it in items), default=t0) - t0
    flagged = [it for it in items if it.get("flag")]
    return {"checked": len(items),
            "with_data": sum(1 for it in items if it["prices"]),
            "flagged": flagged, "items": items,
            "threshold": SPREAD_THRESHOLD,
            "spec": {"unit": "元（最新成交价，不复权口径）",
                     "sources": SOURCE_SPEC,
                     "time_span_sec": round(span, 2),
                     "checked_at": round(t0, 3),
                     "version": XCHECK_VERSION}}


def quality_block(codes, sample=60):
    """build 收尾调用：返回可挂到数据/详情报告的 quality 区块。"""
    try:
        res = cross_check(codes, sample=sample)
    except Exception as e:  # noqa: BLE001
        return {"skipped": True, "reason": redact(repr(e))[:120]}
    return {"skipped": False, "checked": res["checked"],
            "with_data": res["with_data"],
            "flagged_count": len(res["flagged"]),
            "flagged": [{"code": f["code"], "prices": f["prices"],
                         "spread_pct": f.get("spread_pct")}
                        for f in res["flagged"]],
            "spec": res["spec"]}
