# -*- coding: utf-8 -*-
"""智谱免费模型真验脚本（本地运行，需要真实 key）。

用法：
  set GLM_API_KEY=你的key
  python -X utf8 tools/test_glm.py
或：
  python -X utf8 tools/test_glm.py 你的key

验证内容：
  1) provider 配置正确（默认 glm-4.7-flash 免费档）
  2) 端到端 narrate() 走 GLM 链路并产出非空叙事
  3) 原始 API 调用返回 usage（确认真的是智谱在响应）
key 获取：https://bigmodel.cn → 控制台 → API Keys。glm-4.7-flash 免费，不扣费。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import narrative  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GLM_API_KEY", "")
    if not key:
        print("FAIL 未提供 key：set GLM_API_KEY=xxx 或作为第一个参数传入")
        return 1

    # ① 配置检查
    provs = {p["name"]: p for p in narrative._providers()}
    glm = provs["glm"]
    print(f"[1] provider 配置: model={glm['model']} url={glm['url']}")
    assert glm["model"], "模型名不能为空"

    # ② 原始调用（能看到 usage / token 消耗）
    body = {"model": glm["model"], "messages": [
        {"role": "user", "content": "只回复两个字：收到"}],
        "temperature": 0.1, "max_tokens": 32,
        "thinking": {"type": "disabled"}}
    req = urllib.request.Request(
        glm["url"], data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json", "User-Agent": UA})
    out = None
    for attempt in range(4):                     # 免费档瞬时限流 429 → 自动退避
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                print(f"    429 限流，25s 后重试（{attempt + 1}/3）")
                time.sleep(25)
                continue
            print(f"FAIL 原始调用异常: HTTP {e.code}: {e}")
            return 1
        except (TimeoutError, urllib.error.URLError) as e:
            if attempt < 3:
                print(f"    超时/网络抖动，15s 后重试（{attempt + 1}/3）："
                      f"{type(e).__name__}")
                time.sleep(15)
                continue
            print(f"FAIL 原始调用异常: {type(e).__name__}: {e}")
            return 1
    content = out["choices"][0]["message"]["content"].strip()
    usage = out.get("usage", {})
    print(f"[2] 原始调用 OK: 回复={content!r} "
          f"tokens={usage.get('total_tokens', '?')} "
          f"model={out.get('model', glm['model'])})")
    # req 的 data 不可复用（流已被消费），重建供后续调用

    # ③ 端到端 narrate()（与收盘复盘推送同一条链路）
    os.environ["GLM_API_KEY"] = key
    os.environ.pop("CF_AI_TOKEN", None)
    os.environ.pop("KIMI_API_KEY", None)
    stats = {"date": "2026-09-14", "mood": {"zt_count": 52, "max_streak": 5,
                                            "promote_rate": 0.51,
                                            "zhaban_rate": 0.28,
                                            "emotion": 58},
             "picks": [{"code": "sh600100", "name": "示例票", "pool": "趋势"}]}
    text = narrative.narrate(stats)
    print(f"[3] narrate() 产出 {len(text)} 字：")
    print(text)
    if len(text) < 20 or "盘后速览" in text and text == narrative.rule_engine(stats):
        print("WARN 结果疑似规则引擎兜底（GLM 链路可能未生效）")
    else:
        print("PASS GLM 链路生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
