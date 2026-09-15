# -*- coding: utf-8 -*-
"""清理 GitHub Actions 的旧行情库缓存（只保留最新 1 份）。

## 为什么需要

`actions/cache/save@v4` **不允许覆盖已存在的 key**。所以：
  - **固定 key**（如 `market-db-v2`）→ 第一次存进去的那份**永久生效**，
    之后再也不更新。实测首次存的是 0 字节空条目 ⇒ 每个 run 都「库最新 空」
    ⇒ 4937 只全量重拉 ≈17 分钟（血案：run 35002284192）。
  - **key 带 run_id** → 每次都能存新的，但**每次都新增一份**，实测累积
    15 份 ≈ 469 MB，最终撑爆仓库 10 GB 缓存上限。

⇒ 正解：key 带 run_id（保证持续更新）+ **本模块每轮删旧**（保证不膨胀）。

## 策略

只清理 `market-db-` 前缀、且 **key 不等于本次 run 那份** 的条目。
即：永远是「上轮那份（供本轮 restore 回退）」被删，「本轮那份」留下。
长期稳定在 1 份。

## 环境变量

  GH_PAT   GitHub PAT（需 actions:write 权限才能删缓存）
  GH_REPO  owner/repo
  GITHUB_RUN_ID  本次 run id（由 Actions 自动注入）

清理失败（缺 token / 权限不足 / 网络抖动）一律**只告警不失败** ——
缓存清理是运维优化，绝不能连坐主链推送。
"""
import json
import os
import sys
import urllib.error
import urllib.request

PREFIX = "market-db-"
API = "https://api.github.com"


def _gh(method, path, tok, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "astock-cache-gc",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw.strip() else {})


def gc(keep_run_id=None):
    tok = os.environ.get("GH_PAT", "").strip()
    repo = os.environ.get("GH_REPO", "").strip()
    if not tok or not repo:
        print("[cache-gc] 缺 GH_PAT / GH_REPO，跳过（不影响主链）")
        return 0
    keep_run_id = keep_run_id or os.environ.get("GITHUB_RUN_ID", "").strip()
    keep_key = f"{PREFIX}{keep_run_id}" if keep_run_id else None

    try:
        st, d = _gh("GET", f"/repos/{repo}/actions/caches?per_page=100", tok)
    except urllib.error.HTTPError as e:
        print(f"[cache-gc] 列缓存失败 HTTP {e.code}（继续，不阻塞主链）")
        return 0
    except Exception as e:
        print(f"[cache-gc] 列缓存异常 {e}（继续，不阻塞主链）")
        return 0

    caches = d.get("actions_caches", []) if st == 200 else []
    targets = [c for c in caches
               if c.get("key", "").startswith(PREFIX)
               and c.get("key") != keep_key]
    print(f"[cache-gc] 命中 {len(caches)} 份缓存，"
          f"其中待清理 {len(targets)} 份"
          f"（保留 {keep_key or '最新一份'}）")
    if not targets:
        print("[cache-gc] 无需清理")
        return 0

    freed = 0
    ok = fail = 0
    for c in targets:
        try:
            _gh("DELETE",
                f"/repos/{repo}/actions/caches/{c['id']}", tok)
            freed += c.get("size_in_bytes", 0)
            ok += 1
            print(f"[cache-gc]  已删 {c['key']} "
                  f"({c.get('size_in_bytes', 0) / 1048576:.1f} MB)")
        except urllib.error.HTTPError as e:
            fail += 1
            print(f"[cache-gc]  删除 {c['key']} 失败 HTTP {e.code}"
                  f"（需 actions:write 权限）")
        except Exception as e:
            fail += 1
            print(f"[cache-gc]  删除 {c['key']} 异常 {e}")
    print(f"[cache-gc] 完成：删 {ok} 份、失败 {fail} 份，"
          f"释放 {freed / 1048576:.1f} MB")
    return 0          # 恒 0：清理是优化，绝不连坐主链


def main():
    return gc()


if __name__ == "__main__":
    sys.exit(main())
