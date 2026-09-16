# -*- coding: utf-8 -*-
"""推送层：md2html 渲染 / 候选行 ≤96 字符 / 去重账本双写 / 昨日推荐今日复核 /
ServerChan + PushPlus 双通道（密钥占位，由使用者填 config/notify.json）。"""
import hashlib
import html
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import core
from .core import load_config, get_conn, fetch_open_snapshot, BASE_DIR

PP_HTML_CAP = 19000          # PushPlus content 上限 20000，安全线 19000
CAND_LINE_CAP = 96           # 候选行硬红线（字符数）
DIST_LEDGER = os.path.join(BASE_DIR, "dist", "push_ledger.json")

# 北京时间：CI runner 的本地时区是 **UTC**，而账本的 ts 是人工排障时
# 第一眼要读的东西。若直接用 now()，账本上会出现 `2026-09-16 05:41:06`
# ——实际是北京时间 13:41，极容易被误读成"凌晨推的/今天没推"。
# ⚠️ 只改**时间部分**：日期部分必须继续用交易日 `date`（见 push() 内注释，
# 那是「补发历史不得占当日额度」的命门）。_daily_sent 只比对日期前缀，
# 故本改动对去重/保险丝零影响。
_CST = timezone(timedelta(hours=8))

ACTION_BADGE = {"现在买": "✅买入", "等回踩": "⏳等回踩", "小仓试": "🔸小仓试",
                "次日竞价达标买": "🎯竞价达标买", "观望": "👀观望", "禁买": "⛔禁买"}

RULE_VERSION = "v2-20260912"   # 内容+规则版本：升级后 biz_key 自动换新放行


# ---------------------------------------------------------------------------
# 渲染（表格化内联样式）
#
# 版面纪律（2026-09-13 重做，修复"推送版面不美观"）：
#   1) 一律用 <table> 做对齐 —— 微信/PushPlus/邮件 webview 对 float/flex 支持
#      极不稳定，旧的 float:right 徽章在手机上会掉行甚至压住标题；
#   2) 指标用「标签列固定宽 + 数值列」两列表，中文冒号宽度不一的参差消失；
#   3) 每张卡片固定顺序：状态 → 名称 → 价格矩阵 → 失效条件 → 理由，有落点；
#   4) 纯文本通道（ServerChan）走 html_to_text 结构化降级，不再粗暴剥标签。
# ---------------------------------------------------------------------------

_STY = {
    # 2026-09-14 晚二次迭代（用户反馈"不喜欢白色底板"）：整份消息统一深色底——
    # 根容器与卡片同色系（#15181e / #1d222b），无白色色块，视觉上"融入底色"。
    # 关键教训：微信/PushPlus 深色模式 webview 会把无背景容器渲染成透明（黑），
    # 所以必须显式给出深色背景，而不是依赖 webview 自己的底色。
    # 文字全部浅色；红/绿/蓝三色语义标注按深色底提亮（#ff6b5e/#4ecf8e/#6ab0ff）。
    "doc": ("font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
            "font-size:14px;color:#e8eaed;line-height:1.75;"
            "background:#15181e;padding:2px 2px 8px"),
    "h1": ("font-size:18px;font-weight:700;color:#f1f3f4;"
           "border-bottom:2px solid #ff6b5e;padding-bottom:7px;margin:4px 0 10px"),
    "h2": ("font-size:15px;font-weight:700;color:#f1f3f4;"
           "border-left:4px solid #ff6b5e;padding-left:8px;margin:16px 0 8px"),
    "h3": "font-size:14px;font-weight:700;color:#f1f3f4;margin:10px 0 4px",
    "p": "margin:5px 0",
    "li": "margin:4px 0",
    "meta": "color:#9aa0a6;font-size:12px;margin:6px 0",
}

_LABEL_W = 80          # 指标标签列固定宽（两列表对齐的关键）


def _esc(s):
    """统一转义 + 空值占位（避免推送里出现 None / 空档位）。"""
    return html.escape("—" if s is None or s == "" else str(s))


