# -*- coding: utf-8 -*-
"""P8 补全回归：技巧基线守门 / 涨停池情绪 / AI 降级链 / 调度文件零密钥。"""
import glob
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import mood, narrative, scoring, techniques  # noqa: E402
from pipeline.core import get_conn  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mk_con_with_klines():
    """三天递推：09-09 全部 10 元基期 → 09-10 三只涨停/一只炸板 → 09-11 晋级。"""
    con = get_conn(":memory:")

    def put(d, code, o, h, l, c):
        # klines 列序: code,date,o,h,l,c,v,amt,pct,turn
        con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (code, d, o, h, l, c, 1e6, None, None, None))

    sh = lambda n: f"sh{n}" if n.startswith("6") else f"sz{n}"  # noqa: E731
    for n in ("600001", "600002", "000003", "600004", "000005"):
        put("2026-09-09", sh(n), 9.95, 10.05, 9.95, 10.0)
    # 上证指数行：trade_calendar 的权威日历来源（生产由 fetch_daily 写入）
    for d in ("2026-09-09", "2026-09-10", "2026-09-11"):
        put(d, "sh000001", 3000, 3010, 2995, 3005)
    # 09-10：三只涨停（c=11.0），600004 触板未封（炸板），000005 平稳
    for n in ("600001", "600002", "000003"):
        put("2026-09-10", sh(n), 9.95, 11.05, 9.95, 11.0)
    put("2026-09-10", "sh600004", 10.0, 10.99, 9.98, 10.4)
    put("2026-09-10", "sz000005", 10.0, 10.15, 9.98, 10.05)
    # 09-11：三只 2 连板，600004 再炸板
    for n in ("600001", "600002", "000003"):
        put("2026-09-11", sh(n), 11.0, 12.15, 11.0, 12.1)
    put("2026-09-11", "sh600004", 10.5, 11.45, 10.45, 11.0)
    put("2026-09-11", "sz000005", 10.05, 10.2, 10.0, 10.1)
    con.commit()
    return con


class TestTechniques(unittest.TestCase):
    def test_baseline_no_shrink(self):
        with open(os.path.join(BASE, "tools", "baseline_techniques.json"),
                  encoding="utf-8") as f:
            base = __import__("json").load(f)["count"]
        n = techniques.baseline_guard(base)
        self.assertEqual(n, base, "注册表数量不得低于基线")

    def test_registry_shape(self):
        ids = [t["id"] for t in techniques.TECHNIQUES]
        self.assertEqual(len(ids), len(set(ids)), "技巧 id 不得重复")
        for t in techniques.TECHNIQUES:
            self.assertIn(t["status"], ("active", "embedded", "planned"))
            if t["status"] == "active":
                self.assertTrue(callable(t["impl"]),
                                f"active 技巧 {t['id']} 必须有实现")
        cats = {t["category"] for t in techniques.TECHNIQUES}
        self.assertEqual(cats, {"特色", "趋势", "波段", "区间"})


class TestMood(unittest.TestCase):
    def test_promote_zhaban_emotion(self):
        con = _mk_con_with_klines()
        m1 = mood.compute_mood(con, "2026-09-10")
        self.assertEqual(m1["zt_count"], 3, "09-10 三只涨停")
        self.assertEqual(m1["max_streak"], 1)
        m = mood.compute_mood(con, "2026-09-11")
        self.assertIsNotNone(m)
        self.assertEqual(m["zt_count"], 3, "今日 3 只晋级涨停")
        self.assertEqual(m["max_streak"], 2, "昨日涨停今日再板 = 2连板")
        self.assertAlmostEqual(m["promote_rate"], 1.0, places=2, msg="3/3 晋级")
        self.assertAlmostEqual(m["zhaban_rate"], 0.25, places=2)
        self.assertTrue(0 <= m["emotion"] <= 100)
        # zt_pool 落库可递推
        rows = con.execute("SELECT code, streak FROM zt_pool WHERE date='2026-09-11'").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(s == 2 for _, s in rows))
        con.close()

    def test_env_bias_neutral_when_empty(self):
        con = get_conn(":memory:")
        self.assertIsNone(mood.compute_mood(con, "2099-01-01"))
        con.close()


