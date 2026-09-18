# -*- coding: utf-8 -*-
"""板块热度 + 行情放开限量的回归锁（2026-09-18 新增，用户需求）。

锁两组不可回退的口径：

A. 板块标注必须真的生效（修两处**静默失效**）
   `scoring.compute_top_picks` 的 `sector_temp` 冷热因子与「同板块去重」在此
   之前**没有数据源**：全仓库没有任何一处写入 `sector_temp`（恒 None，加成
   从未生效），`sector_of` 也退化成"按池别去重"（不同行业的波段票互斥）。
   本套件用真 schema + 内存库锁住"标注能打上、因子能吃上"。

B. 行情好放开限量，行情一般**不许变**
   用户口径：行情好时不再限制 3 只，针对评分高的个股全部推荐。
   反向红线同样重要——数据未达标（覆盖不足）时**绝不放开**，
   否则等于用不可信的情绪分放大推荐量。
"""
import importlib
import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ⚠️ 必须自带 sys.path 引导：回归按顺序跑多个套件，前面的 e2e 套件会改 cwd，
# 没有它会出现「单独跑全绿、全量回归一片 ERROR」。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pipeline.core as core          # noqa: E402
import pipeline.notifier as notifier  # noqa: E402
import pipeline.scoring as scoring    # noqa: E402
import pipeline.sector as sector      # noqa: E402

DATE = "2026-09-18"


def _mkcon():
    con = sqlite3.connect(":memory:")
    con.executescript(core._SCHEMA)     # 用真 schema：顺带验证建表真的存在
    return con


def _seed(con, date=DATE):
    con.executemany("INSERT OR REPLACE INTO sector_heat VALUES(?,?,?,?,?,?)", [
        (date, "半导体", 4.20, 12.5, 80, 10),
        (date, "银行", 0.30, 1.2, 20, 5),
        (date, "房地产", -2.50, -8.0, 3, 60),
    ])
    con.executemany("INSERT OR REPLACE INTO stock_industry VALUES(?,?,?)", [
        ("600001", "半导体", date),
        ("600002", "银行", date),
        ("600003", "房地产", date),
        ("600004", "不存在于板块榜", date),
    ])
    con.commit()


def _cand(code, pool="趋势", score=80.0, **kw):
    c = {"code": code, "name": "票" + code[-2:], "pool": pool,
         "close": 10.0, "score": score, "action": "现在买",
         "buy_low": 9.8, "buy_high": 10.2, "stop": 9.5,
         "sell_low": 11.0, "sell_high": 12.0}
    c.update(kw)
    return c


class TestSectorHeat(unittest.TestCase):
    """A. 板块热度计算与标注。"""

    def test_schema_has_new_tables(self):
        con = _mkcon()
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("sector_heat", names, "sector_heat 表缺失（板块热度无处落库）")
        self.assertIn("stock_industry", names, "stock_industry 表缺失")

    def test_temp_of_levels(self):
        self.assertEqual(sector.temp_of(4.2), "🔥强")
        self.assertEqual(sector.temp_of(sector.HOT_PCT), "🔥强", "边界值应算强")
        self.assertEqual(sector.temp_of(-2.5), "❄弱")
        self.assertEqual(sector.temp_of(sector.COLD_PCT), "❄弱", "边界值应算弱")
        self.assertEqual(sector.temp_of(0.5), "·平")
        self.assertEqual(sector.temp_of(None), "", "无数据必须返回空串而非占位符")

    def test_rank_board_sorted_desc(self):
        con = _mkcon()
        _seed(con)
        board = sector.load_board(con)
        ranked = sector.rank_board(board, n=3)
        self.assertEqual([r["sector"] for r in ranked],
                         ["半导体", "银行", "房地产"], "必须按涨幅降序")
        self.assertEqual(ranked[0]["temp"], "🔥强")
        self.assertEqual(ranked[0]["net_yi"], 12.5)

    def test_annotate_marks_candidates(self):
        """核心锁：标注必须真的落到候选上（曾因无数据源静默失效）。"""
        con = _mkcon()
        _seed(con)
        cands = [_cand("sh600001"), _cand("sz600002", pool="波段"),
                 _cand("sh600003", pool="区间"), _cand("sh600004")]
        board, hot = sector.annotate(con, DATE, cands)
        self.assertEqual(cands[0]["sector"], "半导体")
        self.assertEqual(cands[0]["sector_temp"], "🔥强")
        self.assertEqual(cands[1]["sector_temp"], "·平")
        self.assertEqual(cands[2]["sector_temp"], "❄弱")
        self.assertNotIn("sector", cands[3],
                         "板块榜里没有的行业不得硬标（宁缺勿假）")
        self.assertTrue(hot and hot[0]["sector"] == "半导体")

    def test_annotate_graceful_without_data(self):
        """空库（无板块/无行业）→ 静默降级，绝不抛异常。"""
        con = _mkcon()
        cands = [_cand("sh600001")]
        board, hot = sector.annotate(con, DATE, cands)
        self.assertEqual(board, {})
        self.assertEqual(hot, [])
        self.assertNotIn("sector", cands[0])

    def test_empty_fetch_never_overwrites(self):
        """抓取失败返回空 → 落库函数必须不写，否则会把昨日有效热度抹掉。"""
        con = _mkcon()
        _seed(con)
        self.assertEqual(sector.save_board(con, "2026-09-19", {}), 0)
        self.assertEqual(sector.save_industry(con, {}), 0)
        # 昨日数据仍在，且 load_board 不带日期取的是最新有效一天
        self.assertEqual(len(sector.load_board(con)), 3)
        self.assertEqual(len(sector.load_board(con, DATE)), 3)

    def test_sector_tag_format(self):
        self.assertEqual(
            sector.sector_tag({"sector": "半导体", "sector_pct": 4.2,
                               "sector_temp": "🔥强"}), "半导体+4.2%🔥")
        self.assertEqual(sector.sector_tag({"sector": "银行"}), "银行")
        self.assertEqual(sector.sector_tag({}), "", "无数据必须空串")


