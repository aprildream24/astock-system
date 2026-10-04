# -*- coding: utf-8 -*-
"""并列排名总序决胜回归锁（2026-10-05）。

用户原话：「到底是云瑶健康还是吉鑫科技，现在又是两个带头相互第一」。

根因（CI 日志 + 源码取证）：
  1. compute_top_picks 排序键 (eff_score, action) 并列时，Python 稳定排序
     保留**输入序**——而连板池候选来自无 ORDER BY 的 zt_pool 查询（rowid
     序随每晚重建漂移），同分票名次在两次构建间互换；
  2. build 展示层用**原始分**重排（排名却用**有效分**），两把尺子并存；
  3. 标签读取（_extra_of / _enrich ROW_NUMBER）同分时 SQLite 随手抓一行。

修正：全部排序键补齐总序决胜——有效分 → 可执行动作 → 确认次数 →
代码升序（绝对决胜）。本套件锁住：
  T1 同分同动作：输入序翻转，输出序不变（代码升序）；
  T2 同分：双确认优先于首推（确认次数决胜）；
  T3 高有效分优先于高原始分（排名/展示同一把尺）。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pipeline.scoring as scoring     # noqa: E402

ENV_W = {"连板": 1.0, "趋势": 1.0, "区间": 1.0, "波段": 1.0}
WINRATES = {}


def _cand(code, name, score=70.0, action="现在买", confirms=None):
    c = {"code": code, "name": name, "pool": "波段", "close": 20.0,
         "worth": score, "action": action,
         "buy_low": 19.5, "buy_high": 20.5, "score": score}
    if confirms is not None:
        c["confirms"] = confirms
    return c


class TestRankTiebreak(unittest.TestCase):
    """同分票的名次必须由固定决胜键决定，与输入序无关。"""

    def test_同分同动作_输入序翻转输出不变(self):
        a = _cand("sz301115", "云瑶健康")       # 代码序在前
        b = _cand("sh603949", "吉鑫科技")
        out1 = scoring.compute_top_picks([a, b], ENV_W, WINRATES,
                                         limit=None, per_sector=0,
                                         ladder_cap=99)
        out2 = scoring.compute_top_picks([b, a], ENV_W, WINRATES,
                                         limit=None, per_sector=0,
                                         ladder_cap=99)
        order1 = [c["code"] for c in out1]
        order2 = [c["code"] for c in out2]
        self.assertEqual(order1, order2, "输入序翻转后输出序必须一致")
        self.assertEqual(order1, ["sh603949", "sz301115"],
                         "同分决胜 = 代码升序（sh < sz）")

    def test_同分_双确认优先于首推(self):
        first = _cand("sh600000", "首推票", confirms=1)      # 代码序在前
        double = _cand("sz000001", "双确认票", confirms=2)
        out1 = scoring.compute_top_picks([first, double], ENV_W, WINRATES,
                                         limit=None, per_sector=0,
                                         ladder_cap=99)
        out2 = scoring.compute_top_picks([double, first], ENV_W, WINRATES,
                                         limit=None, per_sector=0,
                                         ladder_cap=99)
        self.assertEqual([c["code"] for c in out1],
                         ["sz000001", "sh600000"], "双确认票排首推票前面")
        self.assertEqual(out1, out2, "输入序翻转输出不变")

    def test_确认次数字段缺失_退化为代码序不报错(self):
        a = _cand("sz300001", "甲")
        b = _cand("sz000002", "乙")
        out = scoring.compute_top_picks([a, b], ENV_W, WINRATES,
                                        limit=None, per_sector=0,
                                        ladder_cap=99)
        self.assertEqual([c["code"] for c in out],
                         ["sz000002", "sz300001"])

    def test_同分_现在买动作优先于等回踩(self):
        # 排名层语义：有效分优先，动作优先级只在**同分**时决胜；
        # 「可下单永远排前」由 build 展示层负责（另一把锁）。
        now = _cand("sz000001", "现价可买", score=70, action="现在买")
        wait = _cand("sz000002", "等回踩同分", score=70, action="等回踩")
        out1 = scoring.compute_top_picks([wait, now], ENV_W, WINRATES,
                                         limit=None, per_sector=0,
                                         ladder_cap=99)
        out2 = scoring.compute_top_picks([now, wait], ENV_W, WINRATES,
                                         limit=None, per_sector=0,
                                         ladder_cap=99)
        self.assertEqual(out1[0]["code"], "sz000001",
                         "同分时现在买（可执行动作）决胜")
        self.assertEqual(out1, out2, "输入序翻转输出不变")


class TestDeterministicSources(unittest.TestCase):
    """读取/展示层排序的源码级锁定（与 test_executor_auto 锁 YAML 同风格）。"""

    def _src(self, *paths):
        base = os.path.join(ROOT, "pipeline")
        return "\n".join(
            open(os.path.join(base, p), encoding="utf-8").read()
            for p in paths)

    def test_build_zt_pool查询有序(self):
        src = self._src("build.py")
        self.assertIn("FROM zt_pool WHERE date=? \"\n        \"ORDER BY code",
                      src.replace("ORDER BY code, streak", "ORDER BY code"),
                      "zt_pool 读取必须 ORDER BY code（rowid 序会漂移）")

    def test_intraday计划读取显式排序(self):
        src = self._src("intraday.py")
        self.assertIn("FROM rec_picks WHERE date=? ORDER BY score DESC, code",
                      src, "盘中计划读取必须显式排序，不得依赖写入序")

    def test_build展示排序用有效分而非原始分(self):
        src = self._src("build.py")
        self.assertIn('-(c.get("eff_score") or c.get("score") or 0)', src,
                      "展示排序必须与终审排名同尺（有效分）")


if __name__ == "__main__":
    unittest.main()
