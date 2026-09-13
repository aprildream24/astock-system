# -*- coding: utf-8 -*-
"""推送层：md2html 渲染 / 候选行 ≤96 字符 / 去重账本双写 / 昨日推荐今日复核 /
ServerChan + PushPlus 双通道（密钥占位，由使用者填 config/notify.json）。"""
import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime

from . import core
from .core import load_config, get_conn, fetch_open_snapshot, BASE_DIR

PP_HTML_CAP = 19000          # PushPlus content 上限 20000，安全线 19000
CAND_LINE_CAP = 96           # 候选行硬红线（字符数）
DIST_LEDGER = os.path.join(BASE_DIR, "dist", "push_ledger.json")

ACTION_BADGE = {"现在买": "✅买入", "等回踩": "⏳等回踩", "小仓试": "🔸小仓试",
                "次日竞价达标买": "🎯竞价达标买", "观望": "👀观望", "禁买": "⛔禁买"}

RULE_VERSION = "v2-20260912"   # 内容+规则版本：升级后 biz_key 自动换新放行


# ---------------------------------------------------------------------------
# 渲染（内联样式自绘——微信/PushPlus 只认内联样式，避免字体大小失控）
# ---------------------------------------------------------------------------

_STY = {
    "doc": ("font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
            "font-size:14px;color:#333;line-height:1.75"),
    "h1": ("font-size:17px;font-weight:700;color:#111;"
           "border-bottom:2px solid #d93026;padding-bottom:6px;margin:6px 0 10px"),
    "h2": ("font-size:15px;font-weight:700;color:#111;"
           "border-left:4px solid #d93026;padding-left:8px;margin:14px 0 6px"),
    "h3": "font-size:14px;font-weight:700;color:#111;margin:10px 0 4px",
    "p": "margin:5px 0",
    "li": "margin:4px 0",
    "meta": "color:#999;font-size:12px;margin:6px 0",
}

ACTION_BG = {"现在买": "#d93026", "次日竞价达标买": "#b9860b",
             "等回踩": "#b9860b", "小仓试": "#b9860b",
             "观望": "#6b7280", "禁买": "#7a2226", "未推荐": "#9ca3af",
             "条件满足": "#1d7a4c", "等待确认": "#b9860b", "数据不足": "#6b7280",
             "超价取消": "#c62828", "结构失效": "#c62828", "到期失效": "#6b7280"}


def _badge(text, bg):
    return (f'<span style="background:{bg};color:#fff;border-radius:4px;'
            f'padding:1px 8px;font-size:12px;font-weight:700;'
            f'display:inline-block">{text}</span>')


def _inline(s):
    s = re.sub(r"\*\*(.+?)\*\*", r'<strong style="color:#111">\1</strong>', s)
    return s


def md2html(md):
    """极简 markdown → 内联样式 HTML（红涨绿卖灰辅助，微信 webview 兼容）。"""
    out = []
    for line in md.splitlines():
        esc = html.escape(line)
        if line.startswith("### "):
            out.append(f'<div style="{_STY["h3"]}">{_inline(esc[4:])}</div>')
        elif line.startswith("## "):
            out.append(f'<div style="{_STY["h2"]}">{_inline(esc[3:])}</div>')
        elif line.startswith("# "):
            out.append(f'<div style="{_STY["h1"]}">{_inline(esc[2:])}</div>')
        elif line.startswith("- "):
            out.append(f'<div style="{_STY["li"]}">· {_inline(esc[2:])}</div>')
        elif line.strip():
            out.append(f'<div style="{_STY["p"]}">{_inline(esc)}</div>')
    return f'<section style="{_STY["doc"]}">' + "".join(out) + "</section>"


