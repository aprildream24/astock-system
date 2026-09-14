# -*- coding: utf-8 -*-
"""2026-09-13 第二轮整改回归：
① 推送的票必须在可购买区间 —— is_buyable_now 六重闸门（单一出口）
② 推送版面完善 —— 顶部速览条 / 买区位置条 / 昨日推荐复核 / 池别上标题
③ 全市场扫描口径修正 —— 退市/未上市/停牌不再算作「没扫到」
"""
import os
import re
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import build as bld, notifier, scoring  # noqa: E402
from pipeline.core import get_conn  # noqa: E402

TODAY = "2026-09-11"
DATES = [(date(2026, 9, 11) - timedelta(days=59 - i)).isoformat()
         for i in range(60)]
OLD_DATES = [(date(2015, 6, 30) - timedelta(days=59 - i)).isoformat()
             for i in range(60)]


def put_kline(con, d, code, c, o=None, h=None, l=None):
    o = c if o is None else o
    h = c * 1.01 if h is None else h
    l = c * 0.99 if l is None else l
    con.execute("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, d, o, h, l, c, 1e6, None, None, None))


def put_snap(con, code, name, amt, p=TODAY):
    con.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?)",
                (p, code, name, 10.0, 1.0, amt, 2.0, 30e8))


def flat(con, code, dates=None, base=10.0):
    """温和上行，保证能进趋势池并通过买区闸门。"""
    for i, d in enumerate(dates or DATES):
        put_kline(con, d, code, round(base * (1 + i * 0.004), 3))


def _cand(**kw):
    c = {"code": "sh600100", "name": "测试票", "pool": "趋势", "close": 10.1,
         "buy_low": 10.0, "buy_high": 10.3, "sell_low": 11.1,
         "sell_high": 11.6, "stop": 9.4, "action": "现在买"}
    c.update(kw)
    return c


# ---------------------------------------------------------------------------
# ① 可购买区间闸门
# ---------------------------------------------------------------------------

class TestBuyableGate(unittest.TestCase):
    def test_in_zone_is_buyable(self):
        """区内 + 四态能买 + 窄带 + 有盈利空间 → 可下单。"""
        self.assertTrue(scoring.is_buyable_now(_cand()))

    def test_above_zone_not_buyable(self):
        """现价高于买区上沿 → 不可照价下单（这是"推的票买不了"的根因场景）。"""
        self.assertFalse(scoring.is_buyable_now(_cand(close=10.9, dist=5.8)),
                         "现价跳出买区上沿，必须判不可买")
        self.assertFalse(scoring.is_buyable_now(_cand(close=9.5)),
                         "现价低于买区下沿（已破位区），必须判不可买")

    def test_action_not_now_is_not_buyable(self):
        """等回踩/小仓试/观望 都不是「当下可下单」。"""
        for act in ("等回踩", "小仓试", "观望", "禁买", "次日竞价达标买"):
            self.assertFalse(scoring.is_buyable_now(_cand(action=act)),
                             f"{act} 不得进可执行名单")

    def test_untradable_market(self):
        """科创板/北交所实盘买不了 → 一律不可买（推出去等于误导）。"""
        self.assertFalse(scoring.is_buyable_now(_cand(code="sh688001")),
                         "科创板未开通 → 不可买")
        self.assertTrue(scoring.is_buyable_now(_cand(code="sz300999")),
                        "创业板在准入内，必须保持可买")
        self.assertFalse(scoring.is_buyable_now(_cand(code="bj430047")),
                         "北交所未开通 → 不可买")

    def test_limit_up_not_buyable(self):
        """当日涨停=买不进 → 走次日竞价通道，不占主推位。"""
        self.assertFalse(scoring.is_buyable_now(_cand(limit_up=True)))

    def test_muted_or_broken_not_buyable(self):
        self.assertFalse(scoring.is_buyable_now(_cand(observe=True)))
        self.assertFalse(scoring.is_buyable_now(_cand(broken=True)))

    def test_wide_or_dead_zone_not_buyable(self):
        """伪区间（过宽/无盈利空间）即使现价落在里面也不可下单。"""
        self.assertFalse(scoring.is_buyable_now(
            _cand(buy_low=10.0, buy_high=18.0, close=12.0, sell_high=19.0)))
        self.assertFalse(scoring.is_buyable_now(
            _cand(buy_high=10.2, close=10.1, sell_low=10.1, sell_high=10.1)))

    def test_missing_price_is_not_buyable(self):
        self.assertFalse(scoring.is_buyable_now(_cand(close=None)))
        self.assertFalse(scoring.is_buyable_now(_cand(buy_low=None)))

    def test_wait_pullback_has_lower_bound(self):
        """等回踩带上界+下界（2026-09-14 用户口径：飞天上/已破位都不要）。
        略高于上沿 6% 内 → 等回踩；远超上沿 → 观望（飞在天上）；
        回踩到下沿附近（≥97%下沿）→ 等回踩；跌破更深 → 观望/禁买。"""
        base = _cand()   # 区间 10.0~10.3，close=10.1
        # 略高于上沿 → 等回踩（值得等）
        self.assertEqual(scoring._decide({**base, "close": 10.9}), "等回踩")
        # 远超上沿（飞天上）→ 观望，不再说"等回踩"误导
        self.assertEqual(scoring._decide({**base, "close": 12.0}), "观望")
        # 跌破下沿 3% 内（回踩下沿）→ 等回踩
        self.assertEqual(scoring._decide({**base, "close": 9.8}), "等回踩")
        # 跌破更深 → 触发止损 = 禁买；未触止损的深跌 = 观望
        self.assertEqual(scoring._decide({**base, "close": 9.2, "stop": 9.4}),
                         "禁买")
        self.assertEqual(scoring._decide({**base, "close": 9.6}), "观望")


