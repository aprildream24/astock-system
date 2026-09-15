# -*- coding: utf-8 -*-
"""CI 环境健壮性回归（2026-09-15 血案）。

## 血案现场

run 34985391295：CI 上「回归自检」失败 → 后面 7 个步骤全 skipped →
**全天零推送**。用户看到的就是「本地说没问题，实盘什么都没发生」。

三个根因（全是「本地绿、CI 挂」）：
  ① tests/test_e2e_safety.py 断言 config/notify.json 必须存在
     —— 但它是隐私文件，CI 上必然不存在（走 Secrets 注入）
  ② 新测试 import nacl / yaml —— runner 预装列表里没有这两个包
  ③ run_regression.py 把「环境缺包导致 skip」误判成「回归倒退」exit 2
  ④ 计数用 out.count("ok") 数子串 → 基线 303 是虚高假数字（实为 291）

本文件把这些约束固化成断言，防止任何人再把 CI 改挂。
"""
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "stock.yml")
RR = os.path.join(ROOT, "tests", "run_regression.py")
BASELINE = os.path.join(ROOT, "tests", "baseline.json")

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class TestWorkflowInstallsDeps(unittest.TestCase):
    """CI 必须显式安装 runner 未预装的依赖，否则回归自检必挂。"""

    @classmethod
    def setUpClass(cls):
        with open(WF, encoding="utf-8") as f:
            cls.text = f.read()

    def test_pynacl_installed_in_ci(self):
        self.assertIn("pynacl", self.text.lower(),
                      "CI 未安装 PyNaCl —— sync_watch 写 Secret 会 "
                      "ModuleNotFoundError（实测）")

    def test_pyyaml_installed_in_ci(self):
        self.assertIn("pyyaml", self.text.lower(),
                      "CI 未安装 PyYAML —— test_sync_watch 的 YAML 断言会 "
                      "ERROR（实测 no pyyaml）")

    def test_install_step_precedes_regression(self):
        """安装步骤必须在回归自检之前，否则装了也没用。"""
        if not HAS_YAML:
            self.skipTest("no pyyaml")
        import yaml
        d = yaml.safe_load(self.text)
        names = [s.get("name", "") for s in d["jobs"]["build"]["steps"]]
        try:
            i_inst = next(i for i, n in enumerate(names) if "依赖" in n)
            i_reg = names.index("回归自检")
        except (StopIteration, ValueError) as e:
            self.fail(f"缺少安装/回归步骤：{e}")
        self.assertLess(i_inst, i_reg,
                        "依赖安装步骤在回归自检之后 —— 顺序错了")


