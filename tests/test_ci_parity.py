# -*- coding: utf-8 -*-
"""CI 环境对等性测试（2026-09-15 新增）。

背景（真实事故）：本仓库的回归套件原本依赖本机 `config/*.json`（口令、推送
密钥、自选名单都在里面，且**不入库**）。CI runner 上没有这些文件 ⇒
第 5 步「回归自检」必挂 ⇒ 后续「构建+推送」「构建加密站点」全部 skipped
⇒ **全天零推送**，而表面上每次 run 都"跑过了"。

这条链路极隐蔽：本机跑一定全绿，只有 CI 才挂。所以必须有一个测试，
在「剥离全部私有配置」的状态下验证套件仍能跑通。

设计要点（踩过的坑，务必保持）：
1. **不得递归**：早期版本 spawn `tests/run_regression.py` 做整轮回归，
   而整轮里又包含本套件 → 无限递归、900s 超时。改为只跑**受影响的
   子集**（推送相关套件），由环境变量 `CI_PARITY_CHILD=1` 二次保险
   防递归。
2. **不得留残留**：改名配置文件必须 atexit + finally 双兜底；测试前后
   各清一次历史残留。残留会污染本机 config（曾真实发生过）。
3. **不得依赖网络**：只做「无凭据时必须仍通过」的断言。
"""
import atexit
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE = ("users.json", "watch.json", "holdings.json", "notify.json")
BAK = ".ciparity_bak"

# 受本机配置影响的套件（断言 sent 的那些）。整轮回归不在此列——
# 那会导致递归。
AFFECTED_SUITES = ("test_push_crypto", "test_final", "test_push2026b")


def _restore(cfg, moved):
    for p, b in moved.items():
        try:
            if os.path.exists(b) and not os.path.exists(p):
                shutil.move(b, p)
        except OSError:
            pass


def _purge_residue(cfg):
    """清理上次被强杀留下的残留。"""
    moved = {}
    for n in PRIVATE:
        b = os.path.join(cfg, n + BAK)
        if os.path.exists(b):
            moved[os.path.join(cfg, n)] = b
    _restore(cfg, moved)
    return moved


class TestCIParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cfg = os.path.join(ROOT, "config")
        _purge_residue(cls.cfg)

    def test_no_config_residue_left_behind(self):
        """私有配置不得残留 .ciparity_bak——本机 config 必须干净。"""
        residue = [f for f in os.listdir(self.cfg) if f.endswith(BAK)]
        self.assertEqual(residue, [], f"config 有测试残留：{residue}")

    def test_sent_assertions_are_mocked(self):
        """静态断言：凡断言 sent 的用例，必须显式固定通道配置。

        CI 无 config/notify.json → load_config() 返回空 → 通道列表为空 →
        sent=False。任何"裸断言 sent=True"的用例都会在 CI 必挂。
        """
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        offenders = []
        for fn in sorted(os.listdir(tests_dir)):
            if not fn.startswith("test_") or not fn.endswith(".py"):
                continue
            if fn == os.path.basename(__file__):
                continue
            src = open(os.path.join(tests_dir, fn), encoding="utf-8").read()
            if '["sent"]' not in src and "['sent']" not in src:
                continue
            mocked = ("load_config" in src and "lambda" in src) or \
                     ("resolve_targets" in src)
            if not mocked:
                offenders.append(fn)
        self.assertEqual(offenders, [],
                         "以下套件断言 sent 却未 mock 通道配置，CI 必挂："
                         f"{offenders}")

    def test_affected_suites_pass_without_private_config(self):
        """剥离私有配置后，受影响的套件必须全绿（模拟 CI runner）。

        只跑 AFFECTED_SUITES（不含本套件），避免递归。
        """
        if os.environ.get("CI_PARITY_CHILD"):
            self.skipTest("子进程内不再展开（防递归）")
        moved = {}
        for n in PRIVATE:
            p = os.path.join(self.cfg, n)
            if os.path.exists(p):
                b = p + BAK
                shutil.move(p, b)
                moved[p] = b
        atexit.register(_restore, self.cfg, moved)
        try:
            env = dict(os.environ)
            env["CI_PARITY_CHILD"] = "1"
            r = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "unittest",
                 *AFFECTED_SUITES],
                cwd=os.path.join(ROOT, "tests"), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=300,
                env=env)
            out = (r.stdout or "") + (r.stderr or "")
            fails = [l for l in out.splitlines()
                     if l.startswith(("FAIL:", "ERROR:"))]
            self.assertEqual(r.returncode, 0,
                             "无私有配置时套件失败（CI 会全天零推送）：\n"
                             + "\n".join(fails[:25]) + "\n" + out[-1200:])
        finally:
            _restore(self.cfg, moved)
            missing = [n for n in PRIVATE
                       if os.path.exists(os.path.join(self.cfg, n + BAK))]
            self.assertEqual(missing, [], f"配置未恢复：{missing}")

    # ---- workflow 契约（静态，零成本） ----

    def _wf(self):
        p = os.path.join(ROOT, ".github", "workflows", "stock.yml")
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_workflow_regression_step_matches_local_entry(self):
        """workflow 里的回归命令必须与本机入口一致。"""
        self.assertIn("python tests/run_regression.py", self._wf())

    def test_workflow_capture_steps_not_fatal(self):
        """抓取步骤必须 continue-on-error——抓取失败不得连坐推送。"""
        self.assertGreaterEqual(self._wf().count("continue-on-error: true"), 2)

    def test_workflow_timeout_has_headroom(self):
        """冷库全量实测 ≈53 分钟，超时必须显著大于它。"""
        vals = [int(l.split(":")[1]) for l in self._wf().splitlines()
                if "timeout-minutes" in l and l.strip().startswith("timeout")]
        self.assertTrue(vals, "未找到 timeout-minutes")
        self.assertGreaterEqual(max(vals), 70,
                                "超时余量不足，冷库全量会被 cancel")


if __name__ == "__main__":
    unittest.main()
