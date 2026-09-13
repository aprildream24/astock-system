# -*- coding: utf-8 -*-
"""市场准入过滤（吸收自原项目 mktfilter.py，#486 用户拍板口径）。

实盘资金只能交易 沪深主板 + 创业板：科创板（688/689）与北交所
（43/83/87/88/920）未达开通要求——任何选股/推送/模拟盘下单必须先剔除，
「推出去的票 = 能买的票」。未知代码段（B股/指数/ETF）一律 False：
宁可漏推也不推买不了的票。
"""

_OK_PREFIX = ("600", "601", "603", "605",      # 沪市主板
              "000", "001", "002", "003",      # 深市主板（002/003 原中小板并入）
              "300", "301")                    # 创业板
_KC_PREFIX = ("688", "689")                    # 科创板（含 CDR）
_BJ_PREFIX = ("43", "83", "87", "88", "920")   # 北交所


def market_of(code):
    c = str(code or "").strip().zfill(6)
    if len(c) != 6 or not c.isdigit():
        return "其它"
    if c.startswith(_KC_PREFIX):
        return "科创板"
    if c.startswith(_BJ_PREFIX):
        return "北交所"
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith(_OK_PREFIX):
        return "沪深主板"
    return "其它"


def tradable(code):
    return market_of(code) in ("沪深主板", "创业板")


def filter_codes(codes):
    return [c for c in (codes or []) if tradable(c)]


def filter_items(items, key="code"):
    return [x for x in (items or []) if tradable((x or {}).get(key))]