class TestRegressionCounterIsSound(unittest.TestCase):
    """计数器的三个坑必须已被修掉，否则 PASS 虚低会误杀全天推送。"""

    @classmethod
    def setUpClass(cls):
        with open(RR, encoding="utf-8") as f:
            cls.src = f.read()
        # 用 importlib 真加载（exec 会缺 __file__）
        import importlib.util
        spec = importlib.util.spec_from_file_location("_rr_probe", RR)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.counter = staticmethod(mod._count)
        cls.ran_total = staticmethod(mod._ran_total)

    def test_no_substring_counting(self):
        """不许再用 out.count("ok") —— 会把 traceback 里的 ok 也数进去。

        只扫真代码：剥掉 docstring 与行注释（历史叙述里会提到这个写法）。
        """
        import ast
        tree = ast.parse(self.src)
        # 收集所有字符串字面量的位置（docstring 也是字面量，一并排除）
        str_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    str_lines.add(ln)
        bad = []
        for i, line in enumerate(self.src.splitlines(), 1):
            code = line.split("#")[0]
            if i in str_lines:
                continue
            if 'count("ok")' in code or "count('ok')" in code:
                bad.append(i)
        self.assertEqual(bad, [], f"仍在用子串计数 ok（行 {bad}）")

    def test_counts_ok_before_skipped(self):
        """ok 必须排在 skipped 之前判，否则 skipped 里的 ok 被误计。"""
        p, f, s = self.counter("test_a (m.T.test_a) ... skipped '原因'")
        self.assertEqual((p, f, s), (0, 0, 1))

    def test_real_output_shape_cross_line_ok(self):
        """真实 -v 形态：用例头一行，docstring + 结果一行 —— 必须能计到。

        实测 2026-09-15：早期实现只看同行结果词，导致这种形态大面积漏计
        （一次漏 82 条）→ PASS 虚低 → 误判倒退 → 全天零推送。
        """
        p, f, s = self.counter(
            "test_a (m.T.test_a)\n说明文字。 ... ok\n"
            "test_b (m.T.test_b) ... ok\n")
        self.assertEqual(p, 2, f"跨行结果漏计：PASS={p}")

    def test_docstring_with_ellipsis_before_result(self):
        """docstring 自身含 ` ... ` 时，必须切**最后**一个才能取到结果词。"""
        p, f, s = self.counter(
            "test_a (m.T.test_a)\n讲 ... 这个坑。 ... ok\n")
        self.assertEqual((p, f, s), (1, 0, 0),
                         "按第一个 ... 切会把说明文字当结果词 → 漏计")

    def test_cross_line_fail_and_error(self):
        """跨行形态的 FAIL / ERROR 同样要识别。"""
        p, f, s = self.counter("test_a (m.T.test_a)\n说明 ... ERROR\n")
        self.assertEqual((p, f), (0, 1))

    def test_counts_ok_when_warning_interleaves(self):
        """ResourceWarning 插在 ... 与 ok 之间时，该用例仍须被计到。"""
        out = ("test_x (m.T.test_x) ... /path/unittest/case.py:1: "
               "ResourceWarning: unclosed file\n  self.doCleanups()\n"
               "ResourceWarning: Enable tracemalloc\nok\n")
        p, f, s = self.counter(out)
        self.assertEqual(p, 1, f"警告夹插时漏计：PASS={p}")

    def test_does_not_swallow_next_case(self):
        """向后找结果词不得越到下一条用例（否则会吞掉/错配）。"""
        out = ("test_a (m.T.test_a) ... noise\n"
               "test_b (m.T.test_b) ... ok\n")
        p, f, s = self.counter(out)
        self.assertEqual(p, 1, "跨用例误计")

    def test_prose_only_line_not_a_case(self):
        """非用例头行（无括号路径）一律不计。"""
        p, f, s = self.counter("test_plain ... ok\nprose ... ok\n")
        self.assertEqual((p, f, s), (0, 0, 0), "普通行被当成了用例")

    def test_ignores_prose_containing_dots(self):
        """普通输出行含 " ... " 时不得被当成用例行、更不得吞掉下一条。

        这是实测踩到的坑：某个测试的 docstring 独立成行、且里面就带 `... `
        字样，会被当作用例头 → 上一条用例的结果词查找被掐断 → PASS 少一 →
        有效数 < 基线 → exit 2 → 后面步骤全 skipped → 全天零推送。
        输入严格复刻真实的 `-v` 输出形态（用例名一行，docstring 一行）。
        期望：test_a 通过「向后找」补上 ok，docstring 行被忽略，test_b 正常计。
        """
        out = ("test_a (m.T.test_a)\n"
               "结果词插在 ... 与 ok 之间时也要算到。 ... ok\n"   # 误报源
               "test_b (m.T.test_b) ... ok\n")
        p, f, s = self.counter(out)
        self.assertEqual(p, 2, f"docstring 含点点点导致漏计：PASS={p}")
        self.assertEqual((f, s), (0, 0), "误报行被当成了结果")

    def test_never_overcounts(self):
        """凡是自造输出，解析数不得超过 Ran N（防虚高回归）。"""
        out = ("test_a (m.T.test_a) ... ok\n"
               "test_b (m.T.test_b) ... ok\n"
               "警告路径里的 ok 不算数 /unittest/case.py\n"
               "Ran 2 tests in 0.1s\n")
        p, f, s = self.counter(out)
        self.assertEqual(p + f + s, self.ran_total(out),
                         f"解析总数 {p + f + s} 超出 Ran 2")

    def test_fail_and_error_recognized(self):
        """FAIL / ERROR 必须识别（用带括号路径的真实用例头形态）。"""
        p, f, s = self.counter("test_a (m.T.test_a) ... FAIL\n"
                               "test_b (m.T.test_b) ... ERROR\n")
        self.assertEqual((p, f), (0, 2))

    def test_ran_total_parsed(self):
        self.assertEqual(self.ran_total("Ran 26 tests in 7.6s"), 26)
        self.assertIsNone(self.ran_total("nothing here"))

    def test_resource_warning_suppressed_in_subprocess(self):
        """跑子进程时必须 -W ignore::ResourceWarning，去掉噪声源。"""
        self.assertIn("ignore::ResourceWarning", self.src)

    def test_unresolved_is_hard_fail_not_silent_skip(self):
        """解析不出的用例必须硬失败，不得混进 SKIP 洗白。

        早期实现把未解析数加进 skipped → 「有效数 = PASS + SKIP」被虚高
        → 真正的解析丢失被掩盖，放过了一次错误的绿色。现在必须：
          · 未解析单独计 UNRESOLVED，不并入 SKIP；
          · 有未解析就 exit 1。
        """
        self.assertIn("UNRESOLVED", self.src, "未解析项没有独立计数")
        self.assertIn("total_unresolved", self.src, "未解析项没有独立累加")
        # 未解析必须触发失败退出
        self.assertRegex(self.src,
                         r"if total_unresolved:\s*\n\s*print\([^)]*\)\s*\n\s*"
                         r"sys\.exit\(1\)",
                         "未解析项没有触发 exit 1")


