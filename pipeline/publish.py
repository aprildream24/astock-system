# -*- coding: utf-8 -*-
"""发布层：PBKDF2-HMAC-SHA256 加密数据包 + 静态站构建 + 部署红线体检。

加密格式：file = salt(16) + 密文(XOR HMAC-SHA256 计数器密钥流)。
前端 auth.js 用 WebCrypto.subtle 同算法解密。
明文 data.js 绝不发布（verify_site 红线）。
"""
import hashlib
import hmac
import json
import os
import shutil
import struct

from .core import BASE_DIR, SITE_DIR, DIST_DIR

PBKDF2_ITER = 200000
M38_NOTE = ("M38：PBKDF2 只是密钥派生。本方案补齐：密文完整性校验"
            "（encrypt-then-MAC, HMAC-SHA256）、salt 每次随机（nonce 管理）、"
            "错口令快速失败。已知边界：离线弱口令猜测风险仍在——"
            "口令必须足够强，且泄露后需轮换（重新构建全量密文）。")


def derive_key(password: str, salt: bytes) -> bytes:
    """派生 64 字节：前 32 为加密密钥，后 32 为 MAC 密钥（M38）。"""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                               PBKDF2_ITER, dklen=64)


def _keystream(key: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hmac.new(key, struct.pack(">I", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def encrypt_bytes(plaintext: bytes, password: str) -> bytes:
    """格式 v2：salt(16) + ct + tag(32)——encrypt-then-MAC 认证加密。

    tag = HMAC-SHA256(mac_key, salt+ct)。解密前先验 tag，密文被篡改
    或口令错误都会在完整性校验处失败（不泄露明文信息）。"""
    salt = os.urandom(16)
    key = derive_key(password, salt)
    enc_key, mac_key = key[:32], key[32:]
    ct = bytes(a ^ b for a, b in zip(plaintext, _keystream(enc_key, len(plaintext))))
    tag = hmac.new(mac_key, salt + ct, hashlib.sha256).digest()
    return salt + ct + tag


def decrypt_bytes(blob: bytes, password: str) -> bytes:
    salt, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    key = derive_key(password, salt)
    enc_key, mac_key = key[:32], key[32:]
    want = hmac.new(mac_key, salt + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(want, tag):
        raise ValueError("完整性校验失败：口令错误或密文被篡改")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, len(ct))))


def strip_owner_fields(data: dict, is_owner: bool):
    """非 owner 密文裁剪持仓成本/浮盈（防成本泄露给普通用户）。"""
    if is_owner:
        return data
    d = json.loads(json.dumps(data))
    d.pop("holdings_detail", None)
    for c in d.get("candidates", []):
        c.pop("cost", None)
        c.pop("float_pnl", None)
    return d


def apply_roles(data: dict, user):
    """按用户角色裁剪数据分组（2026-09-15 分用户分级权限）。

    分组口径：
      watch    自选股（data["watch_advice"]）
      observe  观察池（data["candidates"] / skipped 等扫描产出）
      buy      持仓购入（data["holdings_detail"]，含成本浮盈——敏感）
    owner(all) 全见；其余按角色并集。无对应角色的分组直接**从载荷中移除**，
    不是前端隐藏——密文里就没有，杜绝「前端隐藏但数据仍在」的泄露。

    同时写入 data["_access"]：该用户可见分组与角色名，供前端渲染标题栏。"""
    from . import users as users_mod
    is_owner = getattr(user, "is_owner", False) or user == "owner"
    d = json.loads(json.dumps(data))
    roles = list(getattr(user, "roles", [])) if not isinstance(user, str) \
        else users_mod.LEGACY_ROLE_MAP.get(user, ["observe"])
    uid = getattr(user, "uid", user) if not isinstance(user, str) else user
    name = getattr(user, "name", uid) if not isinstance(user, str) else uid

    def _can(group):
        if is_owner:
            return True
        return group in roles

    if not _can("watch"):
        d.pop("watch_advice", None)
        d.pop("watch", None)
    if not _can("observe"):
        d.pop("candidates", None)
        d.pop("ladder_next", None)
        d.pop("skipped", None)
        d.pop("changes", None)
        d.pop("triggers", None)
    if not _can("buy"):
        d.pop("holdings_detail", None)
        d.pop("holdings", None)
    # 成本/浮盈：仅 buy 或 all 可见（observe 角色即使看观察池也不含成本）
    if not _can("buy"):
        for c in d.get("candidates", []) or []:
            c.pop("cost", None)
            c.pop("float_pnl", None)
    d["_access"] = {"uid": uid, "name": name,
                    "roles": roles if not is_owner else ["all"],
                    "is_owner": is_owner,
                    "groups": (["watch", "observe", "buy"] if is_owner
                               else [g for g in ("watch", "observe", "buy")
                                     if g in roles])}
    # 2026-09-15：**网页自助加自选**。用户诉求原话「我能够在网络上单独
    # 添加自选的版本」——不依赖本机开机、不开命令行。
    # 做法（v2，绕开前端加密）：浏览器只发一个 workflow_dispatch，把自选
    # 清单当文本 input 交给 Actions，由 CI 侧 Python PyNaCl 写 Secret。
    # 浏览器零加密依赖（v1 用 libsodium 在客户端做 sealed box，实测 npm 包
    # 只有 CommonJS 形态、wasm 拼接版解码失败，已废弃）。
    # 安全：令牌只存在于 owner 密文内；非 owner 角色此键为 None；
    # 且令牌绝不出现在明文页面/日志里。
    if is_owner:
        tok = os.environ.get("SITE_EDIT_TOKEN", "").strip()
        d["_admin"] = {
            "repo": os.environ.get("SITE_REPO", "aprildream24/astock-system"),
            "workflow": os.environ.get("SITE_WORKFLOW", "stock.yml"),
            "ref": os.environ.get("SITE_REF", "main"),
            "secret": os.environ.get("SITE_WATCH_SECRET", "WATCH_CODES"),
            "token": tok,
            "enabled": bool(tok),
        }
    else:
        d["_admin"] = None
    return d


def encrypt_data(data: dict, passwords: dict, users=None):
    """每个用户一份密文。users 为 User 列表时按角色裁剪；缺省按旧 owner/guest。

    返回 [(uid, blob)]。"""
    out = []
    if users:
        for u in users:
            payload = json.dumps(apply_roles(data, u),
                                 ensure_ascii=False).encode()
            out.append((u.uid, encrypt_bytes(payload, u.password)))
        return out
    for uid, pwd in passwords.items():
        payload = json.dumps(strip_owner_fields(data, uid == "owner"),
                             ensure_ascii=False).encode()
        out.append((uid, encrypt_bytes(payload, pwd)))
    return out


def build_site(data: dict, passwords: dict, users=None):
    """dist/data/* → site/ 静态站。真源是 dist/，site/ 只是打包暂存。

    原地覆盖写入（不整目录删除）：本地 http.server 预览时目录被占用，
    rmtree 会失败；覆盖写对静态站是幂等的——模板里删除过的旧文件由
    clear_stale 处理。

    users（User 列表）存在时按角色一人一份密文，并写入 site/users.json
    （仅含 uid/加密包名/角色名——**不含口令**，口令永不出现在站点）。
    """
    os.makedirs(os.path.join(DIST_DIR, "data"), exist_ok=True)
    for uid, blob in encrypt_data(data, passwords, users):
        with open(os.path.join(DIST_DIR, "data", f"{uid}.bin"), "wb") as f:
            f.write(blob)
    os.makedirs(SITE_DIR, exist_ok=True)
    # 清理上一次构建的旧数据包（覆盖语义下唯一可能残留的文件类）
    data_dir = os.path.join(SITE_DIR, "data")
    if os.path.isdir(data_dir):
        for n in os.listdir(data_dir):
            try:
                os.remove(os.path.join(data_dir, n))
            except OSError:
                pass
    for name in os.listdir(SITE_DIR_SRC):
        src = os.path.join(SITE_DIR_SRC, name)
        dst = os.path.join(SITE_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, dst)
        else:
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
    os.makedirs(os.path.join(SITE_DIR, "data"), exist_ok=True)
    for uid, blob in encrypt_data(data, passwords, users):
        with open(os.path.join(SITE_DIR, "data", f"{uid}.bin"), "wb") as f:
            f.write(blob)
    # 用户索引（无口令）：前端据此列出可登录身份 + 各自可见分组说明
    if users:
        index = [{"id": u.uid, "name": u.name,
                  "groups": u.visible_groups(),
                  "is_owner": u.is_owner} for u in users]
        with open(os.path.join(SITE_DIR, "users.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"users": index}, f, ensure_ascii=False, indent=1)


SITE_DIR_SRC = os.path.join(BASE_DIR, "site_template")


def verify_site(site_dir=SITE_DIR):
    """14 项体检核心项：明文 data.js 存在 → 部署红线终止。"""
    issues = []
    if os.path.exists(os.path.join(site_dir, "data.js")):
        issues.append("红线：site/ 存在明文 data.js")
    data_dir = os.path.join(site_dir, "data")
    if not os.path.isdir(data_dir) or not any(
            n.endswith(".bin") for n in os.listdir(data_dir)):
        issues.append("缺少加密数据包 data/*.bin")
    for need in ("index.html", "auth.js"):
        if not os.path.exists(os.path.join(site_dir, need)):
            issues.append(f"缺少 {need}")
    # 全部 .bin 用错口令必须解密失败（抽样 owner）
    bins = [n for n in os.listdir(data_dir) if n.endswith(".bin")] \
        if os.path.isdir(data_dir) else []
    for name in bins[:1]:
        with open(os.path.join(data_dir, name), "rb") as f:
            blob = f.read()
        if _try_decrypt(blob, "definitely-wrong-password"):
            issues.append(f"{name} 可被错口令解密")
    return issues


def _try_decrypt(blob, password):
    try:
        json.loads(decrypt_bytes(blob, password).decode())
        return True
    except Exception:  # noqa: BLE001
        return False