def _table(inner):
    return ('<table cellpadding="0" cellspacing="0" border="0" '
            f'style="width:100%;border-collapse:collapse">{inner}</table>')


def _row(k, v, v_color="#e8eaed", v_bold=False):
    """两列表格行：标签灰、数值深，中文冒号宽度不一也不歪。"""
    bold = "font-weight:700;" if v_bold else ""
    return ('<tr>'
            f'<td style="width:{_LABEL_W}px;color:#9aa0a6;font-size:13px;'
            'padding:3px 10px 3px 0;vertical-align:top;white-space:nowrap">'
            f'{_esc(k)}</td>'
            f'<td style="padding:3px 0;color:{v_color};font-size:14px;{bold}">'
            f'{v}</td></tr>')


def _wide_row(inner, pad="2px 0 6px"):
    """跨两列的行（放位置条这类块元素，不挤占标签列）。"""
    return (f'<tr><td colspan="2" style="padding:{pad}">{inner}</td></tr>')


def _zone_bar(lo, hi, close):
    """买区位置条：一眼看出「现价 vs 买区」的位置关系（用户需求 2026-09-13）。

    用户在推送里最常问的就是"这票现在能不能买"，光给数字要读者自己比大小。
    这里用纯表格三段色带实现（无 float / 无 position——微信与 PushPlus 的
    webview 对这两者支持极不稳定），现价所在段打 ▲ 标记。
    """
    if not (lo and hi and close):
        return ""
    v0, v1 = lo * 0.97, hi * 1.06            # 视窗：买区下沿-3% ~ 上沿+6%
    if v1 <= v0:
        return ""

    def pos(x):
        return max(0.0, min(100.0, (x - v0) / (v1 - v0) * 100))

    p_lo, p_hi = pos(lo), pos(hi)
    widths = [round(p_lo, 1), round(p_hi - p_lo, 1), round(100 - p_hi, 1)]
    styles = ["#2b313d", "#a8433c", "#2b313d"]        # 区外暗灰 / 买区红 / 区外暗灰
    aligns = ["right", "center", "left"]
    here = 0 if close < lo else (1 if close <= hi else 2)
    cells = "".join(
        f'<td width="{widths[i]}%" align="{aligns[i]}" '
        f'style="background:{styles[i]};height:15px;line-height:15px;'
        f'white-space:nowrap;font-size:11px;color:#9aa0a6">'
        f'{"▲现价" if i == here else ""}</td>'
        for i in range(3) if widths[i] > 0)
    return ('<table cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'style="border-collapse:collapse"><tr>{cells}</tr></table>'
            '<div style="color:#80868b;font-size:11px;margin-top:2px">'
            '红段=买入区间｜灰段=不建议追价区</div>')


def _status_color(s):
    """复核状态按语义上色（红=走坏/止损，黄=略高，绿=还在跟）。"""
    s = s or ""
    if s.startswith(("⛔", "🔴", "⚠️")):
        return "#ff8a80"
    if s.startswith("🟡"):
        return "#f5b83d"
    if s.startswith("🟢"):
        return "#4ecf8e"
    return "#9aa0a6"


def _summary_strip(meta, n_buy, n_pending, n_ladder):
    """顶部速览条：先给结论（几只能买），再给细节——手机上不必下滑就有答案。"""
    cov = meta.get("coverage")
    items = [("今日可下单", str(n_buy), "#ff6b5e"),
             ("待回踩", str(n_pending), "#f5b83d"),
             ("次日竞价", str(n_ladder), "#6ab0ff"),
             ("扫描覆盖", f"{cov:.0f}%" if cov is not None else "—", "#9aa0a6")]
    # 顺序：先标题后数字 —— ServerChan 纯文本降级按 cell 换行要能读出
    # 「今日可下单 2」，反过来的话降级后只剩一串孤立数字。
    cells = "".join(
        f'<td width="25%" align="center" style="padding:7px 0">'
        f'<div style="font-size:11px;color:#9aa0a6">{_esc(k)}</div>'
        f'<div style="font-size:17px;font-weight:700;color:{c}">{_esc(v)}</div>'
        f'</td>'
        for k, v, c in items)
    return _card(_table(f'<tr>{cells}</tr>'), border="#2b313d")