class TestSectorFactorsActuallyWork(unittest.TestCase):
    """锁：板块冷热因子与同板块去重**真的生效**（修静默失效）。"""

    def test_strong_sector_gets_bonus(self):
        env_w = {"连板": 1.0, "趋势": 1.0, "波段": 1.0, "区间": 1.0}
        hot = _cand("sh600001", sector_temp="🔥强")
        cold = _cand("sh600002", sector_temp="❄弱")
        scoring.compute_top_picks([hot, cold], env_w, {}, limit=None)
        self.assertGreater(hot["eff_score"], cold["eff_score"],
                           "🔥强板块的 eff_score 必须高于 ❄弱板块")

    def test_same_sector_limit_respected(self):
        """同板块限额：per_sector=1 时同板块只活 1 只（原语义），
        行情好放宽到 2 后同板块可活 2 只。"""
        env_w = {"连板": 1.0, "趋势": 1.0, "波段": 1.0, "区间": 1.0}
        cands = [_cand(f"sh60000{i}", sector="半导体", score=90 - i)
                 for i in range(4)]
        one = scoring.compute_top_picks(cands, env_w, {},
                                        sector_of=lambda c: c.get("sector"),
                                        limit=None, per_sector=1)
        self.assertEqual(len(one), 1, "per_sector=1 时同板块只能留最高分 1 只")
        two = scoring.compute_top_picks(cands, env_w, {},
                                        sector_of=lambda c: c.get("sector"),
                                        limit=None, per_sector=2)
        self.assertEqual(len(two), 2)

    def test_real_sector_beats_pool_fallback(self):
        """真实行业不同的票不得被当成同一板块互斥（旧实现的隐性减配）。"""
        env_w = {"连板": 1.0, "趋势": 1.0, "波段": 1.0, "区间": 1.0}
        cands = [_cand("sh600001", pool="波段", sector="半导体", score=90),
                 _cand("sh600002", pool="波段", sector="银行", score=88)]
        picks = scoring.compute_top_picks(cands, env_w, {},
                                         sector_of=lambda c: c.get("sector") or c["pool"],
                                         limit=None, per_sector=1)
        self.assertEqual(len(picks), 2,
                         "两个不同行业的波段票必须都能入选（旧实现只活 1 只）")


class TestMarketHeatQuota(unittest.TestCase):
    """B. 行情档位 → 推荐配额。"""

    def _emo(self, score, qualified=True):
        return {"score": score, "label": "x", "qualified": qualified}

    def test_normal_keeps_top3(self):
        level, limit, per_sec, cap = scoring.market_heat(self._emo(50))
        self.assertEqual(limit, 3, "行情一般必须维持 TOP3 纪律")
        self.assertEqual((per_sec, cap), (1, 2))

    def test_hot_releases_limit(self):
        for score in (60, 75, 76, 90):
            _, limit, per_sec, cap = scoring.market_heat(self._emo(score))
            self.assertIsNone(limit, f"情绪{score}（行情好）必须放开限量")
            self.assertGreaterEqual(per_sec, 2, "放开限量时同板块限额要同步放宽")
            self.assertGreaterEqual(cap, 3, "放开限量时连板席位要同步放宽")

    def test_unqualified_never_releases(self):
        """覆盖不达标 ⇒ 情绪分不可信 ⇒ 绝不放开（反向红线）。"""
        for score in (60, 80, 95):
            _, limit, _, _ = scoring.market_heat(self._emo(score, qualified=False))
            self.assertEqual(limit, 3, "数据未达标时绝不放量")

    def test_missing_emo_safe(self):
        for emo in (None, {}, {"score": None}):
            _, limit, per_sec, _ = scoring.market_heat(emo)
            self.assertEqual((limit, per_sec), (3, 1))

    def test_default_call_unchanged(self):
        """老调用方（不传新参数）行为必须逐字不变——默认值即原语义。"""
        env_w = {"连板": 1.0, "趋势": 1.0, "波段": 1.0, "区间": 1.0}
        cands = [_cand(f"sh60000{i}", sector=f"行业{i}", score=90 - i)
                 for i in range(6)]
        picks = scoring.compute_top_picks(cands, env_w, {})
        self.assertEqual(len(picks), 3, "默认仍是 TOP3")

    def test_hot_releases_to_all(self):
        """行情好：6 只全部符合条件 → 全推（不再截断到 3）。"""
        env_w = {"连板": 1.0, "趋势": 1.0, "波段": 1.0, "区间": 1.0}
        cands = [_cand(f"sh60000{i}", sector=f"行业{i}", score=90 - i)
                 for i in range(6)]
        _, limit, per_sec, cap = scoring.market_heat(self._emo(72))
        picks = scoring.compute_top_picks(cands, env_w, {}, limit=limit,
                                          per_sector=per_sec, ladder_cap=cap)
        self.assertEqual(len(picks), 6, "行情好时必须全部推荐")


