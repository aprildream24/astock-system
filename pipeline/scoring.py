# -*- coding: utf-8 -*-
"""统一评分 / 环境加权 / top_picks 终审 / 胜率熔断闸。"""
import hashlib

from . import engines, mktfilter


# ---------------------------------------------------------------------------
# 3.7 市场环境加权 env_bias
# ---------------------------------------------------------------------------

def env_weights(promote_rate, zhaban_rate, emotion):
    """M09/M10：环境加权唯一入口（build 全部走这里，固定连乘顺序）。
    口径约定：promote_rate = 连板晋级率（与情绪表"连板晋级率"同源
    同算，均出自 zt_pool，禁止另一处另算）。"""
    return env_bias(promote_rate, zhaban_rate, emotion)


def env_bias(promote_rate, zhaban_rate, emotion):
    """单位归一：>1 视为百分数自动 /100。返回 {连板,趋势,波段,区间} 权重。"""
    if promote_rate > 1:
        promote_rate /= 100
    if zhaban_rate > 1:
        zhaban_rate /= 100
    w = {"连板": 1.0, "趋势": 1.0, "波段": 1.0, "区间": 1.0}
    if promote_rate >= 0.55 and zhaban_rate <= 0.30:
        w["连板"] *= 1.25
    if zhaban_rate >= 0.40:
        w["连板"] *= 0.70
    if promote_rate < 0.40:
        w["连板"] *= 0.85
    if emotion >= 60:
        w["趋势"] *= 1.15
    # 情绪差：只降连板，不抬波段/区间（退潮做波段已被实证推翻）
    return w


# ---------------------------------------------------------------------------
# 3.11 统一评分
# ---------------------------------------------------------------------------

def grade(score):
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def position_hint(pool, score):
    if pool == "连板" and score >= 55:
        return "1~2成"
    if pool == "趋势" and score >= 70:
        return "2~3成"
    if pool in ("波段", "区间") and score >= 70:
        return "2成"
    return "1成"


def score_candidate(c, env_w):
    """统一评分：各池基础分 → 环境加权 → eff_score。"""
    pool = c["pool"]
    if pool == "连板":
        rr = max(0.0, (c.get("t1", c["close"]) - c["close"]) / c["close"])
        base = 55 + min(25, rr * 12 * 100) + min(15, c.get("reach10", 0.2) * 15)
    elif pool == "趋势":
        base = c.get("worth_score", c.get("kscore", 50))
        base += 4 if c.get("trend_state") == "加速上行" else (
            -4 if c.get("trend_state") == "增速放缓" else 0)
    elif pool == "区间":
        base = 15 + max(0.0, min(70.0, c.get("worth", 0)))
    else:  # 波段
        base = c.get("worth", 50)
    score = base * env_w.get(pool, 1.0)
    # ── Alpha 因子加成（2026-09-25 融入，engines.alpha_extras 供数）──
    # 全部有小幅上限，方向错了同样扣分——因子是修正项不是主引擎。
    alpha = c.get("alpha") or {}
    if alpha.get("vol_squeeze") and alpha["vol_squeeze"] < 0.8             and c.get("trend_state") != "增速放缓":
        score += 3        # 振幅收缩 + 趋势未坏 = 变盘向上预备（BOLL 收口族）
    if alpha.get("corr_pv") is not None and alpha["corr_pv"] >= 0.3:
        score += 2        # 量价同向 = 健康上涨；背离票不加
    if alpha.get("corr_pv") is not None and alpha["corr_pv"] <= -0.3:
        score -= 3        # 量价背离 = 虚涨，压分
    if c.get("donchian"):
        score += 3        # 20 日通道突破日（海龟经典入场点）
    score = max(0.0, score)
    return round(score, 2)


# ---------------------------------------------------------------------------
# 3.9 胜率熔断闸
# ---------------------------------------------------------------------------