def _cand_line(c):
    """候选行统一单一出口渲染。自适应卸载顺序：挂单价→距买区→仓位→分数；
    买/卖/停永不丢。"""
    badge = ACTION_BADGE.get(c.get("action"), "👀观望")
    extras = []
    if c.get("pool") == "连板" and c.get("streak"):
        extras.append(f"{c['streak']}板")
    if c.get("hot_pick"):
        extras.append("🔥优选")
    core = (f"{badge} {c.get('name','')} {c.get('code','')} "
            f"[{c['pool']} {c.get('score','')}] "
            f"买{c['buy_low']:.2f}-{c['buy_high']:.2f} "
            f"卖{c['sell_low']:.2f}-{c['sell_high']:.2f} "
            f"停{c['stop']:.2f}")
    if c.get("cycle_hint"):
        extras.append(f"周期{c.get('hold_days')}日({c['cycle_hint']})")
    if c.get("entry_hint"):
        extras.append(c["entry_hint"])
    if c.get("dist_pct") is not None:
        extras.append(f"距买区{c['dist_pct']:.1f}%")
    if c.get("position"):
        extras.append(f"仓{c['position']}")
    suffix = " ".join(extras)
    if len(core) + len(suffix) + 1 > CAND_LINE_CAP:
        # 按卸载顺序丢弃
        for drop in ("cycle", "entry", "dist", "pos"):
            if drop == "cycle" and extras and extras[0].startswith("周期"):
                extras.pop(0)
            elif drop == "entry" and any("回落" in e or "挂单" in e for e in extras):
                extras = [e for e in extras if "回落" not in e and "挂单" not in e]
            elif drop == "dist" and any("距买区" in e for e in extras):
                extras = [e for e in extras if "距买区" not in e]
            elif drop == "pos" and any(e.startswith("仓") for e in extras):
                extras = [e for e in extras if not e.startswith("仓")]
            suffix = " ".join(extras)
            if len(core) + len(suffix) + 1 <= CAND_LINE_CAP:
                break
    line = core + (" " + suffix if suffix else "")
    return line[:CAND_LINE_CAP]


def render_candidates(title, picks, extra_lines=()):
    md = [f"# {title}", ""]
    for c in picks:
        md.append("- " + _cand_line(c))
    md += ["" + l for l in extra_lines]
    return md2html("\n".join(md))


# ---------------------------------------------------------------------------
# M35/N10 变化式主报告 + 标的卡片（状态 > 名称 > 价格/失效 > 理由 > 评分）
# ---------------------------------------------------------------------------

STATUS_CLS = {"条件满足": "#1d7a4c", "等待确认": "#b9860b", "数据不足": "#6b7280",
              "超价取消": "#c62828", "结构失效": "#c62828", "到期失效": "#6b7280"}


def _card(inner, border="#e5e5e5"):
    return (f'<div style="border:1px solid {border};border-radius:8px;'
            f'padding:10px;margin:8px 0">{inner}</div>')


def _kv(k, v):
    return (f'<div style="margin:2px 0"><span style="color:#999">{k}</span>'
            f'<span>{v}</span></div>')


def render_card(d, first=False):
    """N10 统一标的卡片（内联样式）。状态>名称>价格/失效>理由>评分。"""
    zone = d.get("zone") or [None, None]
    zone_s = f"{zone[0]:.2f}~{zone[1]:.2f}" if zone[0] and zone[1] else "—"
    cap = f"{zone[1] * 1.03:.2f}" if zone[1] else "—"
    status = d.get("status", "等待确认")
    badge = _badge(status, STATUS_CLS.get(status, "#6b7280"))
    head = ('<div style="color:#5b8def;font-weight:700;font-size:13px;'
            'margin-bottom:4px">【首选观察】</div>' if first else
            '<div style="color:#8b95a5;font-weight:700;font-size:13px;'
            'margin-bottom:4px">【备选观察】</div>')
    inner = (
        f'<div style="margin-bottom:6px"><span style="font-size:15px;'
        f'font-weight:700">{html.escape(d.get("name") or "")}</span>'
        f'<span style="color:#999;font-size:12px"> {html.escape(str(d.get("code", "")))}</span>'
        f' <span style="float:right">{badge}</span></div>'
        + _kv("关注区间：", f'<span class="zone">{zone_s}</span>')
        + _kv("不追价上限：", cap)
        + _kv("止损：", f'{d.get("stop"):.2f}' if d.get("stop") else "—")
        + _kv("有效截止：", html.escape(str(d.get("valid_until", "—"))))
        + _kv("失效条件：", html.escape(str(d.get("invalid_if", "条件破坏即失效"))))
        + (f'<div style="color:#666;font-size:13px;margin-top:6px;'
           f'border-top:1px dashed #eee;padding-top:6px">理由：'
           f'{html.escape(str(d.get("reason") or "—"))}'
           f'<span style="color:#bbb;font-size:11px"> ｜ 评级 '
           f'{html.escape(str(d.get("research_grade", "—")))} 分 '
           f'{d.get("score", "—")}</span></div>' if d.get("reason") or d.get("score") else ""))
    return head + _card(inner)


