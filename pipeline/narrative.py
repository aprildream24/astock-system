# -*- coding: utf-8 -*-
"""AI 叙事降级链（可选模块）：主力(CF Workers AI) → GLM → Kimi → 规则引擎兜底。

错误处理纪律（规格书 九）：
- 402 → 重试一次；RPM 429 → 秒级退避重试；配额 429 → 立即换家；
- 400 invalid temperature → 降 temp 重发；
- Kimi: temp=1、输出 ≤1600 字、RPM=3。
密钥全部走环境变量（源码零密钥）：
  CF_AI_TOKEN / CF_ACCOUNT_ID / GLM_API_KEY / KIMI_API_KEY
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"


def _providers():
    return [
        {"name": "cf", "enabled": bool(os.environ.get("CF_AI_TOKEN")),
         "url": (f"https://api.cloudflare.com/client/v4/accounts/"
                 f"{os.environ.get('CF_ACCOUNT_ID', '')}/ai/run/"
                 f"@cf/meta/llama-3.1-8b-instruct"),
         "headers": {"Authorization": f"Bearer {os.environ.get('CF_AI_TOKEN', '')}",
                     "Content-Type": "application/json"},
         "payload": None, "temp": 0.8, "rpm": 0},
        {"name": "glm", "enabled": bool(os.environ.get("GLM_API_KEY")),
         "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
         "headers": {"Authorization": f"Bearer {os.environ.get('GLM_API_KEY', '')}",
                     "Content-Type": "application/json"},
         "model": "glm-4.6", "temp": 0.8, "rpm": 0},
        {"name": "kimi", "enabled": bool(os.environ.get("KIMI_API_KEY")),
         "url": "https://api.moonshot.cn/v1/chat/completions",
         "headers": {"Authorization": f"Bearer {os.environ.get('KIMI_API_KEY', '')}",
                     "Content-Type": "application/json"},
         "model": "moonshot-v1-8k", "temp": 1.0, "rpm": 3},
    ]


class QuotaExhausted(Exception):
    """配额 429 → 立即换家。"""


def _call(p, prompt, temperature, http_fn=None, max_chars=1600):
    """单家调用；按错误纪律分类抛出。"""
    if p["name"] == "cf":
        body = {"prompt": prompt, "max_tokens": max_chars // 2,
                "temperature": temperature}
    else:
        body = {"model": p["model"], "temperature": temperature,
                "max_tokens": max_chars // 2,
                "messages": [{"role": "user", "content": prompt}]}
    data = json.dumps(body).encode()

    def _post():
        req = urllib.request.Request(p["url"], data=data,
                                     headers={**p["headers"], "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    post = http_fn or _post
    try:
        out = post()
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode(errors="replace") if e.fp else ""
        except Exception:  # noqa: BLE001
            err_body = ""
        err_body = f"{err_body} {e}"          # fp 缺失时至少可用错误消息匹配
        if e.code == 402:
            time.sleep(1.5)                       # 402 → 重试一次
            out = post()
        elif e.code == 429:
            if re.search(r"quota|balance|arrears|欠费|配额", err_body, re.I):
                raise QuotaExhausted(p["name"])   # 配额 429 → 换家
            time.sleep(2.0)                       # RPM 429 → 秒级退避
            out = post()
        elif e.code == 400 and "temperature" in err_body.lower():
            body["temperature"] = 0.5             # 400 invalid temperature → 降档
            data = json.dumps(body).encode()
            out = post()
        else:
            raise
    if p["name"] == "cf":
        return str(out.get("result", "")).strip()
    return out["choices"][0]["message"]["content"].strip()[:max_chars]


def rule_engine(stats):
    """规则引擎兜底：零外部依赖的确定性叙事。"""
    mood = stats.get("mood") or {}
    zt, ms = mood.get("zt_count", 0), mood.get("max_streak", 0)
    pr = mood.get("promote_rate", 0) * 100
    zb = mood.get("zhaban_rate", 0) * 100
    em = mood.get("emotion", 50)
    tone = "偏暖" if em >= 60 else ("偏冷" if em <= 40 else "中性")
    picks = stats.get("picks", [])
    lines = [f"【{stats.get('date', '')} 盘后速览】市场情绪{tone}（{em:.0f}分）："
             f"涨停{zt}家，最高{ms}板，晋级率{pr:.0f}%，炸板率{zb:.0f}%。"]
    if zb >= 40:
        lines.append("封板不稳，接力环境差，明日慎追高位板。")
    elif pr >= 55:
        lines.append("晋级率良好，接力环境尚可，重点关注强高开确认。")
    if picks:
        names = "、".join(f"{p.get('name','')}{p['code']}({p['pool']})"
                          for p in picks[:3])
        lines.append(f"今日重点：{names}；按纪律执行买区/止损，不追高。")
    else:
        lines.append("今日无达标推荐——没有机会就不凑数，空仓也是仓位。")
    return "\n".join(lines)


def narrate(stats, http_fn=None):
    """走降级链；全部失败或未配置 → 规则引擎兜底（永不失败）。"""
    prompt = (
        "你是A股复盘助手。根据以下JSON数据写一段≤300字盘后复盘，"
        "客观、给操作纪律提示、不荐股不夸大：\n"
        + json.dumps(stats, ensure_ascii=False, default=str))
    for p in _providers():
        if not p["enabled"]:
            continue
        for attempt in range(2):
            try:
                text = _call(p, prompt, p["temp"], http_fn=http_fn)
                if text:
                    return text
            except QuotaExhausted:
                break                              # 换家
            except Exception:  # noqa: BLE001
                if attempt:
                    break
    return rule_engine(stats)
