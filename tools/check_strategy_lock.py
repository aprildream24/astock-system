# -*- coding: utf-8 -*-
"""技巧注册表基线守门：数量只许涨不许跌（规格书 6-3 红线）。

用法：
  python tools/check_strategy_lock.py            # 校验（<基线 → exit 2）
  python tools/check_strategy_lock.py --update   # 新增技巧后提升基线（只增）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.techniques import count, active  # noqa: E402

BASELINE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "baseline_techniques.json")


def main():
    baseline = 0
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE, encoding="utf-8") as f:
            baseline = json.load(f).get("count", 0)
    n, na = count(), len(active())
    if "--update" in sys.argv:
        if n < baseline:
            print(f"拒绝下调基线：当前 {n} < 基线 {baseline}（下线技巧需实证+用户同意）")
            sys.exit(2)
        with open(BASELINE_FILE, "w", encoding="utf-8") as f:
            json.dump({"count": n}, f)
        print(f"基线已提升: {baseline} → {n}")
        return
    if n < baseline:
        print(f"FAIL 技巧只增不减红线：注册表 {n} < 基线 {baseline}")
        sys.exit(2)
    print(f"OK 注册表 {n} 个（active {na} / embedded+planned {n - na}）"
          f" ≥ 基线 {baseline}")
    if n > baseline:
        print(f"提示：比基线多 {n - baseline} 个，可 --update 提升基线")


if __name__ == "__main__":
    main()