# ---------------------------------------------------------------------------
# ③ 扫描覆盖口径：退市/未上市不计缺口
# ---------------------------------------------------------------------------

class TestUniverseClassification(unittest.TestCase):
    def _con(self):
        con = get_conn(":memory:")
        for d in DATES:
            put_kline(con, d, "sh000001", 3000)
        # 活跃票：60 根到 09-11，当日有成交额
        flat(con, "sh600100")
        put_snap(con, "sh600100", "活跃票", 5e8)
        # 退市票：成交额为 0，最后一根停在 2015 年
        for d in OLD_DATES:
            put_kline(con, d, "sh600400", 8.0)
        put_snap(con, "sh600400", "邯郸钢铁", 0)
        # 未上市：只有名字，无 K 线，无成交额
        put_snap(con, "sh600500", "某某新材", None)
        con.commit()
        return con

    def test_dead_codes_excluded_from_denominator(self):
        con = self._con()
        snap = bld._snapshot(con, TODAY)
        alive, dead = bld.split_universe(con, TODAY, snap)
        self.assertIn("sh600100", alive, "有成交的票必须在覆盖分母里")
        self.assertIn("sh600400", dead, "退市老代码不得算作未扫到")
        self.assertIn("sh600500", dead, "未上市新股不得算作未扫到")

    def test_coverage_is_100_when_only_alive(self):
        con = self._con()
        _cands, skipped = bld.scan_all(con, TODAY)
        cov = bld.LAST_SCAN_COVERAGE
        self.assertEqual(cov["universe"], 1)
        self.assertEqual(cov["untradable"], 2)
        self.assertEqual(cov["missing_bar"], 0)
        self.assertEqual(cov["coverage"], 100.0,
                         "有效标的全部有当日K线 ⇒ 覆盖率必须是 100%")

    def test_dead_codes_still_traced(self):
        """剥离出分母 ≠ 静默丢弃：必须留痕写明为什么不算。"""
        con = self._con()
        _cands, skipped = bld.scan_all(con, TODAY)
        why = {s["code"]: s["reason"] for s in skipped}
        for c in ("sh600400", "sh600500"):
            self.assertIn(c, why, f"{c} 必须留痕")
            self.assertIn("不可交易", why[c])


# ---------------------------------------------------------------------------
# ② 推送版面
# ---------------------------------------------------------------------------

def _dec(close=10.2, lo=10.0, hi=10.3, status="条件满足", pool="趋势",
         dist=0.0):
    return {"code": "sh600100", "name": "示例票", "status": status,
            "zone": [lo, hi], "stop": 9.4, "close": close, "dist_pct": dist,
            "sell_low": 11.1, "sell_high": 11.6, "invalid_if": "收盘跌破止损",
            "valid_until": "2026-09-18", "reason": "回踩MA5企稳",
            "research_grade": "B", "score": 72, "pool": pool,
            "position": "2成"}


