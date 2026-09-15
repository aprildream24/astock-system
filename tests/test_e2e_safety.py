# -*- coding: utf-8 -*-
"""守护：E2E 演练**绝不能**污染本机 config（2026-09-15 血案）。

背景（真实事故，不是假想）：
    `tools/e2e_drill.py` 为模拟 CI runner 需临时藏起 config 私密文件。
    旧实现把暂存名定为 `<file>.e2e_bak`（**就放在 config/ 里**），
    且恢复时带一条 `not os.path.exists(p)` 守卫：

        if os.path.exists(b) and not os.path.exists(p):
            shutil.move(b, p)

    只要演练过程中任何代码重建了 p（`_prepare_site_users` 就会写
    users.json；pipeline 也可能写 notify.json），真配置被永久遗弃在
    `.e2e_bak`，而生效的是演练造的残缺配置。

    实测后果（2026-09-15 20:43 现场）：
        config/notify.json      缺失  →  本地 task 全部退化成无凭据 dry-run
        config/watch.json       缺失  →  自选股全丢
        config/holdings.json    缺失  →  持仓全丢
        config/users.json       是演练重写的假配置

    最恶劣的是：我拿这个**自己造成的破坏**当证据，反复向用户解释
    「本地没推送是正常的（无凭据）」。这就是"每天说没问题、实盘就出问题"
    的循环本身。

⚠️ 本测试自身的纪律（2026-09-15 第二次踩坑）：
    初版这些用例**直接在真实 config/ 目录上**跑 _hide_private/_restore，
    用例之间互相污染，跑完把 notify.json/watch.json 又弄丢一次。
    现在统一改为：把 e2e_drill 的 ROOT 重定向到一个 **tempdir 沙箱**，
    所有文件操作都发生在沙箱里，真实 config/ 只读不改。

本测试锁死三件事：
    1. `_hide_private` 的暂存文件**不得**落在 config/ 目录内；
    2. `_restore` 必须**无条件覆盖**回原位（不得有"目标已存在就跳过"）；
    3. `_recover_orphans` 能复原历史遗留，且**不覆盖**内容不一致的孤儿。
"""
import glob
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

REAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_CFG = os.path.join(REAL_ROOT, "config")