def tag_winrate(con, days=30, min_n=10, threshold=45.0, today=None):
    """对每个策略 tag 统计近 N 日推荐票的次日胜率。
    胜率 <45% 且样本 ≥10 → observe。"""
    out = {}
    rows = con.execute(
        "SELECT tag, outcome, COUNT(*) FROM rec_picks WHERE date >= date(?, ?) "
        "GROUP BY tag, outcome", (today or "", f"-{days} day")).fetchall()
    agg = {}
    for tag, outcome, n in rows:
        a = agg.setdefault(tag, {"win": 0, "n": 0})
        a["n"] += n
        if outcome in ("win", "tomorrow_up"):
            a["win"] += n
    for tag, a in agg.items():
        wr = a["win"] / a["n"] * 100 if a["n"] else None
        out[tag] = {"winrate": wr, "n": a["n"],
                    "observe": bool(wr is not None and a["n"] >= min_n
                                    and wr < threshold)}
    return out


# ---------------------------------------------------------------------------
# 3.8 top_picks 终审（唯一权威排序，三池合并）
# ---------------------------------------------------------------------------

WINRATE_ANCHOR = {"连板": 1.0, "趋势": 0.72, "波段": 0.72, "区间": 0.72}
ACTION_RANK = {"现在买": 2, "次日竞价达标买": 2, "等回踩": 1, "小仓试": 1, "观望": 0}


def compute_top_picks(cands, env_w, winrates, sector_of=None, limit=3,
                      per_sector=1, ladder_cap=2):
    """cands: 已过熔断闸的候选 list；返回最终推荐（允许 0 只）。

    参数化（2026-09-18 用户需求「行情好时不再限制 3 只」）：
      · `limit=None` → **不限总量**，全部符合条件的高分标的都推；
      · `per_sector` → 同板块最多保留几只（行情一般 =1，热点行情放宽）；
      · `ladder_cap` → 连板池席位上限。
    默认值（3 / 1 / 2）与原实现**逐字等价**，老调用方行为不变。
    """
    scored = []
    for c in cands:
        if c.get("observe"):
            continue                      # 双保险：observe 票不进终审
        pool = c["pool"]
        eff = score_candidate(c, env_w) * WINRATE_ANCHOR.get(pool, 0.72)
        # 躺榜衰减（用户 2026-09-19：「不要几天横排在那里动都不动」）：
        # 同一只票连续多日挂在推荐位却始终未兑现 → 每天 8% 折价，
        # 连续 ≥5 日直接移出（build 侧也会拦，这里是双保险）。
        wd = c.get("wait_days") or 0
        if wd >= 5:
            continue
        if wd > 1:
            eff *= 0.92 ** (wd - 1)
        # 优选因子：板块冷热 / 趋势双态
        # ⚠️ 2026-09-18 前 `sector_temp` 无任何数据源（恒 None）⇒ 本因子静默
        # 失效。数据源已由 pipeline/sector.py 补上，加成重新生效。
        if c.get("sector_temp") == "❄弱":
            eff *= 0.90
        elif c.get("sector_temp") == "🔥强":
            eff *= 1.03
        if c.get("trend_state") == "增速放缓":
            eff *= 0.90
        elif c.get("trend_state") == "加速上行":
            eff *= 1.05
        # RS 超额动量因子（2026-09-19，engines.rs_momentum 供数）：
        # 跑赢大盘 ≥5% ×1.05；跑输 ≥5% ×0.95——绝对动量相同的前提下，
        # 相对强度才是横截面排序的信息来源（Jegadeesh & Titman 1993）。
        rs = c.get("rs_mom")
        if rs is not None:
            if rs >= engines.RS_STRONG:
                eff *= 1.05
            elif rs <= engines.RS_WEAK:
                eff *= 0.95
        c["eff_score"] = round(eff, 2)
        scored.append(c)
    scored.sort(key=lambda c: (c["eff_score"], ACTION_RANK.get(c.get("action"), 0)),
                reverse=True)
    # 板块内限额：同板块保留分数最高的前 per_sector 只。
    # ⚠️ 旧实现的 `sector_of` 因候选无 `sector` 字段而退化成「按池别去重」——
    # 不同行业的波段票被当成同一板块互斥，每次只活一只。现已改为真实行业。
    if sector_of and per_sector:
        seen, merged = {}, []
        for c in scored:
            sec = sector_of(c)
            n = seen.get(sec, 0)
            if n >= per_sector:
                continue
            seen[sec] = n + 1
            merged.append(c)
        scored = merged
    # 类型配额：连板 ≤ ladder_cap 席
    picked, ladder = [], 0
    for c in scored:
        if c["pool"] == "连板":
            if ladder >= ladder_cap:
                continue
            ladder += 1
        picked.append(c)
        if limit is not None and len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------------------
