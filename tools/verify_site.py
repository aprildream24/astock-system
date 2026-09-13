# -*- coding: utf-8 -*-
"""线上站点体检（吸收自原项目 verify_site.py，适配 M38 认证加密格式）。

用法：python tools/verify_site.py <站点URL> [--local]
本地模式省略 URL → 直接对 site/ 目录体检（部署前红线）。

检查项：
  ① 必须存在：index.html / auth.js / data/*.bin
  ② 必须不存在（存在即泄露）：data.js / data.js.bak / push_log / 明文配置
  ③ SPA 回退壳识别：CF Pages 对不存在路径回退 index.html（200+HTML），
     以「200 + 非 HTML 内容」判定真文件，避免把回退壳误报为泄露
  ④ 密文头不像明文 JSON
  ⑤ 解密自检：owner 口令真实解密一份 + 错口令必须失败（无口令则告警跳过）
"""
import hashlib
import hmac
import json
import os
import struct
import sys
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_DIR = os.path.join(ROOT, "site")
PBKDF2_ITER = 200000

MUST_EXIST = ["index.html", "auth.js"]
MUST_MISS = ["data.js", "data.js.bak", "push_log.jsonl",
             "config/notify.json", "config/users.json"]


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "site-check"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()


def is_real(st, body):
    """真文件判定：非 text/html（SPA 回退壳是 text/html 的 index.html）。"""
    if st != 200:
        return False
    head = body[:400].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return False
    return True


def decrypt_bin(blob, passwd):
    """与 publish.py M38 同算法：salt(16)+ct+tag(32)。"""
    from pipeline.publish import decrypt_bytes
    return decrypt_bytes(blob, passwd)


def main():
    base = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("http"):
        base = sys.argv[1].rstrip("/")
    bad = 0
    print("== 必须存在 ==")
    for f in MUST_EXIST:
        if base:
            st, body = fetch(base + "/" + f)
            ok = (st == 200 and len(body) > 0) if f == "index.html" \
                else is_real(st, body)
        else:
            p = os.path.join(SITE_DIR, f)
            ok = os.path.exists(p)
            body = open(p, "rb").read() if ok else b""
        bad += 0 if ok else 1
        print("  %s %-12s %s" % ("OK" if ok else "MISS", f,
                                 "%d bytes" % len(body) if ok else ""))
    print("== 必须不存在（存在即泄露） ==")
    for f in MUST_MISS:
        if base:
            st, body = fetch(base + "/" + f)
            leaked = is_real(st, body)
        else:
            leaked = os.path.exists(os.path.join(SITE_DIR, f))
        bad += 1 if leaked else 0
        print("  %s %-22s" % ("LEAK!" if leaked else "clean", f))
    print("== 加密数据 ==")
    data_dir = os.path.join(SITE_DIR if not base else SITE_DIR, "data")
    if base:
        st, body = fetch(base + "/data/owner.bin")
        ok = st == 200 and len(body) > 48
        print("  %s data/owner.bin HTTP %s %.0f KB"
              % ("OK" if ok else "MISS", st, len(body) / 1024))
        bad += 0 if ok else 1
        if ok and (body[:200].lstrip()[:1] in (b"{", b"[")
                   or b"__STOCK_DATA__" in body[:200]):
            print("  LEAK: 密文开头像明文 JSON，加密可能没生效！")
            bad += 1
    else:
        if not os.path.isdir(data_dir) or not any(
                n.endswith(".bin") for n in os.listdir(data_dir)):
            print("  MISS data/*.bin")
            bad += 1
        else:
            print("  OK data/*.bin 存在")
    print("== 解密自检（需 OWNER_PASS 环境变量） ==")
    passwd = os.environ.get("OWNER_PASS", "")
    blob = None
    if base:
        st, blob = fetch(base + "/data/owner.bin")
    else:
        p = os.path.join(data_dir, "owner.bin")
        blob = open(p, "rb").read() if os.path.exists(p) else None
    if not blob:
        print("  skip: 无密文可验证")
    elif not passwd:
        print("  WARN: 未提供 OWNER_PASS，跳过解密自检（CI 无口令环境可跑）")
    else:
        try:
            obj = json.loads(decrypt_bin(blob, passwd).decode("utf-8"))
            print("  OK owner 口令解密成功 → JSON（date=%s）"
                  % (obj.get("date") or "?"))
            try:
                json.loads(decrypt_bin(blob, passwd + "x").decode("utf-8"))
                print("  FAIL: 错误口令竟然也能解出 JSON！门禁失效。")
                bad += 1
            except Exception:
                print("  OK 错误口令无法解密（符合预期）")
        except Exception as e:  # noqa: BLE001
            print("  FAIL: 口令解密失败（口令错或算法不匹配）：%s" % e)
            bad += 1
    print("\n%s" % ("全部通过" if bad == 0 else "有 %d 项不通过" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