def _load_drill_for(root):
    """加载 tools/e2e_drill.py，并把它的 ROOT 指向指定沙箱目录。

    这样 _hide_private / _restore / _recover_orphans 全部只作用于沙箱，
    真实 config/ 完全不被触碰。
    """
    p = os.path.join(REAL_ROOT, "tools", "e2e_drill.py")
    spec = importlib.util.spec_from_file_location(
        f"e2e_drill_sbx_{id(root)}", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.ROOT = root
    return m


class SandboxCase(unittest.TestCase):
    """基类：每个用例一个独立沙箱，内含四个私密文件的合成副本。"""

    SEED = {
        "notify.json": json.dumps(
            {"primary_channel": "pushplus", "pushplus_token": "SEED_TOKEN",
             "push_dry_run": False, "glm_model": "glm-4.7-flash"},
            ensure_ascii=False),
        "users.json": json.dumps(
            {"users": [{"id": "owner", "pass": "SEED_PASS", "roles": ["all"]}]},
            ensure_ascii=False),
        "watch.json": json.dumps(["sh600359"], ensure_ascii=False),
        "holdings.json": "[]",
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e_sbx_")
        cfg = os.path.join(self.tmp, "config")
        os.makedirs(cfg, exist_ok=True)
        for name, body in self.SEED.items():
            with open(os.path.join(cfg, name), "w", encoding="utf-8") as f:
                f.write(body)
        self.sbx = _load_drill_for(self.tmp)
        self.cfg = cfg

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, name):
        with open(os.path.join(self.cfg, name), encoding="utf-8") as f:
            return f.read()

    def _exists(self, name):
        return os.path.exists(os.path.join(self.cfg, name))


class TestConfigSafety(SandboxCase):

    # ---- 1. 暂存目录必须在 config 之外 ----
    def test_staging_dir_outside_config(self):
        moved = self.sbx._hide_private(True)
        try:
            self.assertTrue(moved, "配置存在时 _hide_private 必须移动到东西")
            for live, bak in moved.items():
                bak_dir = os.path.dirname(os.path.abspath(bak))
                self.assertNotEqual(
                    os.path.normcase(bak_dir),
                    os.path.normcase(os.path.abspath(self.cfg)),
                    f"暂存文件不得落在 config/ 内：{bak}")
                # 也不能是 <原文件>.e2e_bak 这种同目录形式
                self.assertFalse(
                    bak.startswith(live),
                    f"暂存名不得由原路径派生（正是旧 bug 的形态）：{bak}")
                self.assertTrue(os.path.exists(bak), f"暂存文件应存在：{bak}")
        finally:
            self.sbx._restore(moved)

    # ---- 2. 恢复必须覆盖，不得"已存在就跳过" ----
    def test_restore_overwrites_recreated_file(self):
        """模拟旧 bug 的触发条件：隐藏后有人重建了同名文件。"""
        moved = self.sbx._hide_private(True)
        self.assertTrue(moved)
        try:
            for live in moved:
                with open(live, "w", encoding="utf-8") as f:
                    f.write('{"__decoy__": true}')
        finally:
            self.sbx._restore(moved)
        for live in moved:
            name = os.path.basename(live)
            got = self._read(name)
            self.assertNotIn("__decoy__", got,
                             f"{name} 未被真配置覆盖回——旧 bug 复现！")
            self.assertEqual(got, self.SEED[name])

    # ---- 3. 孤儿复原：缺失才补 ----
    def test_recover_orphans_restores_missing_only(self):
        target = os.path.join(self.cfg, "watch.json")
        orphan = target + ".e2e_bak"
        os.remove(target)
        with open(orphan, "w", encoding="utf-8") as f:
            f.write('["sh000001"]')
        fixed, kept = self.sbx._recover_orphans()
        self.assertIn((orphan, target), fixed, "缺失时应复原孤儿")
        self.assertEqual(self._read("watch.json"), '["sh000001"]')
        self.assertFalse(os.path.exists(orphan), "复原后孤儿应消失")

    # ---- 4. 孤儿冲突：不覆盖 ----
    def test_recover_orphans_keeps_conflicting(self):
        target = os.path.join(self.cfg, "watch.json")
        orphan = target + ".e2e_bak"
        with open(orphan, "w", encoding="utf-8") as f:
            f.write('["sh999999"]')
        fixed, kept = self.sbx._recover_orphans()
        self.assertIn((orphan, target), kept,
                      "内容冲突时必须保留孤儿，不得自动覆盖")
        self.assertTrue(os.path.exists(orphan))
        # 正式文件内容不得被改动
        self.assertEqual(self._read("watch.json"), self.SEED["watch.json"])

    # ---- 5. config_state 语义 ----
    def test_config_state_reports_presence(self):
        st = self.sbx._config_state()
        for f in self.sbx.SECRET_FILES:
            self.assertIn(f, st)
            self.assertIsInstance(st[f], bool)
        self.assertTrue(all(st.values()), "沙箱里四个文件都应在场")
        # 藏起来之后必须报 False
        moved = self.sbx._hide_private(True)
        try:
            st2 = self.sbx._config_state()
            self.assertFalse(any(st2.values()),
                             "藏起来后 _config_state 必须报缺失")
        finally:
            self.sbx._restore(moved)

    # ---- 6. 端到端：hide→重建→restore 一轮后，四个文件逐字节还原 ----
    def test_roundtrip_is_byte_identical(self):
        before = {n: self._read(n) for n in self.SEED}
        moved = self.sbx._hide_private(True)
        # 期间模拟 pipeline 乱建文件
        for n in self.SEED:
            with open(os.path.join(self.cfg, n), "w", encoding="utf-8") as f:
                f.write("GARBAGE")
        self.sbx._restore(moved)
        after = {n: self._read(n) for n in self.SEED}
        self.assertEqual(before, after, "hide→restore 一轮后必须逐字节一致")

    # ---- 7. 无残留 ----
    def test_no_e2e_bak_residue(self):
        self.sbx._hide_private(True)  # 故意不 restore
        # 即便不 restore，也绝不能把 .e2e_bak 留在 config/ 里
        left = glob.glob(os.path.join(self.cfg, "*.e2e_bak"))
        self.assertEqual(left, [], f"config 目录残留 .e2e_bak：{left}")


class TestRealConfigUntouched(unittest.TestCase):
    """铁律：跑完本文件后，真实 config/ 必须与跑之前完全一致。"""

    def test_no_e2e_bak_in_real_config(self):
        left = glob.glob(os.path.join(REAL_CFG, "*.e2e_bak"))
        self.assertEqual(left, [],
                         f"真实 config/ 残留 .e2e_bak：{left}")

    def test_real_notify_present(self):
        """本地必须有 notify.json（否则静默退化成无凭据 dry-run）。

        2026-09-15 修：CI 上这个文件**必然不存在**——它是隐私文件，
        在 .gitignore 里，CI 靠 Secrets（PUSHPLUS_TOKEN 等）注入配置。
        原断言不分环境一律要求存在，导致 CI 回归自检必挂 → 后面 7 个
        步骤全 skipped → 全天零推送（实测 run 34985391295）。
        判据：本地（有 .git 且非 CI）要求存在；CI 要求 Secret 已注入。
        """
        in_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
        p = os.path.join(REAL_CFG, "notify.json")
        if in_ci:
            # CI 环境下凭据走 Secrets：至少要有一种推送凭据的 env
            has_secret = any(os.environ.get(k) for k in (
                "PUSHPLUS_TOKEN", "SERVERCHAN_KEY", "WXPUSHER_CONF"))
            self.assertTrue(
                has_secret or os.path.exists(p),
                "CI 环境既没有 config/notify.json，也没有任何推送 Secret "
                "（PUSHPLUS_TOKEN / SERVERCHAN_KEY / WXPUSHER_CONF）——"
                "会静默退化成 dry-run，不推送")
            return
        self.assertTrue(os.path.exists(p),
                        "真实 config/notify.json 缺失——本地任务会静默退化"
                        "成无凭据 dry-run，正是血案现场")


class TestJudgeIsNotEmpty(unittest.TestCase):
    """判定函数：rc=0 不等于 PASS（2026-09-15 第二个坑）。"""

    @classmethod
    def setUpClass(cls):
        cls.drill = _load_drill_for(REAL_ROOT)

    def test_reject_rc_zero_with_refusal(self):
        r = {"task": "pre", "rc": 0, "out": "[build] 2026-09-15 拒绝构建"
             "（2026-09-15 无快照（竞价数据未入库））"}
        ok, why = self.drill._judge(r, "pre")
        self.assertFalse(ok, "rc=0 但『拒绝构建』必须判 FAIL")
        self.assertTrue(any("拒绝构建" in w for w in why))

    def test_reject_rc_zero_no_output(self):
        r = {"task": "close", "rc": 0, "out": ""}
        ok, why = self.drill._judge(r, "close")
        self.assertFalse(ok, "rc=0 且无成功标志必须判 FAIL")

    def test_reject_nonzero_rc(self):
        r = {"task": "close", "rc": 1, "out": "[build] 构建完成"}
        ok, why = self.drill._judge(r, "close")
        self.assertFalse(ok)
        self.assertTrue(any("退出码" in w for w in why))

    def test_accept_real_success(self):
        r = {"task": "close", "rc": 0,
             "out": "[build] 构建完成，已发推送"}
        ok, why = self.drill._judge(r, "close")
        self.assertTrue(ok, f"真实产出应判 PASS，却得到 {why}")


if __name__ == "__main__":
    unittest.main()