def render_brief(today, first, backups, changes, meta, ladder_next=()):
    """M35 主报告（内联样式自绘）：今日结论 / 首选 / 备选≤2 / 次日通道 /
    计划变化 / 数据说明。ladder_next = 次日竞价确认单独分组（非即时可买）。"""
    out = [f'<div style="{_STY["h1"]}">收盘观察 {html.escape(today)}</div>',
           f'<div style="{_STY["meta"]}">复核 {html.escape(str(meta.get("reviewed", "—")))} 只 · '
           f'数据日期 {html.escape(str(meta.get("data_date", today)))} · '
           f'有效期至 {html.escape(str(meta.get("valid_until", "—")))}</div>']
    if first:
        out.append(render_card(first, first=True))
    else:
        out.append('<div style="color:#666;padding:14px 0">今日无当下可买入的机会'
                   '——没有机会就不凑数。</div>')
    for b in (backups or [])[:2]:
        out.append(render_card(b))
    if ladder_next:
        out.append(f'<div style="{_STY["h2"]}">次日竞价确认 · 非即时可买</div>')
        for d in ladder_next[:2]:
            zone = d.get("zone") or [None, None]
            zs = f"{zone[0]:.2f}~{zone[1]:.2f}" if zone[0] and zone[1] else "—"
            out.append(_card(
                f'<div style="margin-bottom:4px"><b>{html.escape(d.get("name", ""))}</b>'
                f'<span style="color:#999;font-size:12px"> {html.escape(str(d.get("code", "")))}</span></div>'
                + _kv("达标条件：", "高开≥2%~5%（按板数）")
                + _kv("低开处理：", "放弃（历史胜率仅24%）")
                + _kv("关注区间：", zs)
                + (f'<div style="color:#666;font-size:12px">{html.escape(str(d.get("gate_evidence") or ""))}</div>'
                   if d.get("gate_evidence") else "")))
    if changes:
        out.append(f'<div style="{_STY["h2"]}">计划变化</div>')
        for c in changes[:8]:
            st_bg = STATUS_CLS.get(c.get("new"), "#6b7280")
            out.append(_kv(html.escape(f"{c.get('code')}"),
                           _badge(c.get("new", ""), st_bg)
                           + f' <span style="color:#999;font-size:12px">'
                           f'{html.escape(str(c.get("reason") or ""))}</span>'))
    out.append(f'<div style="{_STY["meta"]}">{html.escape(str(meta.get("note", "")))}</div>')
    return f'<section style="{_STY["doc"]}">' + "".join(out) + "</section>"