def html_to_text(h):
    """HTML → 结构化纯文本（ServerChan 等不支持 HTML 的通道专用）。

    旧实现是 re.sub(r'<[^>]+>','') 粗暴剥标签，表格/卡片全部黏成一段，
    这是"版面不美观"在纯文本通道上的根因。这里按块级/单元格语义换行。
    """
    h = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</tr>", "\n", h)
    h = re.sub(r"(?i)</t[dh]>", "  ", h)
    h = re.sub(r"(?i)</(p|div|section|table|ul|ol|h\d)>", "\n", h)
    h = re.sub(r"(?i)<li[^>]*>", "· ", h)
    h = re.sub(r"(?is)<a[^>]*href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>",
               r"\2（\1）", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = html.unescape(h)
    out = []
    for ln in h.splitlines():
        ln = re.sub(r"[\t ]+", " ", ln).strip(" 　")
        if ln:
            out.append(ln)
    return "\n".join(out)

ACTION_BG = {"现在买": "#c0392b", "次日竞价达标买": "#b8860b",
             "等回踩": "#b8860b", "小仓试": "#b8860b",
             "观望": "#56606e", "禁买": "#8a2a2e", "未推荐": "#74808f",
             "条件满足": "#1e8e5a", "等待确认": "#b8860b", "数据不足": "#56606e",
             "超价取消": "#c0392b", "结构失效": "#c0392b", "到期失效": "#56606e"}


def _badge(text, bg):
    # white-space:nowrap —— 状态徽章（如"次日竞价达标买"）不得在窄屏折行
    return (f'<span style="background:{bg};color:#fff;border-radius:4px;'
            f'padding:2px 9px;font-size:12px;font-weight:700;'
            f'display:inline-block;line-height:1.7;white-space:nowrap">'
            f'{_esc(text)}</span>')


def _inline(s):
    # 三色动作标注（用户需求 2026-09-14）：加粗文本按动作语义着色——
    # 买入=红 / 卖出·止盈·减仓=绿 / 持有=蓝，其余保持深色。
    def _strong(m):
        t = m.group(1)
        color = "#e8eaed"
        if "买入" in t or "可买" in t:
            color = "#ff6b5e"
        elif any(k in t for k in ("卖出", "止盈", "减仓")):
            color = "#4ecf8e"
        elif "持有" in t:
            color = "#6ab0ff"
        return f'<strong style="color:{color}">{t}</strong>'
    return re.sub(r"\*\*(.+?)\*\*", _strong, s)


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


def _badge_color(text):
    """候选行首徽章的语义色（买入红/卖出绿/持有蓝/等待黄/禁买深红）。"""
    if "买入" in text or "可买" in text:
        return "#ff6b5e"
    if any(k in text for k in ("卖出", "止盈", "减仓")):
        return "#4ecf8e"
    if "持有" in text:
        return "#6ab0ff"
    if "禁买" in text or "破位" in text:
        return "#ff8a80"
    if "回踩" in text or "试" in text or "竞价" in text:
        return "#f5b83d"
    return "#9aa0a6"


def render_candidates(title, picks, extra_lines=()):
    """详情报告（落盘 dist/reports）：标题 + 候选表格卡 + 附加行。"""
    out = [f'<div style="{_STY["h1"]}">{_esc(title)}</div>']
    if picks:
        rows = ""
        for c in picks:
            line = _esc(_cand_line(c))
            # 行首徽章（✅买入 等）按语义着色，三色纪律在详情报告同样生效
            m = re.match(r"^([^\s]+)(.*)$", line, re.S)
            if m:
                bc = _badge_color(m.group(1))
                line = (f'<span style="color:{bc};font-weight:700">'
                        f'{m.group(1)}</span>{m.group(2)}')
            rows += ('<tr><td style="padding:5px 2px;'
                     'border-bottom:1px solid #2b313d">'
                     f'{line}</td></tr>')
        out.append(_card(_table(rows)))
    else:
        out.append(_card('<span style="color:#9aa0a6">今日无可买入标的'
                         '——没有机会就不凑数。</span>', accent="#3a4150"))
    for l in extra_lines:
        out.append(f'<div style="{_STY["li"]}">{_esc(l)}</div>')
    return f'<section style="{_STY["doc"]}">' + "".join(out) + "</section>"


# ---------------------------------------------------------------------------
# M35/N10 变化式主报告 + 标的卡片（状态 > 名称 > 价格/失效 > 理由 > 评分）
# ---------------------------------------------------------------------------

STATUS_CLS = {"条件满足": "#1e8e5a", "等待确认": "#b8860b", "数据不足": "#56606e",
              "超价取消": "#c0392b", "结构失效": "#c0392b", "到期失效": "#56606e"}


def _card(inner, border="#2b313d", accent=None):
    """卡片容器。accent = 左侧色条（首选红 / 备选灰），替代旧版无层次的白框。"""
    bar = f"border-left:3px solid {accent};" if accent else ""
    # <!--card--> 是裁剪哨兵：超限裁剪时按整张卡回退，绝不截半个标签
    return ('<!--card-->'
            f'<div style="border:1px solid {border};{bar}border-radius:8px;'
            f'padding:11px 12px;margin:10px 0;background:#1d222b">{inner}</div>')


def render_card(d, first=False, head=None, accent=None):
    """N10 统一标的卡片（表格化）。顺序：状态 → 名称 → 价格矩阵 → 失效 → 理由。

    head / accent 可覆盖（用于「等待更好买点」等非主推分组，
    避免它们顶着【备选观察】的标题混进主推位）。
    """
    zone = d.get("zone") or [None, None]
    zone_s = (f"{zone[0]:.2f} ~ {zone[1]:.2f}"
              if zone[0] and zone[1] else "—")
    cap = f"{zone[1] * 1.03:.2f}" if zone[1] else "—"
    status = d.get("status", "等待确认")
    badge = _badge(status, STATUS_CLS.get(status, "#6b7280"))
    if head is None:
        label = "首选观察" if first else "备选观察"
        if d.get("pool"):
            label = f"{label} · {d['pool']}"       # 池别上标题，一眼知策略来源
        head, head_color = (f"【{label}】", "#ff6b5e" if first else "#b0b8c4")
    else:
        head_color = "#b0b8c4"
    head = (f'<div style="color:{head_color};font-weight:700;font-size:13px;'
            f'margin:0 0 5px">{_esc(head)}</div>')
    close = d.get("close")
    price_s = f"{close:.2f}" if close else "—"
    dist = d.get("dist_pct")
    if dist:                       # 现价跳出买区必须显式说明，别让读者自己算
        dc = "#ff8a80" if dist > 0 else "#4ecf8e"
        price_s += (f' <span style="color:{dc};font-size:12px;font-weight:700">'
                    f'距买区 {dist:+.1f}%</span>')
    sl, sh = d.get("sell_low"), d.get("sell_high")
    target = f"{sl:.2f} ~ {sh:.2f}" if sl and sh else "—"
    tbl = _table(
        '<tr><td style="padding:0 0 7px">'
        f'<span style="font-size:16px;font-weight:700;color:#e8eaed">'
        f'{_esc(d.get("name"))}</span>'
        f'<span style="color:#9aa0a6;font-size:12px;margin-left:6px">'
        f' {_esc(d.get("code"))}</span></td>'
        f'<td align="right" valign="top" style="padding:0 0 7px">{badge}</td></tr>'
        + _row("现价", price_s)
        + _row("买入区间", f'<span style="color:#ff6b5e">{zone_s}</span>',
               v_bold=True)
        + _wide_row(_zone_bar(zone[0], zone[1], close))
        + _row("不追价上限", cap)
        # 三色纪律（2026-09-14）：买入红 / 卖出（目标区间）绿 / 止损深红
        + _row("目标区间",
               f'<span style="color:#4ecf8e;font-weight:700">{target}</span>')
        + _row("止损",
               f'<span style="color:#ff8a80;font-weight:700">'
               f'{d["stop"]:.2f}</span>' if d.get("stop") else "—")
        + (_row("建议仓位", _esc(d.get("position") or "1成"))
           if d.get("position") or first else "")
        + _row("有效期至", _esc(d.get("valid_until")))
        + _row("失效条件", _esc(d.get("invalid_if") or "条件破坏即失效")))
    inner = tbl
    if d.get("reason") or d.get("score") is not None:
        inner += ('<div style="color:#9aa0a6;font-size:13px;margin-top:9px;'
                  'border-top:1px dashed #2b313d;padding-top:7px">'
                  f'{_esc(d.get("reason") or "—")}'
                  '<span style="color:#9aa0a6;font-size:11px;margin-left:6px">'
                  f'评级 {_esc(d.get("research_grade", "—"))}'
                  f' · 分 {_esc(d.get("score", "—"))}</span></div>')
    if accent is None:
        accent = "#ff6b5e" if first else "#3a4150"
    return head + _card(inner, border=("#7a4440" if first else "#2b313d"),
                        accent=accent)


def render_brief(today, first, backups, changes, meta, ladder_next=(),
                 pending=(), prev_review=()):
    """M35 主报告（表格化版式）：今日速览 / 今日结论 / 首选 / 备选≤2 /
    等待更好买点 / 次日通道 / 昨日推荐复核 / 计划变化 / 数据说明。

    pending = 现价**不在**买区的票（等回踩等），独立分组并强制标注距买区——
    历史 bug：它们被混进"备选观察"，用户以为能照价下单。
    prev_review = 昨日推荐今日结局（#601-B 闭环），让读者知道推荐的票后来怎样。
    """
    cov = meta.get("coverage")
    cov_s = (f' · 扫描 {_esc(meta.get("universe"))} 只（覆盖 {cov}%）'
             if cov is not None else "")
    n_buy = (1 if first else 0) + len((backups or [])[:2])
    n_pending = len((pending or [])[:2])
    n_ladder = len((ladder_next or [])[:2])
    out = [f'<div style="{_STY["h1"]}">收盘观察 {_esc(today)}</div>',
           _summary_strip(meta, n_buy, n_pending, n_ladder),
           f'<div style="{_STY["meta"]}">'
           f'复核 {_esc(meta.get("reviewed", "—"))} 只 · '
           f'数据日期 {_esc(meta.get("data_date", today))} · '
           f'有效期至 {_esc(meta.get("valid_until", "—"))}{cov_s}</div>']
    if first:
        out.append(render_card(first, first=True))
    else:
        out.append(_card('<span style="color:#9aa0a6">今日无当下可买入的机会'
                         '——没有机会就不凑数。</span>',
                         border="#2b313d", accent="#3a4150"))
    for b in (backups or [])[:2]:
        out.append(render_card(b))
    if pending:
        out.append(f'<div style="{_STY["h2"]}">等待更好买点 · 现价不在买区</div>')
        out.append('<div style="color:#9aa0a6;font-size:12px;margin:0 0 6px">'
                   '以下标的现价已跳出买入区间，需回踩到位再买，'
                   '<b>不要按现价追</b>。</div>')
        for d in pending[:2]:
            out.append(render_card(d, head="【待回踩 · 勿按现价追】",
                                   accent="#f5b83d"))
    if ladder_next:
        out.append(f'<div style="{_STY["h2"]}">次日竞价确认 · 非即时可买</div>')
        for d in ladder_next[:2]:
            zone = d.get("zone") or [None, None]
            zs = f"{zone[0]:.2f} ~ {zone[1]:.2f}" if zone[0] and zone[1] else "—"
            out.append(_card(_table(
                '<tr><td style="padding:0 0 6px">'
                f'<span style="font-size:15px;font-weight:700;color:#e8eaed">'
                f'{_esc(d.get("name"))}</span>'
                f'<span style="color:#9aa0a6;font-size:12px;margin-left:6px">'
                f'{_esc(d.get("code"))}</span></td></tr>'
                + _row("达标条件", "高开≥2%~5%（按板数）")
                + _row("低开处理", "放弃（历史胜率仅24%）")
                + _row("关注区间", zs))
                + (f'<div style="color:#9aa0a6;font-size:12px;margin-top:6px">'
                   f'{_esc(d.get("gate_evidence") or "")}</div>'
                   if d.get("gate_evidence") else ""),
                border="#2b313d", accent="#f5b83d"))
    if prev_review:
        # #601-B 闭环：昨日推的票今天怎么样了——推荐不是一锤子买卖
        out.append(f'<div style="{_STY["h2"]}">昨日推荐 · 今日复核</div>')
        rows = "".join(
            '<tr><td style="padding:2px 8px 2px 0;color:#9aa0a6;'
            'font-size:13px;white-space:nowrap">'
            f'{_esc(p.get("name"))} {_esc(p.get("code"))}</td>'
            f'<td style="padding:2px 0;font-size:13px;font-weight:700;'
            f'color:{_status_color(p.get("status", ""))}">'
            f'{_esc(p.get("status"))}</td></tr>'
            for p in prev_review[:4])
        out.append(_card(_table(rows), border="#2b313d"))
    if changes:
        out.append(f'<div style="{_STY["h2"]}">计划变化</div>')
        rows = "".join(
            '<tr><td style="padding:3px 10px 3px 0;color:#9aa0a6;'
            'font-size:13px;white-space:nowrap">'
            f'{_esc(c.get("code"))}</td>'
            f'<td style="padding:3px 0">{_badge(c.get("new", ""), STATUS_CLS.get(c.get("new"), "#6b7280"))}'
            f' <span style="color:#9aa0a6;font-size:12px">'
            f'{_esc(c.get("reason") or "")}</span></td></tr>'
            for c in changes[:8])
        out.append(_card(_table(rows), border="#2b313d"))
    if meta.get("note"):
        out.append(f'<div style="{_STY["meta"]}">{_esc(meta.get("note"))}</div>')
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
    """超限按「整张卡片」回退裁剪（截半个标签会整版错乱）。

    旧实现对 <p>/<li>/<h> 做正则切块，而现版本面用 <div> 卡片 + <table>，
    正则匹配不到 → 超限时返回空串，推送直接白屏。
    """
    if len(content) <= PP_HTML_CAP:
        return content
    parts = content.split("<!--card-->")
    if len(parts) == 1:
        return content[:PP_HTML_CAP]
    head, cards = parts[0], ["<!--card-->" + p for p in parts[1:]]
    while cards and len(head) + sum(len(c) for c in cards) > PP_HTML_CAP:
        cards.pop()
    return head + "".join(cards)


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


def _daily_sent(con, mode, date):
    """同 mode 同日期是否已有 sent 记录（state 账本 + dist 镜像双查）。

    2026-09-14 晚新增：GitHub 自带 cron 幽灵延迟让同 mode 流水线一天跑多次
    （当晚 build_close 20:47 与 22:50 各推一条），候选集合一变 biz_key 就拦不住。
    这里按 mode+日期兜底——收盘复盘/盘前计划这类消息，一天一条才是正确语义。
    """
    try:
        row = con.execute(
            "SELECT 1 FROM push_ledger WHERE mode=? AND ts LIKE ? "
            "AND status='sent' LIMIT 1",
            (mode, date + "%")).fetchone()
    except Exception:
        row = None
    if row:
        return True
    if os.path.exists(DIST_LEDGER):
        try:
            with open(DIST_LEDGER, "r", encoding="utf-8") as f:
                dist = json.load(f)
        except Exception:
            return False
        for v in dist.values():
            if (isinstance(v, dict) and v.get("mode") == mode
                    and str(v.get("ts", "")).startswith(date)
                    and v.get("status") == "sent"):
                return True
    return False


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
    # 日级保险丝：同 mode 同日期已 sent → 拦截（force=True 可绕过）。
    # 触发端重复（GitHub cron 幽灵延迟）的最后一道防线——候选集合变了
    # biz_key 不同照样拦。失败/不确定的首次推送不拦，次日触发可补发。
    if not force and _daily_sent(con, mode, date):
        return {"sent": False, "dedup": True, "key": key, "daily_gate": True}
    # ⚠️ 2026-09-16 修（血案：补发历史会**吃掉当日额度**）：
    # `ts` 的**日期部分必须用交易日 `date`**，不能用「当前日期」。
    # 反例：09-16 凌晨以 `--date 2026-09-15` 补发昨天的收盘报告时，ts 被写成
    # `2026-09-16 …` ⇒ 之后 `_daily_sent(con,'build_close','2026-09-16')`
    # 命中该条 ⇒ **当天 15:22 真正的收盘推送被自家保险丝拦掉**
    # （恰好与「补发历史不占当日额度」的承诺相反——承诺写在文档里，
    #  而实现才是准的，这次以实现为准修数据模型）。
    # 当日正常推送时 date == today ⇒ ts 与旧行为逐字节一致，零影响。
    ts = f"{date} {datetime.now(_CST).strftime('%H:%M:%S')}"
    if not force and _reconcile(con, key, mode, ts, True):
        return {"sent": False, "dedup": True, "key": key}
    # 网页端入口：按钮式（旧版把 50+ 字符裸 URL 直接铺在正文末尾，
    # 手机上换行成一坨，是版面难看的一大来源）
    site_url = cfg.get("site_url") or "https://aprildream24.github.io/astock-system/"
    content += ('<div style="margin-top:16px;padding-top:12px;'
                'border-top:1px solid #2b313d;text-align:center">'
                f'<a href="{site_url}" style="display:inline-block;'
                'background:#1a73e8;color:#fff;text-decoration:none;'
                'border-radius:6px;padding:9px 20px;font-size:14px;'
                'font-weight:700">📊 打开网页版完整详情</a>'
                '<div style="color:#9aa0a6;font-size:11px;margin-top:7px">'
                '访问口令见 config/users.json / SITE_USERS</div></div>')
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
                # ⚠️ 2026-09-16 修（血案：PushPlus 是当前唯一通道，却无兜底）：
                # 原实现只在 **wxpusher 全失败** 时才落 ServerChan 备用
                #（第 672 行 `all_failed`），而**主通道是 PushPlus** 的部署里，
                # PushPlus 一挂（额度耗尽/接口变更/被墙）就**直接零送达**——
                # `results` 里只有一条 failed，聚合 worst=failed，
                # 但用户端什么也收不到，且没有任何第二通道补位。
                # 实测本仓库 config/notify.json 正是这种形态：
                #   primary_channel=pushplus，wxpusher_accounts=[]，
                #   serverchan_key=''（本地空）
                # 而 CI 侧 `SERVERCHAN_KEY` **Secret 已注入**（workflow 已配），
                # 只是代码从不读它作 PushPlus 的兜底 ⇒ 白白浪费一条备用通道。
                # 修法：PushPlus 明确 failed（非 uncertain，避免双发）时，
                # 若有 serverchan_key 则补发一条——与 wxpusher 的兜底对称。
                if st == "failed" and cfg.get("serverchan_key"):
                    st2, d2 = _send_serverchan(
                        cfg["serverchan_key"],
                        f"【{tag}·备用SC】{title}", content)
                    results["serverchan"] = {"status": st2, "detail": d2,
                                             "role": "fallback"}
                    print(f"[notify] PushPlus failed → ServerChan 兜底 {st2}")
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
    # 2026-09-15 修复：原实现无条件 return {"sent": True}，即使 worst=="failed"
    # （所有通道都拒收）也对外报成功 → 调用方 print 看到 sent=True，全通道失败
    # 被伪装成已送达，属于静默失败。sent 必须真实反映 worst 聚合结果。
    return {"sent": worst == "sent", "key": key, "status": worst,
            "results": results}


def _transport_failed(e):
    """判断异常是否属于**连接尚未建立**层面的失败（可安全重试）。

    区分意义（2026-09-16）：TLS 握手超时/连接被拒 ⇒ 请求根本没送达，
    重试绝不会造成"重复送达"；而请求已发出后的读超时则**可能**已受理，
    盲目重试会导致用户收到两条。本函数只对前者返回 True。"""
    cur, hops = e, 0
    while cur is not None and hops < 5:
        if isinstance(cur, (ssl.SSLError, ConnectionError, socket.gaierror)):
            return True
        if "handshake" in str(cur).lower():
            return True
        cur = getattr(cur, "reason", None)
        hops += 1
    return False


def _send_serverchan(key, title, content, _retries=3):
    """返回 (status, detail)。status ∈ sent/failed/uncertain（M37）。

    超时/连接错误 = 受理不确定，不盲目重试双发。
    ⚠️ 2026-09-16 细化：**连接未建立**（TLS 握手失败/连接被拒）必然未送达
    ⇒ 可安全重试（见 `_transport_failed`）；仅"已发出但读超时"保持不重试。
    ServerChan 不支持 HTML：走 html_to_text 结构化降级，保留分行与对齐，
    不再用 re.sub 粗暴剥标签（会把卡片黏成一坨）。"""
    last = ""
    for i in range(_retries):
        try:
            data = urllib.parse.urlencode(
                {"title": title, "desp": html_to_text(content)}
            ).encode()
            req = urllib.request.Request(
                f"https://sctapi.ftqq.com/{key}.send", data=data)
            urllib.request.urlopen(req, timeout=10)
            return "sent", "ok"
        except urllib.error.HTTPError as e:
            return "failed", core.redact(str(e), key)
        except Exception as e:  # noqa: BLE001 — 超时/网络错误：受理状态未知
            last = core.redact(str(e), key)
            if not _transport_failed(e) or i >= _retries - 1:
                return "uncertain", last
            print(f"[notify] ServerChan 连接未建立，重试 {i + 1}/{_retries}")
            time.sleep(1.5 * (i + 1))
    return "uncertain", last


def _send_pushplus(token, title, content, _retries=3):
    """M36 主推通道。返回 (status, detail)。status ∈ sent/failed/uncertain。

    ⚠️ 2026-09-16 修（血案：08:50 盘前推送**彻底丢失**，用户当天没收到任何
    盘前计划）。CI run 35041635767 实证：
        pushplus → '<urlopen error _ssl.c:993: The handshake operation timed out>'
    旧实现单次 `urlopen(timeout=10)`、**无重试**，于是：
      ① TLS 握手都没完成 ⇒ 请求必然没送达（可安全重试）；
      ② 返回 uncertain ⇒ 不触发任何兜底分支 ⇒ 用户零送达且日志只有一行。
    修法：传输层失败（连接建立失败/超时/5xx）**重试 3 次、退避 1.5s/3s**；
    4xx 是确定性拒绝（token 失效/参数错），重试无意义，直接判 failed。
    重试后仍不成功 → 保持 uncertain 语义（受理状态未知，不盲目双发）。

    另注：`_ssl.c:993` 握手超时是 GitHub runner 到 pushplus.plus 的偶发网络
    问题（同一 run 内其他 HTTPS 全部正常），不是 token/接口问题——
    因此重试是最对症的修法。"""
    last = ""
    for i in range(_retries):
        try:
            body = json.dumps({"token": token, "title": title,
                               "content": content[:PP_HTML_CAP],
                               "template": "html"}).encode()
            req = urllib.request.Request(
                "https://www.pushplus.plus/send", data=body,
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            if i:
                print(f"[notify] PushPlus 第 {i + 1} 次尝试成功")
            return "sent", "ok"
        except urllib.error.HTTPError as e:
            last = core.redact(str(e), token)
            if 400 <= e.code < 500:      # 确定性拒绝，重试无意义
                return "failed", last
            print(f"[notify] PushPlus HTTP {e.code}，重试 {i + 1}/{_retries}")
        except Exception as e:  # noqa: BLE001 — 握手/超时/连接失败：未送达
            last = core.redact(str(e), token)
            print(f"[notify] PushPlus 传输失败 {type(e).__name__}，"
                  f"重试 {i + 1}/{_retries}")
        if i < _retries - 1:
            time.sleep(1.5 * (i + 1))
    return "uncertain", f"{last}（已重试 {_retries} 次）"