class TestBaselineIsReal(unittest.TestCase):
    """基线必须来自 TestLoader 权威口径，不能是文本计数的虚高值。"""

    @classmethod
    def setUpClass(cls):
        with open(BASELINE, encoding="utf-8") as f:
            cls.base = json.load(f)

    def test_baseline_not_inflated(self):
        """303 是历史上的虚高值（子串计数产物），不许回到那个数。"""
        self.assertNotEqual(self.base["total_pass"], 303,
                            "基线 303 是子串计数的虚高产物")
        self.assertGreater(self.base["total_pass"], 200,
                           "基线异常低，可能被环境降级固化了")

    def test_baseline_matches_testloader_count(self):
        """基线必须落在真实用例数的合理区间内。

        注意：本套件自己也在 SUITES 里，加/删用例会让总数变动。
        所以判据用「相等 ± 本套件用例数」的宽容区间，避免鸡生蛋。
        真正防回归的是 run_regression.py 里的逐套件比对。
        """
        import importlib
        import unittest as ut
        with open(RR, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"SUITES\s*=\s*\[(.*?)\]", src, re.S)
        self.assertIsNotNone(m, "找不到 SUITES 列表")
        suites = re.findall(r'"([^"]+\.py)"', m.group(1))
        self.assertGreater(len(suites), 15, "套件数异常少")

        real = 0
        for s in suites:
            mod = s[:-3]
            try:
                modobj = importlib.import_module(mod)
            except Exception as e:  # noqa: BLE001
                self.fail(f"导入 {mod} 失败：{e}")
            real += ut.TestLoader().loadTestsFromModule(modobj).countTestCases()

        self_ = ut.TestLoader().loadTestsFromModule(
            sys.modules[__name__]).countTestCases()
        self.assertLessEqual(
            abs(real - self.base["total_pass"]), self_,
            f"基线 {self.base['total_pass']} 与真实用例数 {real} 偏差超过"
            f"本套件规模 {self_}——基线需重建")


class TestNoEnvBrittleAssertions(unittest.TestCase):
    """隐私文件断言必须环境感知，否则 CI 必挂。"""

    def test_e2e_safety_notify_assertion_is_ci_aware(self):
        p = os.path.join(ROOT, "tests", "test_e2e_safety.py")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        i = src.find("def test_real_notify_present")
        self.assertGreater(i, 0, "找不到 test_real_notify_present")
        body = src[i:i + 2000]
        self.assertTrue(
            "GITHUB_ACTIONS" in body or "CI" in body,
            "test_real_notify_present 未做 CI 环境判断 —— CI 上必然失败")
        # CI 分支必须是「只告警、不 fail」：把 CI 判定写严了会连坐整条
        # 推送链路（测试挂 → 后面步骤全 skipped → 全天零推送）。
        ci_branch = body.split("if in_ci:", 1)[-1].split("self.assertTrue(os.path.exists(p)", 1)[0]
        self.assertNotIn("assertTrue", ci_branch,
                         "CI 分支里还有硬断言 —— 缺凭据时会把流水线拉黑")
        self.assertIn("return", ci_branch, "CI 分支没有提前返回，会走到本地断言")


class TestLedgerSyncWired(unittest.TestCase):
    """推送账本必须能回到仓库，否则 daily_check 永远误报零推送。

    注意：`gh_sync.py` 是**本地部署工具，按设计不入库**（它处理 PAT 与
    整仓推送，属运维脚本）。因此断言必须分两层：
      · 本地（有该文件）→ 校验 ALLOW_DIST 白名单逻辑；
      · CI（无该文件）→ 跳过该条，改为校验 workflow 里的回写步骤存在。
    2026-09-15 血案：直接在 CI 上 open("gh_sync.py") → FileNotFoundError
    → 回归自检 FAIL → 全天零推送。
    """

    @classmethod
    def setUpClass(cls):
        with open(WF, encoding="utf-8") as f:
            cls.text = f.read()
        cls.gh_sync = os.path.join(ROOT, "gh_sync.py")

    def test_ledger_sync_step_exists(self):
        self.assertIn("push_ledger_sync", self.text,
                      "CI 未回写账本 —— daily_check 的远端账本永远 404")

    @unittest.skipUnless(os.path.exists(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "gh_sync.py")),
        "gh_sync.py 是本地部署工具（按设计不入库），CI 上不存在")
    def test_gh_sync_allows_ledger_but_not_whole_dist(self):
        with open(self.gh_sync, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("ALLOW_DIST", src)
        self.assertIn("dist/push_ledger.json", src)
        # dist/ 整体仍须排除（有 reports/quarantine 等不该外传的东西）
        self.assertIn('"dist/"', src)


if __name__ == "__main__":
    unittest.main()
