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
          "test_daily_check.py", "test_cloud_watch.py", "test_sync_watch.py",
          # 2026-09-16 新增：周末/法定节假日休市静默（一天只提示一条）
          "test_ci_robustness.py", "test_holiday.py",
          # 2026-09-16 新增：盘前K线新鲜度锚（防候选 0）
          "test_preauction.py",
          # 2026-09-16 新增：盘前/竞价「筛选快照口径」+ 推送传输层重试
          # （血案：用户全天空推送 + 唯一一条是候选 0 只的空计划）
          "test_premarket_snapshot_scope.py",
          # 2026-09-16 新增：M41 盘中计划校验（只读实时快照/零污染主表 +
          # 没有机会就不凑数的打扰纪律）
          "test_intraday_scope.py",
          # 2026-09-16 新增：推送验收/自动补发（云端 watchdog）。
          # 血案：CI 步骤全绿但 build_pre 状态 uncertain、用户端零消息
          # —— 「步骤绿 ≠ 送达」，验收必须落到远端账本的 mode+status。
          "test_push_audit.py",
          # 2026-09-16 新增：cron-job.org 定时器守门（主链 100% 依赖它，
          # 而云端 watchdog 自己也被同一个调度器触发 ⇒ 需独立守门覆盖）。
          "test_timer_guard.py",
          # 2026-09-18 新增：板块热度标注 + 行情好放开限量（用户需求：
          # "行情好时针对评分高的个股全部推荐、标注板块热度、不再限制 3 个"）。
          # 同时锁住两处**静默失效**：sector_temp 冷热因子与同板块去重
          # 此前都因缺数据源而从未生效。
          "test_sector_heat.py",
          # 2026-09-19 新增：决断门控扩展（波段池）+ 躺榜衰减 + push_modes 开关
          # + 前端持仓管理契约（用户四问：要么上要么下/网页改持仓/消息太多）。
          "test_decisive_20260919.py",
          # 2026-09-19 第二批：RS 超额动量因子（横截面动量，qlib Alpha158 同源）
          "test_rs_momentum.py",
          # 2026-09-18 新增：模拟盘自动运行（用户："是不是还有模拟盘没运行？
          # 按 100000 元起步开始自动运行"）。根因：executor 只有退出裁决、
          # 没有任何买入路径 ⇒ 永远空仓 ⇒ 无日志 ⇒ 一条推送都不发。
          "test_executor_auto.py",
          # 2026-09-18 第二轮新增（用户 5 条需求）：
          #   ① 模拟盘版式与主推送同源（"到底买了什么、持有什么完全不知道"）
          #   ② 到价了却买不进也要说清原因（"比如资金不足等等"）
          #   ③ 周末/节假日/非交易时段一律不建仓（"今天已经不在交易时间了
          #      又开始购买"）
          #   ④ 标题规范【模拟】【Astra】/【竞价】【Astra】（"分不清"）
          #   ⑤ 只在有实质动作时推送（"推送消息太多"）
          # 附带锁住：place_order 此前**没有资金充足性检查**（现金能买成负数）
          # 与本套件自身的账本隔离纪律（血案：第一轮就把 exec_auto 写进仓库
          # 账本，第二轮 _daily_sent 命中导致自爆）。
          "test_exec_push.py",
          # 2026-09-18 第三轮新增（用户实盘需求）：真实持仓体检 + 换股建议。
          #   · evaluate_exit 的 cost_override 口径（实盘不在模拟盘批次 → 必须
          #     外部传 buy_price，否则 pnl 恒 +0.0% 把亏损判成完好）
          #   · evaluate_real_holdings 结构化体检（浮亏/裁决/板块热冷）
          #   · render_holding_advice 三段式（概要/体检/候选）
          #   · build.py 在 review 挂 holding_check，且只在 actionable 时推（降噪）
          "test_holding_check.py"]

# runner 预装列表缺失的可选包 → 相关用例会 skip，不算倒退
ENV_OPTIONAL = ("nacl", "yaml")