# 3.12 行情档位 → 推荐配额（2026-09-18 用户需求）
# ---------------------------------------------------------------------------

NORMAL_PICKS = 8        # 行情一般：用户 2026-09-22「不再限定 3 个，10 个以内，标注池别与高/中/低位」
HOT_PICKS = None        # 行情好：**不限量**（None = 全部符合条件标的）


def market_heat(emo):
    """行情档位 → (level, max_picks, per_sector, ladder_cap)。

    用户口径：「行情好时针对评分高的个股**全部推荐**，标注板块热度，
    不再限制 3 个」。口径落地要点：
      · 「行情好」= 十维情绪分 ≥60（偏热）且**数据达标**（qualified）。
        数据未达标时情绪分本身不可信 ⇒ 一律不放开（宁少不滥）。
      · 行情好时 `max_picks=None`（不限量），同时放宽板块内限额与连板席位
        ——否则热点板块刚启动时只能推 1 只，等于把最强的板块主动减配。
      · 行情一般/偏冷：维持 3 只不变（不因本次改动收紧，避免用户感知突变）。
    """
    if not emo or emo.get("score") is None:
        return "未知", NORMAL_PICKS, 2, 2
    score = float(emo.get("score"))
    level = emo.get("label") or "未知"
    if not emo.get("qualified"):
        return level, NORMAL_PICKS, 2, 2      # 覆盖不足 ⇒ 情绪分不用于加权
    if score >= 76:                            # 亢奋
        return level, HOT_PICKS, 3, 4
    if score >= 60:                            # 偏热
        return level, HOT_PICKS, 2, 3
    if score >= 30:                            # 均衡/偏冷：上限 8（用户 09-22）
        return level, NORMAL_PICKS, 2, 2
    return level, 5, 1, 1                      # 冰点：最多 5，宁缺毋滥



def observe_mute(cands, winrates):
    """胜率熔断：tag 胜率不达标 → observe=True（全通道一致的前提）。"""
    for c in cands:
        wr = winrates.get(c.get("tag", ""))
        if wr and wr.get("observe"):
            c["observe"] = True
    return cands


# ---------------------------------------------------------------------------
# 决策 _decide：每股唯一操作结论（渲染层不重判）
# ---------------------------------------------------------------------------

# 引擎四态 → 推送动作（唯一映射表）。
# 历史 bug：_decide 只用退化的买区做判定，把引擎已经判为「过热/勿追」的票
# 又判成「现在买」（实测 14 只）——四态是权威，买区只是报价，不得反客为主。
ACTION_HINT_MAP = {"现在买": "现在买", "小仓试": "小仓试", "等回踩": "等回踩",
                   "勿追": "观望", "禁买": "禁买"}

MAX_ZONE_WIDTH = 0.08      # 买区宽度红线（>8% 视为伪区间，不推）
MIN_UPSIDE_X = 1.02        # 目标区上沿 ≥ 买区上沿×1.02（必须有盈利空间）


def buy_zone_ok(c, max_width=MAX_ZONE_WIDTH):
    """买区自洽性闸门：过宽 / 倒挂 / 无盈利空间 / 整体高于现价 → 不推。

    推出去的票必须能「照着价格下单」：买区是一段窄带，不是统计区间。
    """
    lo, hi = c.get("buy_low"), c.get("buy_high")
    if not lo or not hi or lo <= 0 or hi <= lo:
        return False
    if (hi - lo) / lo > max_width:
        return False
    close = c.get("close")
    if close and lo > close * 1.15:
        return False
    sh = c.get("sell_high")
    if sh is not None and sh < hi * MIN_UPSIDE_X:
        return False
    return True


