# -*- coding: utf-8 -*-
"""分用户分级权限（2026-09-15 新增）的回归锁。

需求：用户自己增删自选/持仓，且「自选 / 观察 / 购入」各自授权给特定人。
本套件锁死：
  A. users.json 双形态兼容（旧扁平 → 新角色化），角色→可见分组映射正确；
  B. 未授权分组在**密文层**被剥离（不是前端隐藏）；
  C. 持仓成本/浮盈仅 buy/all 可见；
  D. 错口令必失败（HMAC 完整性）；
  E. users.json 站点索引不得含口令；
  F. 管理服务写入校验（坏代码拒收、去重、必须保留 all 管理员、
     口令留空保留原值、写路径白名单）。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import publish, users as U  # noqa: E402


def _sample_data():
    return {"date": "2026-09-15",
            "candidates": [{"code": "sh600000", "name": "A", "cost": 10.0,
                            "float_pnl": 5.2, "score": 88}],
            "watch_advice": [{"code": "sh600359", "advice": "持有"}],
            "holdings_detail": [{"code": "sh600088", "buy_price": 20.0,
                                 "pnl_pct": 3.1}],
            "meta": {}, "emotion": {}, "skipped": [], "signals": []}


class TestUserModel(unittest.TestCase):
    def test_legacy_flat_form_compat(self):
        us = U.parse_users({"owner": "p" * 10, "guest": "g" * 10})
        by = U.users_by_id(us)
        self.assertTrue(by["owner"].is_owner)
        self.assertEqual(by["guest"].roles, ["observe"])

    def test_legacy_skips_comment_keys(self):
        us = U.parse_users({"_note": "x", "owner": "p" * 10})
        self.assertEqual([u.uid for u in us], ["owner"])

    def test_new_form_roles(self):
        us = U.parse_users({"users": [
            {"id": "a", "pass": "x" * 10, "roles": ["watch", "buy"]}]})
        u = U.users_by_id(us)["a"]
        self.assertEqual(u.visible_groups(), ["watch", "buy"])
        self.assertTrue(u.can("watch"))
        self.assertFalse(u.can("observe"))

    def test_all_role_sees_everything(self):
        u = U.User("o", "p" * 10, ["all"])
        self.assertTrue(all(u.can(g) for g in ("watch", "observe", "buy")))
        self.assertTrue(u.is_owner)

    def test_unknown_role_dropped(self):
        u = U.User("x", "p" * 10, ["watch", "hacker"])
        self.assertEqual(u.roles, ["watch"])


class TestRoleScopedEncryption(unittest.TestCase):
    def setUp(self):
        self.users = [
            U.User("owner", "owner-pass-2026", ["all"], name="管理员"),
            U.User("zhang", "zhang-pass-2026", ["watch", "buy"], name="张三"),
            U.User("li", "li-pass-2026", ["observe"], name="李四"),
        ]

    def _decrypt_for(self, uid, pwd, data=None):
        blobs = dict(publish.encrypt_data(data or _sample_data(),
                                          U.passwords_of(self.users),
                                          users=self.users))
        return json.loads(publish.decrypt_bytes(blobs[uid], pwd).decode())

    def test_watch_buy_user_cannot_see_observe_group(self):
        d = self._decrypt_for("zhang", "zhang-pass-2026")
        self.assertNotIn("candidates", d, "未授权观察池必须从密文剥离")
        self.assertIn("watch_advice", d)
        self.assertIn("holdings_detail", d)

    def test_observe_user_cannot_see_watch_or_holdings(self):
        d = self._decrypt_for("li", "li-pass-2026")
        self.assertIn("candidates", d)
        self.assertNotIn("watch_advice", d)
        self.assertNotIn("holdings_detail", d)

    def test_cost_only_visible_to_buy_role(self):
        d = self._decrypt_for("li", "li-pass-2026")
        self.assertFalse(any("cost" in c for c in d.get("candidates", [])),
                         "观察角色不得看到持仓成本")

    def test_owner_sees_everything(self):
        d = self._decrypt_for("owner", "owner-pass-2026")
        for k in ("candidates", "watch_advice", "holdings_detail"):
            self.assertIn(k, d)

    def test_wrong_password_always_fails(self):
        blobs = dict(publish.encrypt_data(_sample_data(),
                                          U.passwords_of(self.users),
                                          users=self.users))
        for uid, blob in blobs.items():
            with self.assertRaises(ValueError, msg=f"{uid} 被错口令解开"):
                publish.decrypt_bytes(blob, "definitely-wrong-password")

    def test_access_block_present(self):
        d = self._decrypt_for("zhang", "zhang-pass-2026")
        self.assertEqual(d["_access"]["uid"], "zhang")
        self.assertEqual(d["_access"]["groups"], ["watch", "buy"])
        self.assertFalse(d["_access"]["is_owner"])


class TestSiteIndexNoPassword(unittest.TestCase):
    def test_users_json_index_has_no_password(self):
        users = [U.User("owner", "owner-secret-pw", ["all"]),
                 U.User("zhang", "zhang-secret-pw", ["watch"])]
        tmp = tempfile.mkdtemp()
        o = (publish.DIST_DIR, publish.SITE_DIR)
        try:
            publish.DIST_DIR = tmp
            publish.SITE_DIR = os.path.join(tmp, "site")
            publish.build_site(_sample_data(), U.passwords_of(users),
                               users=users)
            p = os.path.join(publish.SITE_DIR, "users.json")
            self.assertTrue(os.path.exists(p))
            txt = open(p, encoding="utf-8").read()
            self.assertNotIn("owner-secret-pw", txt, "索引泄漏口令")
            self.assertNotIn("zhang-secret-pw", txt)
            idx = json.loads(txt)
            self.assertEqual(len(idx["users"]), 2)
        finally:
            publish.DIST_DIR, publish.SITE_DIR = o
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestAdminWriteGuards(unittest.TestCase):
    """管理服务写入校验（离线，直接调函数）。"""

    def setUp(self):
        from pipeline import admin
        self.admin = admin
        self.tmp = tempfile.mkdtemp()
        self._orig = admin.CONFIG
        admin.CONFIG = self.tmp

    def tearDown(self):
        self.admin.CONFIG = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bad_code_rejected(self):
        r = self.admin.save_watch(["sh600519", "600000"])
        self.assertFalse(r["ok"])
        self.assertIn("600000", r["error"])

    def test_dedup_and_persist(self):
        r = self.admin.save_watch(["sh600519", "sh600519", "sz000858"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 2)
        self.assertEqual(self.admin.read_json("watch.json", []),
                         ["sh600519", "sz000858"])

    def test_must_keep_an_all_admin(self):
        r = self.admin.save_users({"users": [
            {"id": "zhang", "pass": "zhang-pass-1", "roles": ["watch"]}]})
        self.assertFalse(r["ok"])
        self.assertIn("all", r["error"])

    def test_blank_pass_keeps_existing(self):
        self.admin.save_users({"users": [
            {"id": "owner", "pass": "owner-pass-ok", "roles": ["all"]},
            {"id": "zhang", "pass": "zhang-pass-ok", "roles": ["watch"]}]})
        r = self.admin.save_users({"users": [
            {"id": "owner", "pass": "", "roles": ["all"]},
            {"id": "zhang", "pass": "", "roles": ["watch", "buy"]}]})
        self.assertTrue(r["ok"])
        saved = json.loads(open(os.path.join(self.tmp, "users.json"),
                                encoding="utf-8").read())
        by = {u["id"]: u for u in saved["users"]}
        self.assertEqual(by["zhang"]["pass"], "zhang-pass-ok",
                         "口令留空必须保留原值")
        self.assertEqual(by["zhang"]["roles"], ["watch", "buy"])

    def test_short_password_rejected(self):
        r = self.admin.save_users({"users": [
            {"id": "owner", "pass": "short", "roles": ["all"]}]})
        self.assertFalse(r["ok"])

    def test_path_whitelist(self):
        with self.assertRaises(ValueError):
            self.admin._path("../../etc/passwd")
        with self.assertRaises(ValueError):
            self.admin._path("notify.json")

    def test_users_for_ui_never_returns_password(self):
        self.admin.save_users({"users": [
            {"id": "owner", "name": "O", "pass": "owner-pass-ok",
             "roles": ["all"]}]})
        ui = self.admin._users_for_ui()
        for u in ui:
            self.assertNotIn("pass", u, "UI 接口不得回传口令")
            self.assertIn("has_pass", u)


class TestFrontendAuthFix(unittest.TestCase):
    """auth.js 不得硬编码 owner.bin（否则非 owner 用户永远打不开）。"""

    def test_no_hardcoded_owner_bin(self):
        p = os.path.join(ROOT, "site_template", "auth.js")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        # 排除注释行后，不得出现 fetch("data/owner.bin")
        code = "\n".join(l for l in src.split("\n")
                         if not l.strip().startswith("//"))
        self.assertNotIn('"data/owner.bin"', code,
                         "auth.js 仍硬编码 owner.bin：非 owner 用户打不开")
        self.assertIn("loadIndex", code, "缺少用户索引读取")
        self.assertIn("for (const u of idx)", code, "缺少遍历试解逻辑")


if __name__ == "__main__":
    unittest.main(verbosity=2)
