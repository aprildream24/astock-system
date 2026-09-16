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
        """根目录不得残留调试/回归产物。

        用**白名单**而非"排除 _ 前缀"：黑名单列不全。曾因 `_deploy_out.txt`
        不在前缀表里而被推上 CI → runner 上本套件挂 → 全天零推送。
        """
        stray = [f for f in self.files if os.sep not in f and "/" not in f
                 and f not in self.dep.ROOT_ALLOW
                 and os.path.splitext(f)[1] not in self.dep.ROOT_ALLOW_EXT]
        self.assertEqual(stray, [], f"根目录文件不在白名单：{stray}")

    def test_underscore_root_files_never_deployed(self):
        """任何根目录下划线开头文件都不得上线（无论叫什么名字）。"""
        bad = [f for f in self.files if os.sep not in f and "/" not in f
               and f.startswith("_")]
        self.assertEqual(bad, [], f"下划线临时文件混入部署：{bad}")

    def test_backups_not_deployed(self):
        """*.bak / *.ciparity_bak 之类的备份不得上线。"""
        bad = [f for f in self.files if f.endswith((".bak", ".ciparity_bak"))]
        self.assertEqual(bad, [], f"备份文件混入部署：{bad}")


class TestDeploySecretLeakGuards(unittest.TestCase):
    """2026-09-16 血案：**明文站点口令进了公开仓库**。

    `config/users.json.bak`（内容 `astra-owner-2026`/`astra-guest-2026`）在
    公开仓库里躺了数天。成因是**双层黑名单同时漏网**：
      · `EXCLUDE_FILES` 按**精确文件名**排 `users.json` —— `.bak` 名字不同；
      · `EXCLUDE_EXT` 的 `.bak` 是**事后**才补的，而 `sync()` **只增不删**
        ⇒ 已经推上去的文件**永远不会被撤下**。
    本类把「未来不再推」和「历史要清理」两件事一起钉死。
    """

    @classmethod
    def setUpClass(cls):
        cls.dep = _load_deploy()
        cls.files = cls.dep.collect_files()

    # ---- 未来：不该被收集 ----
    def test_config_dir_is_whitelisted(self):
        """`config/` 只允许 `*.example.json` 上线（白名单，不是黑名单）。"""
        bad = [f for f in self.files
               if f.startswith("config/")
               and not os.path.basename(f).endswith(".example.json")]
        self.assertEqual(bad, [], f"config/ 非样例文件混入部署：{bad}")
        self.assertIn("config/users.example.json", self.files)

    def test_no_backup_variants_of_secret_files(self):
        """密钥文件的任何变体（.bak/.old/.tmp/带时间戳…）都不得上线。"""
        for name in ("config/users.json.bak", "config/notify.json.bak",
                     "config/users.json.old", "config/holdings.json.tmp"):
            self.assertNotIn(name, self.files)

    def test_underscore_files_in_subdirs_never_deployed(self):
        """`_` 前缀 = 本机调试产物，**任何目录**都不得上线。

        血案里 `tests/_reg.out.txt` 等 5 个回归输出被推上公开仓库 ——
        根目录早已防住，子目录却漏着。
        """
        bad = [f for f in self.files
               if os.path.basename(f).startswith("_")]
        self.assertEqual(bad, [], f"下划线临时文件混入部署：{bad}")

    def test_local_artifact_dirs_never_deployed(self):
        """`Temp/`、`build_tmp/` 是本机产物目录，不得上线。"""
        bad = [f for f in self.files
               if f.startswith(("Temp/", "build_tmp/"))]
        self.assertEqual(bad, [], f"本机产物目录混入部署：{bad}")

    # ---- 历史：远端要清理 ----
    def test_should_purge_catches_leaked_paths(self):
        for p in ("config/users.json.bak", "Temp/gh2.txt",
                  "Temp/remote_ledger.json", "tests/_reg.out.txt",
                  "build_tmp/procs.txt", "_deploy_out.txt",
                  ".workbuddy/memory/MEMORY.md", "notify.json"):
            self.assertTrue(self.dep.should_purge(p), f"{p} 应被清理")

    def test_should_purge_never_touches_repo_content(self):
        """正常仓库文件绝不能被误删（误删=又一次全天零推送）。"""
        for p in (".github/workflows/stock.yml", "README.md", ".gitignore",
                  "pipeline/build.py", "tests/test_deploy.py",
                  "config/users.example.json", "docs/STRATEGY_LOCK.md",
                  "site_template/app.js", "tools/deploy.py"):
            self.assertFalse(self.dep.should_purge(p), f"{p} 被误判为遗留物")

    def test_should_purge_protects_dist_ledger(self):
        """`dist/push_ledger.json` 是 CI 维护的账本权威 —— 本地不收集它，
        若按"未被收集就删"的粗暴对齐逻辑会把账本删掉（保险丝随即失效）。"""
        for p in ("dist/push_ledger.json", "dist/reports/2026-09-16.json",
                  "dist/data/abc.bin"):
            self.assertFalse(self.dep.should_purge(p), f"{p} 属 dist/ 受保护")

    def test_purge_is_wired(self):
        """部署流程必须真的调用 purge —— 只加规则不清理 = 口令继续挂着。"""
        with open(os.path.join(ROOT, "tools", "run_deploy.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("purge", src)
        import inspect
        self.assertIn("def purge(", inspect.getsource(self.dep))


if __name__ == "__main__":
    unittest.main()
