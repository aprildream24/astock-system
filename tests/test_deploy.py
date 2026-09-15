# -*- coding: utf-8 -*-
"""部署脚本（tools/deploy.py）文件收集不变量测试。

背景（2026-09-15 踩坑）：deploy.py 用"全量 tree commit"推代码——**任何未被
列出的已跟踪文件都会被这次推送从远端删除**。曾把 `.github/workflows` 排除了，
且剪枝条件写成 `not d.startswith(".git")`（把 `.github` 一起剪掉），
导致本次修复的核心文件 stock.yml（75min 超时 + continue-on-error）不会上线，
而 CI 表面上"部署成功"。这类缺陷极隐蔽，必须用测试锁死。
"""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_deploy():
    spec = importlib.util.spec_from_file_location(
        "astock_deploy", os.path.join(ROOT, "tools", "deploy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDeployCollection(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dep = _load_deploy()
        cls.files = cls.dep.collect_files()

    def test_workflows_are_synced(self):
        """CI workflow 必须同步——否则超时/容错修复永不上线。"""
        self.assertIn(".github/workflows/stock.yml", self.files)
        self.assertIn(".github/workflows/executor.yml", self.files)

    def test_dotgit_not_confused_with_dotgithub(self):
        """剪枝不得用 startswith('.git')——那会连 .github 一起剪。"""
        self.assertFalse([f for f in self.files if f.startswith(".git/")])
        self.assertTrue([f for f in self.files if f.startswith(".github/")])

    def test_no_secret_files(self):
        """口令/密钥文件绝不上传。"""
        base = {os.path.basename(f) for f in self.files}
        for bad in ("notify.json", "users.json", "holdings.json", "watch.json"):
            self.assertNotIn(bad, base, f"{bad} 泄漏到部署清单")

    def test_no_binary_or_cache(self):
        for f in self.files:
            self.assertNotIn(os.path.splitext(f)[1], (".db", ".bin", ".pyc"))
            self.assertFalse(f.startswith(("cache/", "dist/", "site/",
                                           "site/data/", ".workbuddy/")))

    def test_core_fix_files_present(self):
        """本次故障修复的每个文件都必须在清单里。"""
        for need in ("pipeline/fetch_daily.py", "pipeline/build.py",
                     "pipeline/notifier.py", "pipeline/users.py",
                     "pipeline/admin.py", "pipeline/publish.py",
                     "site_template/auth.js", "tools/deploy.py",
                     "tools/manage_panel.html"):
            self.assertIn(need, self.files, f"{need} 未纳入部署")

    def test_no_stray_temp_files(self):
        """根目录不得残留 _reg_*.txt / *.log 这类临时产物。"""
        stray = [f for f in self.files
                 if os.sep not in f and (f.startswith("_") or f.endswith(".log"))]
        self.assertEqual(stray, [], f"临时文件混入部署：{stray}")


if __name__ == "__main__":
    unittest.main()
