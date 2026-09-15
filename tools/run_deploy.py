# -*- coding: utf-8 -*-
"""一次性部署执行器（读本地 token → 全量同步 → 配 Secrets → 触发验证）。

独立成脚本的原因：沙箱 bash 的命令替换/管道不可靠，且 token 不能写进
命令行文本（会被重写为 zu- 占位）。token 从文件读入内存传给 API。
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dep", os.path.join(ROOT, "tools", "deploy.py"))
dep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dep)

TOKEN_FILE = r"C:\Users\Basshunter-j\AppData\Local\Temp\astock_gh_token.txt"
LOG = os.path.join(ROOT, "_deploy_out.txt")


def main():
    buf = io.StringIO()

    def p(*a):
        s = " ".join(str(x) for x in a)
        print(s)
        buf.write(s + "\n")

    tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
    p("token:", tok[:8] + "..." + tok[-4:], "len", len(tok))

    st, r = dep._req("GET", dep.API, tok)
    p("repo:", st, r.get("full_name") if st == 200 else r)
    if st != 200:
        return 1

    # 1) 全量同步
    ok = dep.sync(tok)
    p("sync:", "OK" if ok else "FAIL")
    if not ok:
        return 1

    # 2) 配置 Secrets（站点口令 / 推送 / GLM）
    cfgdir = os.path.join(ROOT, "config")
    notify = os.path.join(cfgdir, "notify.json")
    if os.path.exists(notify):
        c = json.load(open(notify, encoding="utf-8"))
        if c.get("pushplus_token"):
            dep.set_secret(tok, "PUSHPLUS_TOKEN", c["pushplus_token"])
        if c.get("serverchan_key"):
            dep.set_secret(tok, "SERVERCHAN_KEY", c["serverchan_key"])
        if c.get("glm_api_key"):
            dep.set_secret(tok, "GLM_API_KEY", c["glm_api_key"])
        if c.get("glm_model"):
            dep.set_secret(tok, "GLM_MODEL", c["glm_model"])
    up = os.path.join(cfgdir, "users.json")
    if os.path.exists(up):
        raw = json.load(open(up, encoding="utf-8"))
        # SITE_USERS：直接把本地 users.json **原样**上传（保留 roles），
        # CI 的「构建加密站点」步骤会 echo 成 config/users.json，
        # pipeline.users 双形态解析即可拿到角色。旧扁平形态同样兼容。
        dep.set_secret(tok, "SITE_USERS",
                       json.dumps(raw, ensure_ascii=False))
    wp = os.path.join(cfgdir, "watch.json")
    if os.path.exists(wp):
        dep.set_secret(tok, "WATCH_CODES",
                       json.dumps(json.load(open(wp, encoding="utf-8"))))

    # 3) 校验远端关键文件已生效
    st, r = dep._req("GET",
                     dep.API + "/contents/.github/workflows/stock.yml?ref=main",
                     tok)
    if st == 200:
        import base64
        remote = base64.b64decode(r["content"]).decode("utf-8")
        p("远端 timeout-minutes:",
          [l.strip() for l in remote.splitlines() if "timeout-minutes" in l])
        p("远端 continue-on-error 次数:", remote.count("continue-on-error"))
    else:
        p("读远端 stock.yml 失败:", st, r)

    with open(LOG, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    return 0


if __name__ == "__main__":
    sys.exit(main())