class TestPushLayoutV2(unittest.TestCase):
    def _brief(self, **kw):
        return notifier.render_brief(
            TODAY, _dec(), [_dec(20.2, 20.0, 20.3, status="等待确认")],
            [{"code": "sh600000", "new": "超价取消", "reason": "超上限"}],
            {"reviewed": 120, "data_date": TODAY, "valid_until": "2026-09-18",
             "universe": 4592, "coverage": 100.0, "note": "预览。"}, **kw)

    def test_summary_strip(self):
        html = self._brief()
        for need in ("今日可下单", "待回踩", "次日竞价", "扫描覆盖"):
            self.assertIn(need, html, "顶部速览条必须有结论性数字")

    def test_zone_bar_rendered(self):
        html = self._brief()
        self.assertIn("▲", html, "必须有现价位置标记")
        self.assertIn('width="', html, "位置条用百分比宽度，不用定位")
        self.assertNotIn("float:", html)
        self.assertNotIn("position:", html)

    def test_pool_on_card_title(self):
        self.assertIn("首选观察 · 趋势", self._brief(),
                      "池别上标题：读者要知道策略来源")

    def test_position_hint_shown(self):
        self.assertIn("建议仓位", self._brief())

    def test_prev_review_block(self):
        html = self._brief(prev_review=[
            {"code": "sh600000", "name": "昨推票", "status": "🔴涨过头（+5.2%）"}])
        self.assertIn("昨日推荐 · 今日复核", html)
        self.assertIn("昨推票", html)
        self.assertIn("涨过头", html)

    def test_still_concise_after_upgrade(self):
        """版面加料不许失控：纯文本（去标签后）仍须在 3000 字符内。"""
        html = self._brief(pending=[_dec(11.0, 10.0, 10.3, dist=6.8,
                                         status="等待确认")])
        self.assertLess(len(re.sub(r"<[^>]+>", "", html)), 3000)

    def test_text_degrade_keeps_structure(self):
        txt = notifier.html_to_text(self._brief(prev_review=[
            {"code": "sh600000", "name": "昨推票", "status": "🟢还在跟"}]))
        lines = [l for l in txt.splitlines() if l.strip()]
        self.assertGreater(len(lines), 10, "降级文本必须保留分行结构")
        self.assertIn("今日可下单", txt)
        self.assertIn("昨日推荐 · 今日复核", txt)
        self.assertFalse(re.search(r"<[a-zA-Z/]", txt), "降级文本不得残留标签")

    def test_no_unbalanced_tags(self):
        html = self._brief(prev_review=[
            {"code": "sh600000", "name": "昨推票", "status": "🟢还在跟"}])
        self.assertEqual(html.count("<div"), html.count("</div>"))
        self.assertEqual(html.count("<table"), html.count("</table>"))
        self.assertEqual(html.count("<section"), html.count("</section>"))

    def test_empty_day_still_honest(self):
        html = notifier.render_brief(TODAY, None, [], [],
                                     {"reviewed": 0, "coverage": 100.0})
        self.assertIn("无当下可买入", html)
        self.assertIn("今日可下单", html)


# ---------------------------------------------------------------------------
# ④ 智谱免费模型接入（GLM 降级链）
# ---------------------------------------------------------------------------

