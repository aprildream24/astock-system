# -*- coding: utf-8 -*-
"""技巧注册表（TECHNIQUES）：四类技巧的唯一登记处。

红线：**技巧只增不减**。下线一个技巧需实证 + 用户同意；
tools/check_strategy_lock.py 以 baseline_techniques.json 守门，数量只许涨不许跌。

status 说明：
  active   —— 有可调用实现（impl 字段）
  embedded —— 已并入核心引擎（无独立入口，引擎名见 note）
  planned  —— 待补（参数/口径需向需求方索要源码片段，不得自行假设）

类别基线（规格书 3.12）：特色 19 · 趋势 15（含 6 个经典策略信号）· 波段 8 · 区间 9。
"""
from . import datacenter, engines, multi_source, recveto

_REGISTRY = []


def register(tid, category, title, status="planned", impl=None, note=""):
    _REGISTRY.append({"id": tid, "category": category, "title": title,
                      "status": status, "impl": impl, "note": note})


# ---- 特色 19 ----
register("auction_gap", "特色", "竞价纪律", "active", engines.auction_discipline,
         "118万根K线回测口径；st=2 需 ≥5% 强高开")
register("ladderplan", "特色", "连板空间计划", "active", engines.ladderplan_plan)
register("fast_box", "特色", "快箱体节奏(#601-C)", "active", engines.classify_box_speed,
         "最深回踩用 min(c15[hi_idx:])，勿改")
register("ladder", "特色", "连板梯队", "planned", note="依赖 zt_pool 连板高度")
register("yaogu", "特色", "妖股基因", "planned")
register("lhb", "特色", "龙虎榜", "active", datacenter.lhb_scan,
         note="EM数据中心 RPT_DAILYBILLBOARD_DETAILSNEW，无网降级 None")
register("dz", "特色", "大宗交易", "active", datacenter.blocktrade_scan,
         note="折价≥5% 视为减持/出货信号；机构专用席位单独披露")
register("rongzi", "特色", "两融", "active", datacenter.margin_scan,
         note="RPTA_RZRQ_LSHJ 两融余额趋势，杠杆情绪佐证")
register("etfflow", "特色", "ETF主力资金流", "active", datacenter.etfflow_scan,
         note="风格判定第五维证据")
register("chips", "特色", "筹码集中度", "planned")
register("tail_steal", "特色", "尾盘偷袭", "planned")
register("theme_heat", "特色", "题材热度", "active", datacenter.theme_scan,
         note="注入式：涨停股需带 concepts/industry 字段，缺数据降级 None")
register("micro_struct", "特色", "微观结构", "planned")
register("style_rot", "特色", "风格轮动", "planned")
register("minefield", "特色", "雷区过滤", "planned")
register("veto", "特色", "败因否决器", "active", recveto.veto,
         note="标注式否决（V1 WARN/VETO），低开-0.1% 灾难区口径；480条回测实证")
register("xcheck", "特色", "三源交叉验证", "active", multi_source.cross_check,
         note="东财/新浪/腾讯中位数为权威价，价差>0.5%标存疑（M01）")
register("reopen", "特色", "断板反包", "planned")
register("dtd", "特色", "地天板", "planned")
register("long_leg", "特色", "大长腿", "planned")
register("subnew", "特色", "次新开板", "planned")

# ---- 趋势 15（含 6 个经典策略信号）----
register("uptrend_main", "趋势", "趋势主通道", "active", engines.screen_uptrend)
register("uptrend_slow", "趋势", "缓坡通道(慢牛)", "active", engines.screen_uptrend,
         note="MA20斜率≥1.5% + 20日涨幅≥8%")
register("slope20", "趋势", "MA20斜率带", "embedded",
         note="并入 screen_uptrend 评分与缓坡通道")
register("kronos", "趋势", "Kronos 结构健康度", "active", engines.kronos_lite)
register("macd_gold", "趋势", "经典信号·MACD金叉", "planned")
register("kdj_gold", "趋势", "经典信号·KDJ金叉", "planned")
register("boll_squeeze", "趋势", "经典信号·BOLL收口", "planned")
register("rsi_rebound", "趋势", "经典信号·RSI超卖回升", "planned")
register("obv_trend", "趋势", "经典信号·OBV能量潮", "planned")
register("vr_ratio", "趋势", "经典信号·VR容量比率", "planned")
register("breakout_pullback", "趋势", "突破回踩", "planned")
register("new_high", "趋势", "新高突破", "planned")
register("double_bottom", "趋势", "双底突破", "planned")
register("turtle20", "趋势", "海龟20日", "planned")
register("vol_price", "趋势", "量价齐升", "planned")

# ---- 波段 8 ----
register("pullback_relay", "波段", "涨停回马枪", "active", engines.screen_pullback_relay)
register("vol_dry", "波段", "缩量回踩", "planned")
register("wave2", "波段", "二波启动", "planned")
register("first_yin", "波段", "首阴反包", "planned")
register("low_first", "波段", "低位首板", "planned")
register("oversold", "波段", "超跌反弹", "planned")
register("zt_gene", "波段", "涨停基因", "planned")
register("wave_zone", "波段", "波段区间买点", "planned")

# ---- 区间 9 ----
register("box_bottom", "区间", "箱体波段", "active", engines.detect_stage_bottom)
register("n_wave", "区间", "N字二波", "active", engines.classify_box_speed,
         note="与快箱体共用三要素分类")
register("entry_zones", "区间", "近端买点阶梯", "active", engines.entry_plan)
register("triple_bottom", "区间", "三重底", "planned")
register("zone_edge", "区间", "区间下沿", "planned")
register("bottom_absorb", "区间", "贴底吸筹", "planned")
register("box_pre_break", "区间", "箱顶突破预备", "planned")
register("weekly_box", "区间", "周线箱体", "planned")
register("monthly_box", "区间", "月线箱体", "planned")
register("range_top", "区间", "区间上沿", "planned")

TECHNIQUES = _REGISTRY


def count():
    return len(TECHNIQUES)


def active():
    return [t for t in TECHNIQUES if t["status"] == "active"]


def baseline_guard(baseline):
    """基线守门：当前数量 < 基线 → 视为技巧被下线，拒绝。"""
    n = count()
    if n < baseline:
        raise AssertionError(
            f"技巧只增不减红线：注册表 {n} < 基线 {baseline}")
    return n
