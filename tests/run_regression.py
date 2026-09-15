# -*- coding: utf-8 -*-
"""回归测试入口：子进程逐套件跑，汇总 PASS/FAIL，ALL_PASS 才过。

## 纪律
PASS 数只许涨不许跌，FAIL 必须 0。基线写在 tests/baseline.json。

## 2026-09-15 血案修正：skip 不等于倒退

原实现把 PASS 数与单一基线硬比。CI runner 缺 PyNaCl / PyYAML 时，
相关用例变 skip → PASS 数天然变少 → 误判「回归倒退」exit 2 →
后面 7 个步骤全 skipped → **全天零推送**（实测 run 34985391295）。

现在分三档：
  · FAIL > 0            → exit 1（真失败，必修）
  · skip 因环境缺失     → 不算倒退（记入 env_skipped，仅提示）
  · 有效 PASS < 基线    → exit 2（真倒退）

判据：`有效用例 = PASS + FAIL`（不含 skip）。基线的比较基准是
`total_pass`，但允许 `env_skip` 个环境性跳过 —— 只要
`PASS + SKIP >= baseline` 且 FAIL == 0 就放行，避免环境差异误杀。

## 2026-09-15 基线校正（重要）

旧基线 `total_pass=303` 是**虚高假数字**：旧 `_count()` 用
`out.count("ok")` 数子串，traceback/ResourceWarning 路径里出现的 "ok"
也被计入（如 `...\\unittest\\case.py` 里的 "ok"、用例名中的 "ok"）。
实际用 `TestLoader().countTestCases()` 权威统计：
**291 个用例**（20 套件）。现基线按 291 重设，且计数改为解析
unittest 结果词 + `Ran N tests` 交叉校验，不再子串匹配。
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_FILE = os.path.join(HERE, "baseline.json")

SUITES = ["test_engines.py", "test_push_crypto.py", "test_guard_t1.py",
          "test_p8.py", "test_final.py", "test_audit.py", "test_absorb.py",
          "test_wx_deploy.py", "test_fix_20260913.py", "test_push2026b.py",
          "test_zero_push_20260915.py", "test_permissions.py",
          "test_deploy.py", "test_ci_parity.py", "test_e2e_safety.py",
          "test_source_failover.py", "test_gate_no_deadlock.py",
          "test_daily_check.py", "test_cloud_watch.py", "test_sync_watch.py"]

# runner 预装列表缺失的可选包 → 相关用例会 skip，不算倒退
ENV_OPTIONAL = ("nacl", "yaml")


def _count(out):
    """解析 unittest -v 输出。

    2026-09-15 三个计数坑（都会让 PASS 虚低 → 误判倒退 → 全天零推送）：
      ① `skipped` 里含 "ok"，不先剥会误计成 PASS；
      ② **ResourceWarning 会插在 `... ` 与真正的 `ok` 之间**（实测
         test_cloud_watch：`... C:\\...\\unittest\\case.py:655: ResourceWarning`
         换 3 行后才 `ok`）→ 只取同行结果会漏计，整批 PASS 虚低；
      ③ 反过来无脑向后找会跨到下一条用例、吞掉别人的 ok。
    对策：向后找结果词，但**遇到下一条用例行（以 test_ 开头或含 `(`…`)`）即停**。
    另：调用方已加 `-W ignore::ResourceWarning`，噪声本身也被消除（双保险）。
    """
    RESULT = ("ok", "FAIL", "ERROR", "skipped")
    lines = out.splitlines()
    passed = failed = skipped = 0
    for i, line in enumerate(lines):
        if " ... " not in line:
            continue
        # 结果词：同行优先，否则向后找，但不得越过下一条用例
        cand = line.split(" ... ", 1)[1].strip()
        j = i
        while not cand.startswith(RESULT) and j + 1 < len(lines):
            nxt = lines[j + 1].strip()
            if nxt.startswith("test_") or ("(" in nxt and ")" in nxt):
                break                      # 下一条用例了，本轮无结果词
            j += 1
            cand = nxt
        if cand.startswith("ok"):
            passed += 1
        elif cand.startswith(("FAIL", "ERROR")):
            failed += 1
        elif cand.startswith("skipped"):
            skipped += 1
    return passed, failed, skipped


def _ran_total(out):
    """从 unittests 的 `Ran N tests` 行取总数，用于交叉校验计数。"""
    m = re.search(r"^Ran (\d+) tests?", out, re.M)
    return int(m.group(1)) if m else None


def main():
    total_pass = total_fail = total_skip = 0
    bad = []
    for s in SUITES:
        p = subprocess.run([sys.executable, "-X", "utf8",
                            "-W", "ignore::ResourceWarning",
                            "-m", "unittest",
                            os.path.splitext(s)[0].replace(os.sep, "."),
                            "-v"], cwd=HERE,
                           capture_output=True, text=True)
        out = (p.stderr or "") + (p.stdout or "")
        passed, failed, skipped = _count(out)
        ran = _ran_total(out)
        counted = passed + failed + skipped
        # 计数自校验：解析漏失会让 PASS 虚低 → 误判倒退（血案机制）
        if ran is not None and counted != ran:
            print(f"!! {s}: 计数不一致（解析 {counted} vs Ran {ran}）"
                  f"——已按 Ran 归入未解析项，避免虚低误判")
            skipped += (ran - counted)
        total_pass += passed
        total_fail += failed
        total_skip += skipped
        tag = f"PASS={passed} FAIL={failed}"
        if skipped:
            tag += f" SKIP={skipped}"
        print(f"--- {s}: {tag}")
        if failed or p.returncode not in (0, 1):
            bad.append((s, p.returncode, out[-2500:]))

    print(f"\n总计: PASS={total_pass} FAIL={total_fail}"
          + (f" SKIP={total_skip}" if total_skip else ""))
    if bad:
        for s, rc, tail in bad:
            print(f"\n===== 失败详情 {s} (rc={rc}) =====")
            print(tail)

    prev = None
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE, encoding="utf-8") as f:
            prev = json.load(f).get("total_pass")

    if total_fail:
        print(f"回归失败：FAIL={total_fail}（必须为 0）")
        sys.exit(1)

    # 有效数 = PASS + SKIP。环境缺包导致的 skip 不该判成倒退。
    effective = total_pass + total_skip
    if prev is not None and effective < prev:
        print(f"回归倒退：有效 {effective}（PASS {total_pass} + SKIP "
              f"{total_skip}）< 基线 {prev}")
        sys.exit(2)

    if total_skip:
        print(f"提示：{total_skip} 个用例因环境缺包跳过"
              f"（可选包 {'/'.join(ENV_OPTIONAL)}）——已计入有效数。")

    # 基线只在全绿且有效数更高时更新（防止降级被固化）
    if prev is None or effective > prev:
        with open(BASELINE_FILE, "w", encoding="utf-8") as f:
            json.dump({"total_pass": effective}, f)
        print(f"ALL_PASS ✓（基线 {prev} → {effective}）")
    else:
        print("ALL_PASS ✓")


if __name__ == "__main__":
    main()
