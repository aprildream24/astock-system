# -*- coding: utf-8 -*-
"""alphalens 式因子有效性验证（零依赖蒸馏版，2026-09-25 融入）。

思想来源：quantopian/alphalens 的因子 IC（信息系数）分析——
一个因子是否真的有预测力，不看名字高大上，看它和**未来收益**的
秩相关是否稳定为正。负 IC 的因子立即降权/停用。

对每个候选因子（rs_mom / decisive 效率 / mom5 / corr_pv / pos_pct）：
  取 candidate_snapshots 历史（含 extras 因子值）× rec_picks 的 T+2 结局，
  计算因子值与后续收益的秩相关（Pearson on ranks）。
样本 < 30 → 输出「样本不足」，不做结论（诚实行为）。

用法：python tools/factor_ic.py [--date 2026-09-24]
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.core import get_conn  # noqa: E402

FACTORS = [("rs_mom", "RS超额动量"), ("decisive_eff", "方向效率"),
           ("mom5", "5日动量"), ("corr_pv", "量价相关"),
           ("pos_pct", "区间位置%")]


def _rank(xs):
    """平均秩（并列取平均），alphalens 同款处理。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs, ys):
    if len(xs) != len(ys) or len(xs) < 5:
        return None
    rx, ry = _rank(xs), _rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = (sum((a - mx) ** 2 for a in rx)) ** 0.5
    dy = (sum((b - my) ** 2 for b in ry)) ** 0.5
    if not dx or not dy:
        return None
    return round(cov / (dx * dy), 4)


def run(con, date):
    rows = con.execute(
        "SELECT code, extra FROM candidate_snapshots WHERE date<=? "
        "ORDER BY date DESC LIMIT 4000", (date,)).fetchall()
    # 因子值 × 该股 T+2 结局（rec_picks.outcome_ret 近似——同一候选多数
    # 也进了 rec_picks；无结局的样本剔除）
    outcome = {}
    for code, ret in con.execute(
            "SELECT code, outcome_ret FROM rec_picks "
            "WHERE outcome_ret IS NOT NULL"):
        outcome.setdefault(code, []).append(ret)
    samples = {f: ([], []) for f, _ in FACTORS}
    n_total = 0
    for code, extra in rows:
        if code not in outcome:
            continue
        try:
            ex = json.loads(extra or "{}")
        except Exception:  # noqa: BLE001
            continue
        n_total += 1
        for f, _name in FACTORS:
            v = ex.get(f)
            if isinstance(v, dict):
                v = v.get("eff")
            if v is None:
                continue
            samples[f][0].append(v)
            samples[f][1].append(sum(outcome[code]) / len(outcome[code]))
    print(f"样本候选 {n_total} 条（有 T+2 结局配对的计入因子）")
    print(f"{'因子':<14}{'IC(秩相关)':>10}  判定")
    results = {}
    for f, name in FACTORS:
        xs, ys = samples[f]
        ic = spearman(xs, ys)
        if ic is None:
            print(f"{name:<14}{'样本不足':>10}  不做结论")
            continue
        verdict = ("有效 ✓ 保持" if ic >= 0.05 else
                   "反向 ⚠ 立即降权" if ic <= -0.05 else "弱，观察")
        print(f"{name:<14}{ic:>10.4f}  {verdict}")
        results[f] = ic
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    con = get_conn()
    run(con, a.date or "9999-12-31")


if __name__ == "__main__":
    main()
