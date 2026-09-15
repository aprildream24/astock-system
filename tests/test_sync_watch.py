# -*- coding: utf-8 -*-
"""pipeline/sync_watch.py 测试（2026-09-15）。

覆盖：代码规范化、输入解析（逗号/空格/JSON/去重/上限）、sealed box 加密
可被 GitHub 侧解开（用 PyNaCl 反向验证）、缺 PAT 时的行为、workflow YAML
里 watch-sync 的接线正确性。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import sync_watch as sw  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "stock.yml")

# 2026-09-15 教训：CI runner 预装列表里**没有** PyNaCl 和 PyYAML。
# 若测试直接 import 它们，CI 上会 ERROR（不是 skip）→ 回归自检挂 →
# 后面所有构建推送步骤 skipped → 全天零推送（实测 run 85391295）。
# 所以一律用 skipUnless 探测，本地全跑、CI 自动降级。
try:
    import nacl  # noqa: F401
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

try:
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class TestNormalize(unittest.TestCase):
    def test_plain_6_digit_sh(self):
        self.assertEqual(sw.normalize_code("600519"), "sh600519")

    def test_plain_6_digit_sz(self):
        self.assertEqual(sw.normalize_code("000001"), "sz000001")
        self.assertEqual(sw.normalize_code("300750"), "sz300750")

    def test_prefixed(self):
        self.assertEqual(sw.normalize_code("sh600519"), "sh600519")
        self.assertEqual(sw.normalize_code("SH600519"), "sh600519")
        self.assertEqual(sw.normalize_code(" sz000001 "), "sz000001")

    def test_invalid(self):
        for bad in ("", None, "60051", "6005199", "abc", "hk00700", "600519x"):
            self.assertIsNone(sw.normalize_code(bad), bad)

    def test_5_digit_rejected(self):
        self.assertIsNone(sw.normalize_code("60051"))


class TestParseCodes(unittest.TestCase):
    def test_comma(self):
        self.assertEqual(sw.parse_codes("600519,000001"),
                         ["sh600519", "sz000001"])

    def test_space_and_semicolon(self):
        self.assertEqual(sw.parse_codes("600519 000001;300750"),
                         ["sh600519", "sz000001", "sz300750"])

    def test_json_array(self):
        self.assertEqual(sw.parse_codes('["sh600519","sz000001"]'),
                         ["sh600519", "sz000001"])

    def test_dedup_keeps_order(self):
        self.assertEqual(sw.parse_codes("600519,600519,000001"),
                         ["sh600519", "sz000001"])

    def test_garbage_ignored(self):
        self.assertEqual(sw.parse_codes("600519,xxx,,000001"),
                         ["sh600519", "sz000001"])

    def test_empty(self):
        self.assertEqual(sw.parse_codes(""), [])
        self.assertEqual(sw.parse_codes(None), [])

    def test_cap(self):
        raw = ",".join(str(600000 + i) for i in range(300))
        self.assertEqual(len(sw.parse_codes(raw)), sw.MAX_CODES)


@unittest.skipUnless(HAS_NACL, "PyNaCl 未安装（CI runner 预装列表无此包）")
class TestSealCrypto(unittest.TestCase):
    """GitHub 用 libsodium sealed box 收 Secret。这里用 PyNaCl 反向解开，
    证明我们发的密文 GitHub 侧真能读——不是只「格式看着对」。"""

    def test_roundtrip_with_pynacl(self):
        from nacl import encoding, public
        pk = public.PrivateKey.generate()
        pub_b64 = pk.public_key.encode(encoding.Base64Encoder()).decode()
        payload = json.dumps(["sh600519", "sz000001"], ensure_ascii=False)

        sealed_b64 = sw.seal(pub_b64, payload)

        box = public.SealedBox(pk)
        got = box.decrypt(encoding.Base64Encoder().decode(sealed_b64)).decode()
        self.assertEqual(json.loads(got), ["sh600519", "sz000001"])

    def test_seal_accepts_bytes_key(self):
        from nacl import encoding, public
        pk = public.PrivateKey.generate()
        pub_b64 = pk.public_key.encode(encoding.Base64Encoder())
        out = sw.seal(pub_b64, "[]")
        self.assertIsInstance(out, str)
        self.assertTrue(out)


class TestMainGuards(unittest.TestCase):
    def _isolate_env(self):
        saved = {}
        for k in ("GH_PAT", "GITHUB_PAT", "SYNC_PAT", "WATCH_CODES_IN"):
            saved[k] = os.environ.pop(k, None)
        return saved

    def _restore(self, saved):
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_codes_returns_2(self):
        saved = self._isolate_env()
        try:
            rc = sw.main(["--codes", "xxx,yyy"])
            self.assertEqual(rc, 2)
        finally:
            self._restore(saved)

    def test_no_pat_returns_3(self):
        saved = self._isolate_env()
        try:
            rc = sw.main(["--codes", "600519"])
            self.assertEqual(rc, 3)
        finally:
            self._restore(saved)

    def test_dry_run_needs_no_pat(self):
        saved = self._isolate_env()
        try:
            rc = sw.main(["--codes", "600519", "--dry-run"])
            self.assertEqual(rc, 0)
        finally:
            self._restore(saved)

    def test_reads_env_when_no_codes_arg(self):
        saved = self._isolate_env()
        try:
            os.environ["WATCH_CODES_IN"] = "600519,000001"
            rc = sw.main(["--dry-run"])
            self.assertEqual(rc, 0)
        finally:
            self._restore(saved)


class TestWorkflowWiring(unittest.TestCase):
    """watch-sync 必须真的接进 CI——步骤/input/Secret 三处都在才算通。"""

    @classmethod
    def setUpClass(cls):
        with open(WF, encoding="utf-8") as f:
            cls.text = f.read()
        if HAS_YAML:
            import yaml
            cls.doc = yaml.safe_load(cls.text)
        else:
            cls.doc = None

    def test_yaml_parses(self):
        if self.doc is None:
            self.skipTest("no pyyaml（CI 无此包，文本级断言仍生效）")
        self.assertIn("jobs", self.doc)

    def test_codes_input_declared(self):
        self.assertIn("codes", self.text)
        self.assertIn("watch-sync", self.text)

    def test_sync_step_invokes_module(self):
        self.assertIn("python -m pipeline.sync_watch", self.text)

    def test_sync_step_carries_codes_env(self):
        self.assertIn("WATCH_CODES_IN", self.text)

    def test_pat_used_for_secret_write(self):
        # 写 Secrets 必须 PAT；GITHUB_TOKEN 没这权限
        self.assertIn("GH_PAT: ${{ secrets.GH_PAT }}", self.text)

    def test_deploy_skips_watch_sync(self):
        if self.doc is None:
            self.skipTest("no pyyaml")
        cond = str(self.doc["jobs"]["deploy-pages"]["if"])
        self.assertIn("watch-sync", cond)

    def test_all_heavy_steps_skip_watch_sync(self):
        if self.doc is None:
            self.skipTest("no pyyaml")
        for s in self.doc["jobs"]["build"]["steps"]:
            name = s.get("name", "")
            if name in ("回归自检", "构建+推送（WxPusher/PushPlus 多渠道）",
                        "构建加密站点", "上传 Pages 构件"):
                self.assertIn("watch-sync", str(s.get("if", "")),
                              f"{name} 未排除 watch-sync")


class TestPanelAndTemplate(unittest.TestCase):
    """前端模板必须走 workflow_dispatch，且不再依赖 libsodium。"""

    @classmethod
    def setUpClass(cls):
        cls.app = open(os.path.join(ROOT, "site_template", "app.js"),
                       encoding="utf-8").read()
        cls.html = open(os.path.join(ROOT, "site_template", "index.html"),
                        encoding="utf-8").read()

    def test_no_libsodium_script(self):
        self.assertNotIn("libsodium.js", self.html)

    def test_no_sodium_usage(self):
        # 注释里提到 v1 的历史做法是允许的（记录为何废弃）；这里只禁真实调用
        self.assertNotIn("window.sodium", self.app)
        for line in self.app.splitlines():
            code = line.split("//")[0]
            self.assertNotIn("crypto_box_seal", code,
                             f"仍在代码中调用客户端加密：{line.strip()}")
            self.assertNotIn("sodium.", code,
                             f"仍在调用 sodium API：{line.strip()}")

    def test_uses_dispatch(self):
        self.assertIn("/dispatches", self.app)
        self.assertIn("watch-sync", self.app)

    def test_dispatch_posts_codes_input(self):
        self.assertIn("codes:", self.app)
        self.assertIn("task: \"watch-sync\"", self.app)

    def test_orphan_b64_helpers_removed(self):
        # v1 遗留的 base64 工具在 v2 已无用途
        self.assertNotIn("_b64ToU8", self.app)
        self.assertNotIn("_sealSecret", self.app)


if __name__ == "__main__":
    unittest.main()