class TestGlmProvider(unittest.TestCase):
    """GLM provider 必须默认走智谱免费档 glm-4.7-flash，可用 GLM_MODEL 覆盖。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("GLM_API_KEY", "GLM_MODEL",
                                 "CF_AI_TOKEN", "KIMI_API_KEY")}
        for k in ("GLM_API_KEY", "GLM_MODEL", "CF_AI_TOKEN", "KIMI_API_KEY"):
            os.environ.pop(k, None)
        # 密闭化：把 CONFIG_DIR 指到空目录，隔离本地 config/notify.json 的真 key
        from pipeline import core
        self._cfg_dir_saved = core.CONFIG_DIR
        self._tmp_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "cache", "_test_cfg")
        os.makedirs(self._tmp_cfg, exist_ok=True)
        # 每个用例从干净状态开始（用例可能写入 notify.json）
        leftover = os.path.join(self._tmp_cfg, "notify.json")
        if os.path.exists(leftover):
            os.remove(leftover)
        core.CONFIG_DIR = self._tmp_cfg

    def tearDown(self):
        from pipeline import core
        core.CONFIG_DIR = self._cfg_dir_saved
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set_cfg(self, **kv):
        import json
        p = os.path.join(self._tmp_cfg, "notify.json")
        cfg = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
        cfg.update(kv)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def _providers(self):
        from pipeline import narrative
        return {p["name"]: p for p in narrative._providers()}

    def test_default_model_is_free_flash(self):
        os.environ["GLM_API_KEY"] = "test-key"
        p = self._providers()["glm"]
        self.assertEqual(p["model"], "glm-4.7-flash",
                         "默认必须用智谱免费档（glm-4.5-flash 已下线，"
                         "glm-4.6 是付费模型，都不许当默认）")
        self.assertTrue(p["enabled"])
        self.assertIn("open.bigmodel.cn", p["url"])

    def test_model_env_override(self):
        os.environ["GLM_API_KEY"] = "test-key"
        os.environ["GLM_MODEL"] = "glm-4.6"
        self.assertEqual(self._providers()["glm"]["model"], "glm-4.6")

    def test_config_file_fallback(self):
        """无环境变量时读 config/notify.json（本地计划任务场景）。"""
        self.assertFalse(self._providers()["glm"]["enabled"],
                         "env 与配置都没有 key → 必须禁用")
        self._set_cfg(glm_api_key="cfg-key", glm_model="glm-4.6")
        p = self._providers()["glm"]
        self.assertTrue(p["enabled"])
        self.assertEqual(p["model"], "glm-4.6")
        self.assertEqual(p["headers"]["Authorization"], "Bearer cfg-key")

    def test_glm_disabled_without_key(self):
        self.assertFalse(self._providers()["glm"]["enabled"])

    def test_provider_order_cf_glm_kimi(self):
        from pipeline import narrative
        names = [p["name"] for p in narrative._providers()]
        self.assertEqual(names, ["cf", "glm", "kimi"])

    def test_glm_request_payload(self):
        """真发请求时的载荷与鉴权头格式（标准 OpenAI 兼容响应可被解析）。"""
        from pipeline import narrative

        def ok_post():
            return {"choices": [{"message": {"content": " ok "}}]}

        p = self._providers()["glm"]
        self.assertEqual(p["model"], "glm-4.7-flash")
        self.assertTrue(p["headers"]["Authorization"].startswith("Bearer "))
        out = narrative._call(p, "prompt", 0.8, http_fn=ok_post)
        self.assertEqual(out, "ok")


# ---------------------------------------------------------------------------
# ④ CI 推送配置（2026-09-14）：无本地 notify.json 时 Secrets 必须真发
#    修复前的两个 CI 哑火点：
#    a) push_dry_run 默认 True → CI 上账本写了、消息永远不出
#    b) primary_channel 默认 wxpusher → 没配 WxPusher 时 PushPlus 分支不进
# ---------------------------------------------------------------------------

class TestCIConfig(unittest.TestCase):
    """模拟 CI：无本地 notify.json + env 注入 Secrets。"""

    def setUp(self):
        import tempfile
        from pipeline import core
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_config_dir = core.CONFIG_DIR
        self._orig_env = {k: os.environ.get(k)
                          for k in ("PUSHPLUS_TOKEN", "SERVERCHAN_KEY",
                                    "WXPUSHER_CONF")}
        core.CONFIG_DIR = self._tmp.name          # 指向空目录 = 无 notify.json
        for k in self._orig_env:
            os.environ.pop(k, None)

    def tearDown(self):
        from pipeline import core
        core.CONFIG_DIR = self._orig_config_dir
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _cfg(self):
        from pipeline import core
        return core.load_config()

    def test_ci_with_pushplus_secret_sends_for_real(self):
        """CI + PUSHPLUS_TOKEN Secret ⇒ dry-run 解除、主通道 pushplus。"""
        os.environ["PUSHPLUS_TOKEN"] = "PP_TOKEN_CI"
        cfg = self._cfg()
        self.assertFalse(cfg["push_dry_run"],
                         "CI 配了 Secret 还停在 dry-run = 哑火")
        self.assertEqual(cfg["primary_channel"], "pushplus")

    def test_ci_with_serverchan_secret_sends(self):
        os.environ["SERVERCHAN_KEY"] = "SC_KEY_CI"
        cfg = self._cfg()
        self.assertFalse(cfg["push_dry_run"])
        self.assertEqual(cfg["primary_channel"], "serverchan")

    def test_ci_with_wxpusher_conf(self):
        os.environ["WXPUSHER_CONF"] = (
            '[{"name":"主号","app_token":"AT_x","uids":["UID_x"]}]')
        cfg = self._cfg()
        self.assertFalse(cfg["push_dry_run"])
        self.assertEqual(len(cfg["wxpusher_accounts"]), 1)
        self.assertEqual(cfg["primary_channel"], "wxpusher")

    def test_ci_zero_secrets_stays_dry_run(self):
        """零密钥场景（本地开发）保持 dry-run，不误发。"""
        cfg = self._cfg()
        self.assertTrue(cfg["push_dry_run"])

    def test_dry_run_string_normalized(self):
        """notify.json 写 \"false\"（字符串）不得被当成真值 dry-run。"""
        import json
        from pipeline import core
        with open(os.path.join(self._tmp.name, "notify.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"push_dry_run": "false",
                       "pushplus_token": "PP_X"}, f)
        cfg = self._cfg()
        self.assertFalse(cfg["push_dry_run"])

    def test_local_explicit_dry_run_respected(self):
        """本地显式 dry-run（含密钥也不发）不被 CI 分支误解除。"""
        import json
        from pipeline import core
        with open(os.path.join(self._tmp.name, "notify.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"push_dry_run": True,
                       "pushplus_token": "PP_X"}, f)
        cfg = self._cfg()
        self.assertTrue(cfg["push_dry_run"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
