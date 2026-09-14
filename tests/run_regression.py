# -*- coding: utf-8 -*-
"""回归测试入口：子进程逐套件跑，汇总 PASS/FAIL，ALL_PASS 才过。
基线：任何改动后 PASS 数只许涨不许跌。"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_FILE = os.path.join(HERE, "baseline.json")

SUITES = ["test_engines.py", "test_push_crypto.py", "test_guard_t1.py",
          "test_p8.py", "test_final.py", "test_audit.py", "test_absorb.py",
          "test_wx_deploy.py", "test_fix_20260913.py", "test_push2026b.py"]


def main():
    total_pass, total_fail = 0, 0
    bad = []          # (套件名, returncode, stderr 尾部) —— CI 上失败时用于定位
    for s in SUITES:
        p = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest",
                            os.path.splitext(s)[0].replace(os.sep, "."),
                            "-v"], cwd=HERE,
                           capture_output=True, text=True)
        out = p.stderr
        passed = out.count("ok")
        failed = out.count("FAIL") + out.count("ERROR")
        total_pass += passed
        total_fail += failed
        print(f"--- {s}: PASS={passed} FAIL={failed}")
        if failed or p.returncode:
            bad.append((s, p.returncode, (out or p.stdout or "")[-2500:]))
    print(f"\n总计: PASS={total_pass} FAIL={total_fail}")
    if bad:
        # 只在失败时打印，避免正常运行时淹没输出
        for s, rc, tail in bad:
            print(f"\n===== 失败详情 {s} (rc={rc}) =====")
            print(tail)
    prev = None
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE, encoding="utf-8") as f:
            prev = json.load(f).get("total_pass")
    if prev is not None and total_pass < prev:
        print(f"回归倒退：{total_pass} < 基线 {prev}")
        sys.exit(2)
    if total_fail:
        sys.exit(1)
    with open(BASELINE_FILE, "w", encoding="utf-8") as f:
        json.dump({"total_pass": total_pass}, f)
    print("ALL_PASS ✓（基线已更新）")


if __name__ == "__main__":
    main()