def is_buyable_now(c):
    """「这只票现在能不能照价下单」的唯一出口（用户需求 2026-09-13）。

    历史事故：主推位只校验 action∈(现在买/等回踩/小仓试) 就上台，结果把现价
    早已跳出买区的票推给读者——点开一看根本买不了，这就是"推的票不在购买
    区间"的直接来源。此处把「可下单」收敛成六重闸门，任一不过即为 False：
      1) 市场准入：沪深主板/创业板可买，科创板/北交所/其它一律否
      2) 未闯熔断：observe / broken 不进可执行名单
      3) 当日涨停：封死买不进 → 走次日竞价确认通道
      4) 引擎四态：action 必须是「现在买」（等回踩/小仓试不算可执行）
      5) 买区自洽：窄带 / 不倒挂 / 有盈利空间（buy_zone_ok）
      6) 现价在区内：dist_pct == 0（跳出买区 = 不可照价下单）
    渲染层禁止自己重判，只准读本函数结果。"""
    code = c.get("code") or ""
    num = code[2:] if code[:2] in ("sh", "sz") else code
    if not mktfilter.tradable(num):
        return False
    if c.get("observe") or c.get("broken"):
        return False
    if c.get("limit_up"):
        return False
    if c.get("action") != "现在买":
        return False
    if not buy_zone_ok(c):
        return False
    return dist_pct(c) == 0


def dist_pct(c):
    """现价相对买区的偏离（负=低于下沿，正=高于上沿，0=区内）。"""
    lo, hi, close = c.get("buy_low"), c.get("buy_high"), c.get("close")
    if not (lo and hi and close):
        return None
    if close < lo:
        return round((close / lo - 1) * 100, 1)
    if close > hi:
        return round((close / hi - 1) * 100, 1)
    return 0.0


def _decide(c, today=None):
    """动作 ∈ {现在买, 等回踩, 次日竞价达标买, 观望, 禁买}。"""
    if c.get("observe"):
        return "观望"
    if c.get("broken"):
        return "禁买"
    close = c["close"]
    if c["pool"] == "连板":
        gap = c.get("gap_pct")
        if gap is None:
            return "次日竞价达标买"        # 收盘时点：等明日竞价达标确认
        follow, watch = engines.auction_discipline(c.get("streak", 1), gap)
        if follow:
            return "现在买"                # 竞价达标 → 开盘买（🔥优选）
        return "禁买" if not watch else "观望"   # 低开放弃 → 禁买
    lo, hi = c["buy_low"], c["buy_high"]
    hint = c.get("action_hint")
    if hint in ACTION_HINT_MAP:
        act = ACTION_HINT_MAP[hint]
        # 四态说能买，仍要落在买区内才成立（防报价漂移）；
        # 跳出买区回落成"等回踩"也要带上界——飞在天上的不算等回踩（观望）
        if act == "现在买":
            if lo <= close <= hi:
                return "现在买"
            return "等回踩" if close <= hi * 1.06 else "观望"
        return act
    if lo <= close <= hi:
        return "现在买"
    # 等回踩带上界+下界（2026-09-14 用户口径：不要飞在天上的，也不要已破位的）：
    # ① 略高于上沿 6% 内 → 等回踩（回踩进区就买）
    # ② 略破下沿 3% 内（回踩下沿）→ 等回踩
    # ③ 更远的（飞天上 / 深破位）→ 观望或禁买，绝不以"等回踩"误导
    up_band = hi < close <= hi * 1.06
    dn_band = lo * 0.97 <= close < lo
    if up_band or dn_band:
        return "等回踩"
    if c.get("stop") and close <= c["stop"]:
        return "禁买"
    return "观望"


def sid_of(strategy, code, date):
    return hashlib.sha1(f"{strategy}|{code}|{date}".encode()).hexdigest()[:16]
