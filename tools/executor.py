# -*- coding: utf-8 -*-
"""模拟盘入口（薄壳）：核心实现位于 pipeline/executor.py（RiskGate/批次T+1/分账）。

用法：python -m tools.executor --task auto|now|scan|tail|review

task 语义（2026-09-18）：
  · auto —— 自动建仓 + 巡逻（**默认**，模拟盘"自动运行"的形态）
  · now/scan/tail —— 只巡逻，不买入（保持原语义）
保留 sell_decision 兼容旧测试的 T+1 语义（buy_date == today → HOLD）。
"""
import argparse

from pipeline import executor


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


def run(task="auto"):
    return executor.run(task)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="auto")
    a = ap.parse_args()
    for row in run(a.task):
        print(row)
