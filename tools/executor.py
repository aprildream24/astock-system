# -*- coding: utf-8 -*-
"""模拟盘入口（薄壳）：核心实现位于 pipeline/executor.py（RiskGate/批次T+1/分账）。

用法：
  python -m tools.executor --task auto            # 默认：自动建仓 + 巡逻
  python -m tools.executor --task scan            # 只巡逻，不买入
  python -m tools.executor --task auto --slot pm  # 尾盘：用 snapshot_live 的当下价
  python -m tools.executor --task auto --dry      # ★ 只渲染不推送（测试用）

task 语义（2026-09-18）：
  · auto —— 自动建仓 + 巡逻（**默认**，模拟盘"自动运行"的形态）
  · now/scan/tail —— 只巡逻，不买入（保持原语义）

★ `--dry` 是「测试所有功能无误后再推送」这条用户要求的落地口子：
  它跑完整链路（闸门/风控/撮合/渲染），只在最后一步把推送换成落盘 HTML，
  因此可以在**不打扰用户**的前提下核对版面与判决。
  预览产物写到 dist/reports/exec_preview.html（dist/ 不入库，不会上公网）。

⚠️ `--dry` 仍会**真实写本地库**（撮合是不可逆的）。要纯粹看版面就别连生产库；
  本机 cache/market.db 与 CI 的行情库是两份，互不影响。
保留 sell_decision 兼容旧测试的 T+1 语义（buy_date == today → HOLD）。
"""
import argparse
import os

from pipeline import executor, notifier
from pipeline.core import BASE_DIR

PREVIEW = os.path.join(BASE_DIR, "dist", "reports", "exec_preview.html")


def sell_decision(h, price, today, env_score=0):
    """兼容接口：T+1 硬约束 —— 当日买入批次任何通道不许卖。

    按批次语义（M26）：这里按整票口径判断（buy_date < today 才可能卖）。
    """
    if h["buy_date"] == today:
        return "HOLD", "T+1 当日买入不许卖"
    ret = (price / h["buy_price"] - 1) * 100
    if price <= h["stop"]:
        return "SELL", f"触发止损 {price:.2f}≤{h['stop']:.2f}"
    if ret <= -8:
        return "SELL", "深度回撤保护"
    if ret >= 15:
        return "SELL", "目标止盈"
    if env_score < executor.RISK["daily_loss_halt"]:
        return "HOLD", "环境冰点观望"
    return "HOLD", ""


def run(task="auto", slot=None, dry=False):
    """跑一轮模拟盘。dry=True 时**不推送**，把卡片落到 dist/reports/。"""
    if not dry:
        return executor.run(task, slot=slot)
    origin = notifier.push

    def _capture(mode, title, content, **kw):
        # 用真实标题前缀，预览里能直接核对【模拟】【Astra】形态
        cfg = notifier.load_config()
        full = notifier.title_prefix(mode, cfg.get("push_tag") or "Astra",
                                     "PushPlus") + title
        os.makedirs(os.path.dirname(PREVIEW), exist_ok=True)
        with open(PREVIEW, "w", encoding="utf-8") as f:
            f.write("<!doctype html><meta charset='utf-8'>"
                    f"<div style='background:#15181e;padding:14px;"
                    f"font-family:sans-serif'><div style='color:#f1f3f4;"
                    f"font-size:15px;font-weight:700;margin-bottom:10px'>"
                    f"[dry-run] {full}</div>{content}</div>")
        print(f"[executor] dry-run 标题：{full}")
        print(f"[executor] dry-run 正文：{notifier.html_to_text(content)}")
        print(f"[executor] dry-run 已落盘 {PREVIEW}")
        return {"status": "dry-run", "detail": "no-push", "mode": mode}

    try:
        notifier.push = _capture
        return executor.run(task, slot=slot)
    finally:
        notifier.push = origin


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="auto")
    ap.add_argument("--slot", default=None,
                    help="盘中任务槽位：am=早盘 / pm=尾盘（取 snapshot_live 当下价）")
    ap.add_argument("--dry", action="store_true",
                    help="只渲染不推送（测试版面用）")
    a = ap.parse_args()
    for row in run(a.task, slot=a.slot, dry=a.dry):
        print(row)
