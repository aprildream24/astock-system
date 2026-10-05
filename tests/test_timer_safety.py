# -*- coding: utf-8 -*-
"""cron-job 定时器安全回归锁（2026-10-05，用户澄清后的永久防线）。

用户原话：「cron-job.org 的还有另一个系统是 stock-/exec-，所以不需要删」。

历史事故：tools/cron_cleanup.py 的 2026-09-25 版本会把 stock-* / exec-*
前缀的定时器当「旧系统遗留」**删除**——而那是用户另一个在用系统的触发器。
本套件从源码层锁死：任何定时器相关的 CI 工具都**不得含删除/写操作**，
防止同类事故以任何形式回归。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TOOLS = os.path.join(ROOT, "tools")


class TestTimerToolSafety(unittest.TestCase):
    def _src(self, name):
        return open(os.path.join(TOOLS, name), encoding="utf-8").read()

    def test_核验脚本永不含删除操作(self):
        src = self._src("cron_cleanup.py")
        self.assertNotIn('"DELETE"', src)
        self.assertNotIn("'DELETE'", src)
        self.assertNotIn('method="DELETE"', src)
        self.assertIn("只读核验", src,
                      "脚本必须是只读核验形态")

    def test_核验脚本明确承认另一系统不可碰(self):
        src = self._src("cron_cleanup.py")
        self.assertIn("stock-*", src)
        self.assertIn("绝不触碰", src)

    def test_timer_live_只操作astock前缀(self):
        src = self._src("timer_live.py")
        self.assertIn("astock-", src)
        self.assertIn("一律不碰", src,
                      "timer_live 必须显式声明不碰其他系统前缀")
        self.assertNotIn('"DELETE"', src)

    def test_工作流不再描述删除行为(self):
        wf = open(os.path.join(ROOT, ".github", "workflows", "stock.yml"),
                  encoding="utf-8").read()
        self.assertNotIn("删除旧系统", wf,
                         "cron-cleanup 分支注释不得再声称删除行为")
        self.assertIn("只读核验", wf)


if __name__ == "__main__":
    unittest.main()
