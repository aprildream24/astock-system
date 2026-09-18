# -*- coding: utf-8 -*-
"""推送版面本地预览：用真实库数据渲染一份推送，落盘 HTML 供直接查看。

用法：python tools/push_preview.py [--date YYYY-MM-DD]

为什么要这个工具：
  推送版面改动不该靠脑补验收——serverchan/pushplus 一封 pushes 就是出去了，
  收不回来。这里用**真实 K 线数据**渲染出微信 webview 版（HTML）与
  ServerChan 降级版（纯文本）并排落盘，改版面后先看一眼再发。

与 build.py 的关系：只复用「扫描 → 评分 → 决策 → 渲染」这条链，
不碰交易日守门 / 推送去重 / 落库——预览不该污染正式账本。
"""
import argparse
import html as _html
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import build as bld, notifier, scoring, sector  # noqa: E402
from pipeline.core import BASE_DIR, get_conn, trade_calendar  # noqa: E402

OUT = os.path.join(BASE_DIR, "dist", "reports", "push_layout_preview.html")


def _latest_kl_date(con):
    return con.execute("SELECT MAX(date) FROM klines").fetchone()[0]


def _emo_from_db(con, date):
    """从 emotion_log 取当日情绪 —— 预览必须与正式推送同口径。

    读不到就返回 None（market_heat 会退回"不放开"），**不凭空假设行情好**：
    预览版面的可信度全靠"和线上一样"。
    """
    try:
        from pipeline import emotion
        row = con.execute(
            "SELECT score, qualified FROM emotion_log WHERE date=?",
            (date,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    return {"score": row[0], "qualified": bool(row[1]),
            "label": emotion.label(row[0])}


def build_preview_cards(con, date, force_hot=False):
    """扫描 → 评分 → 决策（与 build.build 同口径，但不写任何账本）。

    ⚠️ 2026-09-18：预览必须与正式推送**同口径**，否则预览就失去了意义。
    板块标注与行情档位（放开限量）一并接入——这正是本次改动的两块内容。
    """
    cal = trade_calendar(con)
    valid_until = (cal[min(cal.index(date) + 5, len(cal) - 1)]
                   if date in cal else "下一交易日复核")
    cands, skipped = bld.scan_all(con, date)
    winrates = scoring.tag_winrate(con, today=date)
    cands = scoring.observe_mute(cands, winrates)
    env_w = {"连板": 1.0, "趋势": 1.0, "波段": 1.0, "区间": 1.0}
    sector_board, hot_sectors = {}, []
    try:
        _board = sector.refresh(con, date)
        sector_board, hot_sectors = sector.annotate(con, date, cands,
                                                    board=_board)
    except Exception as e:  # noqa: BLE001 — 板块标注失败不影响预览
        print(f"[preview] 板块标注失败：{type(e).__name__} {e}")
    for c in cands:
        c["score"] = scoring.score_candidate(c, env_w)
        c["position"] = scoring.position_hint(c["pool"], c["score"])
        c["action"] = scoring._decide(c)
        c["buyable_now"] = scoring.is_buyable_now(c)
    emo = {"score": 80.0, "qualified": True, "label": "亢奋"} if force_hot \
        else _emo_from_db(con, date)
    heat_level, pick_limit, per_sector, ladder_cap = scoring.market_heat(emo)
    if force_hot:
        heat_level = (heat_level or "") + "(强制模拟)"
    print(f"[preview] 行情档位={heat_level} 限量={pick_limit} "
          f"同板块≤{per_sector} 连板≤{ladder_cap}")
    now_actions = ("现在买", "等回踩", "小仓试")
    picks = scoring.compute_top_picks(
        [c for c in cands if c.get("action") in now_actions],
        env_w, winrates, sector_of=lambda c: c.get("sector") or c["pool"],
        limit=pick_limit, per_sector=per_sector, ladder_cap=ladder_cap)
    ladder = scoring.compute_top_picks(
        [c for c in cands if c.get("action") == "次日竞价达标买"],
        env_w, winrates, sector_of=lambda c: c.get("sector") or c["pool"],
        limit=2, ladder_cap=ladder_cap)

    def card(c, status):
        d = {"code": c["code"], "name": c.get("name", ""),
             "strategy": c.get("pool", ""), "status": status,
             "action": c.get("action"),
             "reason": c.get("entry_hint") or c.get("cycle_hint", ""),
             "zone": [c.get("buy_low"), c.get("buy_high")],
             "stop": c.get("stop"), "valid_until": valid_until,
             "invalid_if": "条件破坏即失效", "pool": c.get("pool"),
             "close": c.get("close"), "dist_pct": c.get("dist_pct"),
             "sell_low": c.get("sell_low"), "sell_high": c.get("sell_high"),
             "score": c.get("score"), "position": c.get("position"),
             "research_grade": scoring.grade(c.get("score", 0))}
        d.update(bld._sector_fields(c))
        return d

    first = None
    backups = []
    pending = []
    for c in picks:
        d = card(c, "条件满足" if c.get("buyable_now") else "等待确认")
        if c.get("buyable_now"):
            if first is None:
                first = d
            else:
                backups.append(d)
        else:
            pending.append(d)
    ladder_cards = [card(c, "等待确认") for c in ladder]
    prev_review = build_prev_review(con, date, cands + ladder)
    return (cands, skipped, first, backups, pending, ladder_cards, prev_review,
            {"heat_level": heat_level, "hot_sectors": hot_sectors,
             "pick_limit": pick_limit})


def build_prev_review(con, date, cands, ladder_cards=None):
    """昨日推荐今日复核。库里没有更早的推荐 → 用明确标注的示例行占位，
    目的只为了展示版式，示例行一眼可辨，不会冒充真实复盘数据。"""
    rows = []
    for p in bld.prev_picks_of(con, date)[:4]:
        rows.append({"code": p["code"], "name": p.get("name", ""),
                     "status": notifier._prev_pick_status(
                         p, cands, None, compact=True)})
    if rows:
        return rows
    return [{"code": "sh600xxx", "name": "（版式示例·非真实数据）昨日主推",
             "status": "🟢还在跟"},
            {"code": "sh600yyy", "name": "（版式示例·非真实数据）昨日备选",
             "status": "🟡略高"}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--heat", choices=["hot"], default=None,
                    help="强制按行情好的档位渲染（验证放开限量的版面）")
    a = ap.parse_args()
    con = get_conn()
    date = a.date or _latest_kl_date(con)
    print(f"[preview] 渲染日期 {date}（全市场扫描中，约需数十秒）…")
    (cands, skipped, first, backups, pending, ladder, prev_review,
     heat) = build_preview_cards(con, date, force_hot=(a.heat == "hot"))
    cov = dict(bld.LAST_SCAN_COVERAGE)
    print("[preview] 覆盖：", cov)
    lim = heat.get("pick_limit")
    meta = {"reviewed": len(cands), "data_date": date,
            "valid_until": (first or next(iter(backups), {}) or {}).get(
                "valid_until", "下一交易日复核"),
            "coverage": cov.get("coverage"),
            "universe": cov.get("universe"),
            "heat_level": heat.get("heat_level"),
            "hot_sectors": heat.get("hot_sectors"),
            "note": "预览：口径与正式推送一致，未写入任何账本。"
                    + (f"行情{heat.get('heat_level')}，"
                       + ("已放开限量（全部符合条件标的）" if lim is None
                          else f"维持 TOP{lim}") + "。")}
    brief = notifier.render_brief(date, first, backups, [], meta,
                                  ladder_next=ladder,
                                  pending=pending,
                                  prev_review=prev_review)
    text = notifier.html_to_text(brief)
    counts = {"可下单": sum(1 for x in [first] + backups if x),
              "待回踩": len(pending), "次日竞价": len(ladder),
              "候选": len(cands), "剔除留痕": len(skipped)}
    page = f"""<!doctype html><meta charset="utf-8">
<title>推送版面预览 {date}</title>
<div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
            max-width:1080px;margin:0 auto;padding:18px;color:#222">
<h2 style="margin:0 0 4px">推送版面预览 · {date}</h2>
<p style="color:#777;font-size:13px;margin:0 0 14px">
数据源：本地 cache/market.db（真实 K 线）。左＝微信/PushPlus webview（HTML）；
右＝ServerChan 降级（纯文本）。</p>
<div style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start">
  <div style="flex:1 1 420px;min-width:360px;border:1px solid #e3e3e3;
              border-radius:10px;padding:14px;background:#fff">
    <div style="color:#999;font-size:12px;margin-bottom:8px">① HTML 通道</div>
    {brief}
  </div>
  <div style="flex:1 1 380px;min-width:320px;border:1px solid #e3e3e3;
              border-radius:10px;padding:14px;background:#fff">
    <div style="color:#999;font-size:12px;margin-bottom:8px">② 纯文本降级</div>
    <pre style="white-space:pre-wrap;font-size:13px;line-height:1.7;
                margin:0;font-family:Consolas,Monaco,monospace">{
        _html.escape(text)}</pre>
  </div>
</div>
<div style="margin-top:16px;padding:12px;border:1px solid #e3e3e3;
            border-radius:10px;background:#fafafa;font-size:13px">
  <b>扫描口径核对</b><br>
  有效标的 {cov.get('universe')} 只 · 当日K线齐全 {cov.get('with_bar')} 只 ·
  缺口 {cov.get('missing_bar')} 只 · 覆盖率 <b>{cov.get('coverage')}%</b><br>
  已退市/未上市/停牌（不计缺口）{cov.get('untradable')} 只 ·
  陈旧K线 {cov.get('stale')} · 缺历史 {cov.get('no_history')} ·
  涨停池 {cov.get('zt_pool')} 只<br>
  候选 {counts['候选']} · 剔除留痕 {counts['剔除留痕']} ·
  今日可下单 {counts['可下单']} · 待回踩 {counts['待回踩']} ·
  次日竞价 {counts['次日竞价']}
</div>
</div>"""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"[preview] 已生成 {OUT}")
    print(f"[preview] 计数 {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