class TestBriefRendersSector(unittest.TestCase):
    """推送版面必须真的把板块热度显示出来。"""

    def _card(self):
        return {"code": "sh600001", "name": "票A", "action": "等回踩",
                "zone": [9.8, 10.2], "stop": 9.5, "score": 70.0,
                "sector": "半导体", "sector_pct": 4.2,
                "sector_temp": "🔥强", "sector_net_yi": 12.5,
                "valid_until": "下一交易日", "pool": "趋势"}

    def test_brief_has_hot_sector_block(self):
        meta = {"coverage": 99.0, "universe": 5000, "heat_level": "偏热",
                "hot_sectors": [{"sector": "半导体", "pct": 4.2,
                                 "net_yi": 12.5, "temp": "🔥强"}]}
        html = notifier.render_brief(DATE, None, [], [], meta,
                                     pending=[self._card()])
        self.assertIn("今日板块热度", html)
        self.assertIn("半导体", html)
        self.assertIn("偏热", html)

    def test_card_shows_sector_row(self):
        html = notifier.render_card(self._card(), head="【待回踩】")
        self.assertIn("板块热度", html)
        self.assertIn("半导体", html)
        self.assertIn("+4.20%", html)

    def test_cand_line_contains_sector(self):
        line = notifier._cand_line(_cand("sh600001", sector="半导体",
                                         sector_pct=4.2, sector_temp="🔥强"))
        self.assertIn("半导体", line)
        self.assertLessEqual(len(line), notifier.CAND_LINE_CAP, "候选行不得超长")

    def test_no_sector_degrades_cleanly(self):
        """无板块数据时必须干净降级：不出现 None / 空标签。"""
        line = notifier._cand_line(_cand("sh600001"))
        self.assertNotIn("None", line)
        html = notifier.render_card(self._card() | {"sector": None})
        self.assertNotIn("板块热度", html)

    def test_many_picks_stay_readable(self):
        """放开限量后 10 只全推：前 2 只出完整卡，其余走紧凑行（可读性红线）。"""
        meta = {"coverage": 99.0, "universe": 5000, "heat_level": "亢奋"}
        cards = [dict(self._card(), code=f"sh60000{i}", name=f"票{i}")
                 for i in range(10)]
        html = notifier.render_brief(DATE, None, cards, [], meta)
        for i in range(10):
            self.assertIn(f"票{i}", html, "10 只必须全部出现在推送里")
        self.assertIn("其余可下单标的", html, "超出 2 只后应走紧凑行分组")

    def test_brief_never_exceeds_pushplus_cap(self):
        """压力锁：80 只标的时正文不得超过 PushPlus 上限。

        超上限会被 `content[:PP_HTML_CAP]` **硬截断**（切在半张卡中间），
        比少显示几只更糟——所以排版层必须自己设线并明确告知"其余见详情"。
        """
        meta = {"coverage": 99.0, "universe": 5000, "heat_level": "亢奋"}
        cards = [{"code": f"sh6000{i:02d}", "name": f"票{i}号",
                  "action": "现在买", "zone": [9.8, 10.2], "stop": 9.5,
                  "score": 70.0, "sector": "半导体", "sector_pct": 4.2,
                  "sector_temp": "🔥强", "dist_pct": 0.3,
                  "valid_until": "下一交易日", "pool": "趋势"}
                 for i in range(80)]
        html = notifier.render_brief(DATE, None, cards, [], meta,
                                     pending=cards, ladder_next=cards)
        self.assertLessEqual(len(html), notifier.PP_HTML_CAP,
                             "推送正文超过 PushPlus 上限会被硬截断")
        self.assertIn("见网页版完整详情", html, "被省略的标的必须明确告知读者")


if __name__ == "__main__":
    unittest.main(verbosity=2)