def save_detail_report(html, today, data_json=None):
    """详情落盘：HTML + JSON（程序读取/归因），微信只发摘要。"""
    d = os.path.join(BASE_DIR, "dist", "reports")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{today}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    if data_json is not None:
        with open(os.path.join(d, f"{today}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(data_json, f, ensure_ascii=False, indent=1,
                      default=str)
    return d


def _clip_html(content):
    """超限整行回退裁剪再重渲染（截半个 div 会整版错乱）。"""
    if len(content) <= PP_HTML_CAP:
        return content
    blocks = re.findall(r"<(?:p|li)>.*?</(?:p|li)>|<h\d>.*?</h\d>", content)
    while blocks and len("".join(blocks)) > PP_HTML_CAP:
        if blocks and blocks[-1].startswith("<li>"):
            # 从尾部候选行开始整块删
            blocks.pop()
        else:
            blocks.pop()
    return "".join(blocks)


# ---------------------------------------------------------------------------
# #601-B 昨日推荐今日复核 _prev_pick_status（单一出口）
# ---------------------------------------------------------------------------

def _prev_pick_status(it, today_pool, lv=None, compact=False):
    """判定优先级（走坏优先于买区——先判生死再判位置）：

    跌破止损 > 走坏(chg≤-4%) > 还在跟(≤买区上沿) > 略高(≤上沿×1.03)
    > 涨过头(chg≥+3%) > 跟踪中。
    降级路径（无实时行情）：现价在今日买区→还在跟 / 距上沿≤3%→略高
    / 超出→涨过头 / today=None→✂️今日剔除。
    """
    code, stop = it["code"], it.get("stop")
    price = prev = None
    if lv and code in lv:
        price, prev = lv[code]["price"], lv[code]["prev"]
    if price is not None and prev:
        chg = (price / prev - 1) * 100
        if stop and price <= stop:
            return f"⛔跌破止损（现价{price:.2f}）"
        if chg <= -4:
            return f"⚠️走坏（{chg:+.1f}%）"
        if price <= it.get("buy_high", price):
            return "🟢还在跟"
        if price <= it.get("buy_high", price) * 1.03:
            return f"🟡略高（距买区+{(price / it['buy_high'] - 1) * 100:.1f}%）"
        if chg >= 3:
            return f"🔴涨过头（{chg:+.1f}%）"
        return f"🔹跟踪中（今开{lv[code]['open_pct']:+.1f}%）" if not compact else "🔹跟踪中"
    # 降级路径：候选池对比
    if today_pool is None:
        return "✂️今日剔除"
    in_pool = any(c["code"] == code for c in today_pool)
    if not in_pool:
        return "✂️今日剔除"
    c = next(c for c in today_pool if c["code"] == code)
    price = c["close"]
    if price <= c["buy_high"]:
        return "🟢还在跟"
    if price <= c["buy_high"] * 1.03:
        return "🟡略高"
    return "🔴涨过头"


def prev_pick_review_lines(prev_picks, today_pool):
    """盘前/竞价推送里给昨日推荐逐票加一行「今天它怎么样了」。"""
    lv = fetch_open_snapshot([p["code"] for p in prev_picks]) if prev_picks else {}
    lines = []
    for p in prev_picks:
        st = _prev_pick_status(p, today_pool, lv)
        lines.append(f"{p.get('name','')} {p['code']}：{st}")
    return lines


# ---------------------------------------------------------------------------
# 去重账本（双写防丢）
# ---------------------------------------------------------------------------

def biz_key(mode, date, codes):
    raw = f"{mode}|{date}|{RULE_VERSION}|{','.join(sorted(codes))}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _reconcile(con, key, mode, ts, dist_ok):
    """每次 push 前对账：dist 有今日而 state 缺 → 判重回补。"""
    dist = {}
    if os.path.exists(DIST_LEDGER):
        with open(DIST_LEDGER, "r", encoding="utf-8") as f:
            dist = json.load(f)
    row = con.execute("SELECT 1 FROM push_ledger WHERE biz_key=?", (key,)).fetchone()
    if row is None and dist.get(key):
        con.execute(
            "INSERT OR REPLACE INTO push_ledger VALUES(?,?,?,?,?,?,?)",
            (key, mode, dist[key].get("ts", ts), 1,
             dist[key].get("status", "sent"), mode, "reconciled from dist"))
        con.commit()
        return True          # 已发过，拦截
    if row is not None:
        return True
    return False


def push(mode, title, content, date=None, con=None,
         channels=None, force=False):
    """推送 + 三态账本（M37）+ 防混淆标识 + 去重。

    主通道由配置决定（primary_channel: wxpusher | pushplus | serverchan，
    默认 wxpusher 多账户）；调用方可用 channels= 显式覆盖。
    防混淆标识（用户需求 2026-09-13）：每条消息标题加
    【{push_tag}·{来源}】前缀（如 【Astra·主号】/【Astra·PushPlus】），
    正文顶部加同源角标——多账户/多渠道混收时一眼可辨。
    受理不确定(uncertain)不盲目双发；force=True 重要风险绕过普通去重。"""
    cfg = load_config()
    date = date or datetime.now().strftime("%Y-%m-%d")
    con = con or get_conn()
    tag = cfg.get("push_tag") or "Astra"
    primary = cfg.get("primary_channel") or "wxpusher"
    if channels is None:
        channels = (primary,)
    codes = re.findall(r"\d{6}", content)
    key = biz_key(mode, date, codes)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not force and _reconcile(con, key, mode, ts, True):
        return {"sent": False, "dedup": True, "key": key}
    # 网页端入口：每条推送底部附站点地址（用户需求 2026-09-13）
    site_url = cfg.get("site_url") or "https://aprildream24.github.io/astock-system/"
    content += (f'<div style="margin-top:14px;padding-top:8px;'
                f'border-top:1px solid #e5e5e5;color:#576b95;font-size:13px">'
                f'🌐 网页端详情：<a href="{site_url}" style="color:#576b95">'
                f'{site_url}</a>（访问口令见 config/users.json / SITE_USERS）</div>')
    results = {}
    if cfg.get("push_dry_run"):
        from . import wxpusher
        accts = wxpusher.load_accounts() if "wxpusher" in channels else []
        if accts:
            for a in accts:
                results[f"wxpusher:{a['name']}"] = {"status": "dry-run",
                                                    "detail": ""}
        else:
            for c in channels:
                results[c] = {"status": "dry-run", "detail": ""}
    else:
        from . import wxpusher
        wx_accounts = wxpusher.resolve_targets(mode, cfg=cfg) \
            if "wxpusher" in channels else []
        for a in wx_accounts:
            t = f"【{tag}·{a.get('name', '')}】{title}"
            body = f"<p><small>📮 {tag} · {a.get('name', '')}</small></p>" + content
            st, detail = wxpusher.send(a, t, body)
            results[f"wxpusher:{a['name']}"] = {"status": st, "detail": detail}
        statuses = [r["status"] for r in results.values()]
        all_failed = wx_accounts and statuses and all(
            s == "failed" for s in statuses)
        if all_failed and cfg.get("serverchan_key"):
            st2, d2 = _send_serverchan(cfg["serverchan_key"],
                                       f"【{tag}·备用SC】{title}", content)
            results["serverchan"] = {"status": st2, "detail": d2,
                                     "role": "fallback"}
        if not wx_accounts or "wxpusher" not in channels:
            if "pushplus" in channels and cfg.get("pushplus_token"):
                st, detail = _send_pushplus(cfg["pushplus_token"],
                                            f"【{tag}·PushPlus】{title}",
                                            f"<p><small>📮 {tag} · PushPlus</small></p>" + content)
                results["pushplus"] = {"status": st, "detail": detail}
            elif "serverchan" in channels and cfg.get("serverchan_key"):
                st, detail = _send_serverchan(cfg["serverchan_key"],
                                              f"【{tag}·SC】{title}", content)
                results["serverchan"] = {"status": st, "detail": detail}
    # 聚合口径：任一通道送达即 sent；不确定优先于 failed
    statuses = [r["status"] for r in results.values()] or ["dry-run"]
    if "sent" in statuses:
        worst = "sent"
    elif "uncertain" in statuses:
        worst = "uncertain"
    elif "failed" in statuses:
        worst = "failed"
    else:
        worst = statuses[0]
    dist = {}
    if os.path.exists(DIST_LEDGER):
        with open(DIST_LEDGER, "r", encoding="utf-8") as f:
            dist = json.load(f)
    dist[key] = {"mode": mode, "ts": ts, "status": worst,
                 "channels": {c: r["status"] for c, r in results.items()}}
    os.makedirs(os.path.dirname(DIST_LEDGER), exist_ok=True)
    with open(DIST_LEDGER, "w", encoding="utf-8") as f:
        json.dump(dist, f, ensure_ascii=False, indent=1)
    try:
        con.execute("INSERT OR REPLACE INTO push_ledger VALUES(?,?,?,?,?,?,?)",
                    (key, mode, ts, 1, worst,
                     ",".join(results.keys()),
                     json.dumps({c: r["status"] for c, r in results.items()},
                                ensure_ascii=False)))
        con.commit()
    except Exception:   # state 写失败 → 告警行 + 即时自愈（dist 已有镜像）
        results["_ledger_alert"] = {"status": "failed",
                                    "detail": "state ledger write failed"}
    return {"sent": True, "key": key, "status": worst, "results": results}


def _send_serverchan(key, title, content):
    """返回 (status, detail)。status ∈ sent/failed/uncertain（M37）。
    超时/连接错误 = 受理不确定，不盲目重试双发。"""
    try:
        data = urllib.parse.urlencode(
            {"title": title, "desp": re.sub(r"<[^>]+>", "", content)}
        ).encode()
        req = urllib.request.Request(
            f"https://sctapi.ftqq.com/{key}.send", data=data)
        urllib.request.urlopen(req, timeout=10)
        return "sent", "ok"
    except urllib.error.HTTPError as e:
        return "failed", core.redact(str(e), key)
    except Exception as e:  # noqa: BLE001 — 超时/网络错误：受理状态未知
        return "uncertain", core.redact(str(e), key)


def _send_pushplus(token, title, content):
    """M36 主推通道。返回 (status, detail)。"""
    try:
        body = json.dumps({"token": token, "title": title,
                           "content": content[:PP_HTML_CAP],
                           "template": "html"}).encode()
        req = urllib.request.Request(
            "https://www.pushplus.plus/send", data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return "sent", "ok"
    except urllib.error.HTTPError as e:
        return "failed", core.redact(str(e), token)
    except Exception as e:  # noqa: BLE001
        return "uncertain", core.redact(str(e), token)
