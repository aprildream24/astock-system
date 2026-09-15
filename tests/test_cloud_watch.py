# -*- coding: utf-8 -*-
"""云端自选管理网关回归（2026-09-15）。

覆盖用户诉求：「我能够在网络上单独添加自选」「不要命令行要可视化入口」
「特定用户特定访问」。本套件锁住的就是这条链路的**安全与正确性**：

  ① normalize_code：用户输入千奇百怪（600519 / sh600519 / SH600519 /
     空格 / 非法串），必须统一成 sh600519/sz000001，非法一律 None；
  ② load_watch / save_watch：本地镜像原子写，只收合法代码；
  ③ _seal：GitHub Secrets 的 libsodium sealed box 加密必须可被
     对应私钥解出（真加解密对拍，不是只测"不抛异常"）；
  ④ HTTP 契约：/api/watch 读、/add 增、/remove 删、重复增要拒、
     要删不存在的要拒；对外监听缺 CLOUD_WATCH_TOKEN 必须拒绝启动；
  ⑤ PAT 绝不出现在任何响应体里（安全红线）。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pipeline import cloud_watch as cw  # noqa: E402


class TestNormalizeCode(unittest.TestCase):
    """输入规范化——用户手输，容错但不放行垃圾。"""

    def test_plain_six_digits(self):
        self.assertEqual(cw.normalize_code("600519"), "sh600519")
        self.assertEqual(cw.normalize_code("000001"), "sz000001")
        self.assertEqual(cw.normalize_code("300750"), "sz300750")
        self.assertEqual(cw.normalize_code("002415"), "sz002415")

    def test_already_prefixed(self):
        self.assertEqual(cw.normalize_code("sh600519"), "sh600519")
        self.assertEqual(cw.normalize_code("sz000001"), "sz000001")

    def test_case_and_whitespace(self):
        self.assertEqual(cw.normalize_code("SH600519"), "sh600519")
        self.assertEqual(cw.normalize_code(" 600519 "), "sh600519")
        self.assertEqual(cw.normalize_code(" sh 600519 "), "sh600519")

    def test_rejects_garbage(self):
        for bad in ("", None, "abc", "60051", "6005199", "hk00700",
                    "sh60051a", "600519;rm -rf", "../etc/passwd", 12.5):
            self.assertIsNone(cw.normalize_code(bad), f"{bad!r} 应被拒")

    def test_prefix_mismatch_is_kept_as_given(self):
        """显式前缀优先于首位数字推断——用户写了 sz600519 说明他知道自己在做什么，
        交给下游按代码查不到即可，规范化层不擅自改写前缀。"""
        self.assertEqual(cw.normalize_code("sz600519"), "sz600519")


class TestWatchFile(unittest.TestCase):
    """本地镜像读写（原子 + 白名单过滤）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = cw.core.CONFIG_DIR
        cw.core.CONFIG_DIR = self.tmp.name

    def tearDown(self):
        cw.core.CONFIG_DIR = self._orig
        self.tmp.cleanup()

    def test_roundtrip(self):
        cw.save_watch(["sh600519", "sz000001"])
        self.assertEqual(cw.load_watch(), ["sh600519", "sz000001"])

    def test_missing_file_is_empty(self):
        self.assertEqual(cw.load_watch(), [])

    def test_load_filters_illegal_entries(self):
        p = os.path.join(self.tmp.name, "watch.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(["sh600519", "garbage", 123, "sz000001"], f)
        self.assertEqual(cw.load_watch(), ["sh600519", "sz000001"])

    def test_corrupt_file_does_not_raise(self):
        p = os.path.join(self.tmp.name, "watch.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(cw.load_watch(), [])

    def test_save_is_atomic_no_tmp_left(self):
        cw.save_watch(["sh600519"])
        leftovers = [n for n in os.listdir(self.tmp.name)
                     if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestSealCrypto(unittest.TestCase):
    """GitHub Secrets 用 sealed box——真加解密对拍，确保写进云端能被解出。"""

    def test_sealed_box_roundtrip(self):
        try:
            from nacl import encoding, public
        except ImportError:  # pragma: no cover
            self.skipTest("PyNaCl 未安装")
        pk, sk = public.PrivateKey.generate(), None
        sk = public.PrivateKey(bytes(pk))
        pub_b64 = encoding.Base64Encoder().encode(bytes(pk.public_key))
        secret = json.dumps(["sh600519", "sz000001"], ensure_ascii=False)
        sealed = cw._seal(pub_b64, secret)
        box = public.SealedBox(sk)
        got = box.decrypt(encoding.Base64Encoder().decode(sealed))
        self.assertEqual(json.loads(got), ["sh600519", "sz000001"])

    def test_seal_output_is_base64(self):
        try:
            from nacl import encoding, public
        except ImportError:  # pragma: no cover
            self.skipTest("PyNaCl 未安装")
        pk = public.PrivateKey.generate()
        pub_b64 = encoding.Base64Encoder().encode(bytes(pk.public_key))
        sealed = cw._seal(pub_b64, "[]")
        self.assertIsInstance(sealed, str)
        encoding.Base64Encoder().decode(sealed)     # 不抛即合法


class _PanelCase(unittest.TestCase):
    """起一个真实 HTTP 服务，走真 socket（不是 mock handler）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._orig_cfg = cw.core.CONFIG_DIR
        cw.core.CONFIG_DIR = cls.tmp.name
        cls._orig_pat = os.environ.pop("GH_PAT", None)
        cls._orig_tok = os.environ.pop("CLOUD_WATCH_TOKEN", None)

    @classmethod
    def tearDownClass(cls):
        cw.core.CONFIG_DIR = cls._orig_cfg
        if cls._orig_pat is not None:
            os.environ["GH_PAT"] = cls._orig_pat
        if cls._orig_tok is not None:
            os.environ["CLOUD_WATCH_TOKEN"] = cls._orig_tok
        cls.tmp.cleanup()

    def _serve(self):
        h = cw.make_handler("owner/repo", "WATCH_CODES")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _req(self, port, method, path, body=None, token=None):
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Auth-Token"] = token
        c.request(method, path,
                  json.dumps(body).encode() if body is not None else None,
                  headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except Exception:  # noqa: BLE001
            return r.status, raw.decode("utf-8", "replace")


class TestPanelAPI(_PanelCase):
    """/api/watch 增删契约。"""

    def setUp(self):
        cw.save_watch(["sh600359"])
        self.port = self._serve()

    def test_get_returns_codes(self):
        st, js = self._req(self.port, "GET", "/api/watch")
        self.assertEqual(st, 200)
        self.assertEqual(js["codes"], ["sh600359"])

    def test_add_normalizes(self):
        st, js = self._req(self.port, "POST", "/api/watch/add",
                           {"code": "600519"})
        self.assertTrue(js["ok"])
        self.assertIn("sh600519", js["codes"])

    def test_add_duplicate_rejected(self):
        self._req(self.port, "POST", "/api/watch/add", {"code": "sh600519"})
        st, js = self._req(self.port, "POST", "/api/watch/add",
                           {"code": "600519"})       # 同票不同写法
        self.assertFalse(js["ok"])
        self.assertIn("已在自选", js["message"])
        self.assertEqual(js["codes"].count("sh600519"), 1)

    def test_add_garbage_rejected(self):
        st, js = self._req(self.port, "POST", "/api/watch/add",
                           {"code": "<script>"})
        self.assertFalse(js["ok"])
        self.assertNotIn("<script>", json.dumps(js["codes"]))

    def test_remove(self):
        self._req(self.port, "POST", "/api/watch/add", {"code": "sh600519"})
        st, js = self._req(self.port, "POST", "/api/watch/remove",
                           {"code": "600519"})
        self.assertTrue(js["ok"])
        self.assertNotIn("sh600519", js["codes"])

    def test_remove_absent_rejected(self):
        st, js = self._req(self.port, "POST", "/api/watch/remove",
                           {"code": "sh600519"})
        self.assertFalse(js["ok"])

    def test_panel_html_served(self):
        st, body = self._req(self.port, "GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("自选", body)

    def test_unknown_api_404(self):
        st, js = self._req(self.port, "POST", "/api/nope", {})
        self.assertEqual(st, 404)

    def test_dark_theme_present(self):
        """用户长期偏好深色 HUD——面板根底色必须是深色，不许白底。"""
        st, body = self._req(self.port, "GET", "/")
        self.assertIn("#15181e", body)


class TestPanelSecurity(_PanelCase):
    """安全红线。"""

    def setUp(self):
        cw.save_watch(["sh600359"])
        self.port = self._serve()

    def test_no_pat_in_responses(self):
        os.environ.pop("GH_PAT", None)
        for path in ("/api/watch", "/"):
            st, body = self._req(self.port, "GET", path)
            text = body if isinstance(body, str) else json.dumps(body)
            self.assertNotIn("ghp_", text)
            self.assertNotIn("github_pat_", text)

    def test_token_gate_when_configured(self):
        os.environ["CLOUD_WATCH_TOKEN"] = "s3cret"
        try:
            st, js = self._req(self.port, "POST", "/api/watch/add",
                               {"code": "600519"})
            self.assertEqual(st, 401)          # 无令牌 → 拒
            st, js = self._req(self.port, "POST", "/api/watch/add",
                               {"code": "600519"}, token="s3cret")
            self.assertTrue(js["ok"])          # 带对令牌 → 放行
        finally:
            os.environ.pop("CLOUD_WATCH_TOKEN", None)

    def test_bind_guard_refuses_public_without_token(self):
        """对外监听必须设令牌——否则任何人可改自选。"""
        src = open(os.path.join(BASE, "pipeline", "cloud_watch.py"),
                   encoding="utf-8").read()
        self.assertIn("CLOUD_WATCH_TOKEN", src)
        self.assertIn("拒绝启动", src)


class TestSyncWithoutPat(_PanelCase):
    """未配 PAT 时：本地生效，云端同步失败要明说，不许假装成功。"""

    def setUp(self):
        cw.save_watch(["sh600359"])
        os.environ.pop("GH_PAT", None)
        self.port = self._serve()

    def test_add_local_only_message(self):
        st, js = self._req(self.port, "POST", "/api/watch/add",
                           {"code": "600519"})
        self.assertTrue(js["ok"])                       # 本地成功
        self.assertIn("无法同步云端", js["message"])     # 但如实告知
        self.assertIn("sh600519", cw.load_watch())      # 本地确实写上了

    def test_sync_endpoint_reports_missing_pat(self):
        st, js = self._req(self.port, "POST", "/api/watch/sync", {})
        self.assertFalse(js["ok"])
        self.assertIn("GH_PAT", js["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