def _count(out):
    """解析 unittest -v 输出。

    真实输出形态（实测，`-v` 模式）——**用例头一行、结果另一行**：
        test_xxx (pkg.mod.Class.test_xxx)
        用例的中文 docstring。 ... ok
        test_yyy (pkg.mod.Class.test_yyy) ... ok     ← 无 docstring 时同行
        某用例 ... skipped '原因'

    2026-09-15 四个计数坑（都会让 PASS 虚低 → 误判倒退 → 全天零推送）：
      ① `skipped` 里含 "ok"，不先判 ok 会误计成 PASS；
      ② **ResourceWarning 会插在 `... ` 与真正的 `ok` 之间**（实测
         test_cloud_watch：`...\\unittest\\case.py:655: ResourceWarning`
         之后两三行才 `ok`）→ 只看同行会漏计；
      ③ 无脑向后找会跨到下一条用例、吞掉别人的 ok；
      ④ **不能用 `" ... " in line` 模糊匹配** —— 用例头行本身通常不含
         `... `（结果在下一行），而 docstring 行反而含 `... ` → 判据反了
         会大面积漏计（实测一次漏 82 条）。
    对策：以「带括号路径的用例头」为锚，逐个头向后找结果词，
    遇下一个用例头 / 段落分隔即停。调用方另加
    `-W ignore::ResourceWarning` 消除噪声源（双保险）。
    """
    RESULT = ("ok", "FAIL", "ERROR", "skipped")
    # 用例头形态：`test_xxx (pkg.mod.Class.test_xxx)`（带括号路径）。
    # 只认这种行做锚，普通输出/docstring 行不会被误当用例。
    CASE_RE = re.compile(r"^test_\w+\s*\(\S+\.\S+\.\S+\)")

    def _verdict(text):
        """从一行文本里取结果词。

        结果可能同行（`test_x (m.T.test_x) ... ok`），也可能在后续行、
        且**前面还挂着 docstring**（`某说明。 ... ok`）——所以必须按
        ` ... ` 切一刀取后半段，不能直接对整行 startswith。

        注意必须切**最后一个** ` ... `：docstring 自身可能也含 ` ... `
        （本套件的用例就在讲这个坑），按第一个切会把说明文字当成结果词，
        该用例被判为「无结果」→ 漏计。
        """
        t = text.strip()
        if " ... " in t:
            t = t.rsplit(" ... ", 1)[1].strip()
        for word in RESULT:
            if t.startswith(word):
                return word
        return ""

    lines = [l.rstrip() for l in out.splitlines()]
    passed = failed = skipped = 0
    for i, line in enumerate(lines):
        rest = line.strip()
        if not CASE_RE.match(rest):
            continue
        # 先用本行判定；无果则逐行向后找，遇下一个用例头 / 分隔线即停。
        verdict = _verdict(rest)
        j = i
        while not verdict and j + 1 < len(lines):
            nxt = lines[j + 1].strip()
            if CASE_RE.match(nxt):
                break                      # 下一条用例了，本轮无结果词
            if nxt.startswith("====") or nxt.startswith("----"):
                break                      # 段落分隔
            j += 1
            verdict = _verdict(nxt)
        if verdict == "ok":
            passed += 1
        elif verdict in ("FAIL", "ERROR"):
            failed += 1
        elif verdict == "skipped":
            skipped += 1
    return passed, failed, skipped


def _ran_total(out):
    """从 unittests 的 `Ran N tests` 行取总数，用于交叉校验计数。"""
    m = re.search(r"^Ran (\d+) tests?", out, re.M)
    return int(m.group(1)) if m else None


def main():
    total_pass = total_fail = total_skip = total_unresolved = 0
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
        # 计数自校验：解析漏失会让 PASS 虚低 → 误判倒退（血案机制）。
        # 注意：漏失项归入 unresolved 而非 skipped —— 混进 skipped 会虚高
        # 「有效数」（PASS+SKIP），把真正的解析丢失洗白，反而放过回归。
        unresolved = 0
        if ran is not None and counted != ran:
            unresolved = ran - counted
            print(f"!! {s}: 计数不一致（解析 {counted} vs Ran {ran}）"
                  f"——{unresolved} 条结果未被识别")
        total_pass += passed
        total_fail += failed
        total_skip += skipped
        total_unresolved += unresolved
        tag = f"PASS={passed} FAIL={failed}"
        if skipped:
            tag += f" SKIP={skipped}"
        if unresolved:
            tag += f" UNRESOLVED={unresolved}"
        print(f"--- {s}: {tag}")
        if failed or p.returncode not in (0, 1):
            bad.append((s, p.returncode, out[-2500:]))

    print(f"\n总计: PASS={total_pass} FAIL={total_fail}"
          + (f" SKIP={total_skip}" if total_skip else "")
          + (f" UNRESOLVED={total_unresolved}" if total_unresolved else ""))
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

    # 解析不出结果 = 计数器不可信。绝不能静默放行：宁可报错重跑，
    # 也不要基于一个失真的 PASS 数决定"可以推送了"。
    if total_unresolved:
        print(f"回归失败：{total_unresolved} 条用例结果未被解析"
              f"（计数器失准，须修 _count 后重跑）")
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