class TestNarrative(unittest.TestCase):
    def test_rule_engine_fallback(self):
        stats = {"date": "2026-09-12",
                 "mood": {"zt_count": 60, "max_streak": 5,
                          "promote_rate": 0.6, "zhaban_rate": 0.45,
                          "emotion": 62},
                 "picks": [{"code": "600000", "name": "示例", "pool": "连板"}]}
        text = narrative.rule_engine(stats)
        self.assertIn("62分", text)
        self.assertIn("示例600000", text)
        # 空仓场景
        text2 = narrative.rule_engine({"date": "d", "mood": {}, "picks": []})
        self.assertIn("空仓也是仓位", text2)

    def test_chain_quota_switch(self):
        """配额 429 → 立即换家（不重试同一 provider）→ 无可用家时规则兜底。"""
        import io
        import urllib.error
        calls = []

        def fake_post():
            calls.append(1)
            raise urllib.error.HTTPError("u", 429, "quota exceeded", None,
                                         io.BytesIO(b'{"error":"quota exceeded"}'))

        fake = {"name": "cf", "url": "http://x", "headers": {}, "temp": 0.8,
                "rpm": 0, "enabled": True}
        orig = narrative._providers
        narrative._providers = lambda: [fake]
        try:
            stats = {"date": "d", "mood": {"zt_count": 0}, "picks": []}
            text = narrative.narrate(stats, http_fn=fake_post)
        finally:
            narrative._providers = orig
        self.assertTrue(text)
        self.assertEqual(len(calls), 1, "配额 429 不应重试同一 provider")

    def test_chain_400_temperature_retry(self):
        """400 invalid temperature → 降 temp 重发成功。"""
        import io
        import urllib.error
        attempts = {"n": 0}

        def post_400_then_ok():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.HTTPError(
                    "u", 400, '{"error":"invalid temperature"}', None,
                    io.BytesIO(b'{"error":"invalid temperature"}'))
            return {"result": "ok-narrative"}

        fake = {"name": "cf", "url": "http://x", "headers": {}, "temp": 0.8,
                "rpm": 0, "enabled": True}
        out = narrative._call(fake, "测试", 0.8, http_fn=post_400_then_ok)
        self.assertEqual(out, "ok-narrative")
        self.assertEqual(attempts["n"], 2, "400 temperature 应降档重试一次")


class TestWorkflowsZeroKeys(unittest.TestCase):
    def test_no_hardcoded_keys(self):
        secret_pat = re.compile(r"(SCT[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{8,}|"
                                r"[A-Za-z0-9]{32,})")
        offenders = []
        for pat in ("*.yml", "*.yaml", "*.py"):
            for f in glob.glob(os.path.join(BASE, "**", pat), recursive=True):
                if "site" + os.sep in f or "dist" + os.sep in f:
                    continue
                with open(f, encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
                if secret_pat.search(txt) and "${{ secrets." not in txt:
                    offenders.append(f)
        self.assertEqual(offenders, [], f"发现疑似明文密钥: {offenders}")

    def test_workflow_crons_present(self):
        with open(os.path.join(BASE, ".github", "workflows", "stock.yml"),
                  encoding="utf-8") as f:
            y = f.read()
        self.assertEqual(y.count("- cron:"), 9, "主链 8+1 时点")
        self.assertIn("secrets.SERVERCHAN_KEY", y)
        with open(os.path.join(BASE, ".github", "workflows", "executor.yml"),
                  encoding="utf-8") as f:
            e = f.read()
        self.assertEqual(e.count("- cron:"), 16, "模拟盘 16 时点")


if __name__ == "__main__":
    unittest.main(verbosity=1)
