# -*- coding: utf-8 -*-
"""用户与权限模型（2026-09-15 新增）。

背景：原 config/users.json 是扁平字典 {uid: 口令}，只有 owner/guest 之分，
无法表达「自选/观察/购入 各自给特定人看」。用户需求：自己增删标的，且
不同接收人只能看到被授权的分组。

设计：向后兼容的**双形态** users.json
  旧形态（仍支持）：{"owner": "口令", "guest": "口令"}
    → owner = 全角色；guest = 仅 observe
  新形态：{"users": [{"id": "...", "name": "...", "pass": "...",
                      "roles": ["watch", "observe"]}]}
    → roles ∈ {all, watch, observe, buy}

角色语义（决定可见分组）：
  all     → 全部（含持仓成本/浮盈等敏感字段），等价旧 owner
  watch   → 自选股分组
  observe → 观察池分组（原 guest 语义）
  buy     → 持仓/购入分组（含成本与浮盈）
一人可多角色，取并集。
"""
import json
import os

ROLES = ("all", "watch", "observe", "buy")
ROLE_LABELS = {
    "all": "全部权限",
    "watch": "自选股",
    "observe": "观察池",
    "buy": "持仓购入",
}
# 旧扁平形态的口令 → 角色（向后兼容）
LEGACY_ROLE_MAP = {
    "owner": ["all"],
    "guest": ["observe"],
}


class User:
    __slots__ = ("uid", "name", "password", "roles")

    def __init__(self, uid, password, roles, name=None):
        self.uid = uid
        self.name = name or uid
        self.password = password
        self.roles = [r for r in (roles or []) if r in ROLES]

    @property
    def is_owner(self):
        return "all" in self.roles

    def can(self, group):
        """能否看 group：all 通吃；否则看显式角色。"""
        if self.is_owner:
            return True
        if group == "watch":
            return "watch" in self.roles
        if group == "observe":
            return "observe" in self.roles
        if group in ("buy", "holdings"):
            return "buy" in self.roles
        return False

    def visible_groups(self):
        g = [x for x in ("watch", "observe", "buy") if self.can(x)]
        return g

    def to_dict(self):
        return {"id": self.uid, "name": self.name, "roles": list(self.roles)}


def parse_users(raw):
    """把 users.json 原始 dict 解析成 [User]。兼容旧扁平形态与注释键。"""
    users = []
    if not isinstance(raw, dict):
        return users
    if isinstance(raw.get("users"), list):
        for it in raw["users"]:
            if not isinstance(it, dict):
                continue
            uid = str(it.get("id") or "").strip()
            pwd = it.get("pass") or it.get("password")
            if not uid or not pwd:
                continue
            users.append(User(uid, str(pwd), it.get("roles"),
                              name=it.get("name")))
        return users
    # 旧扁平形态：跳过注释键（以 _ 开头）与非字符串值
    for uid, pwd in raw.items():
        if uid.startswith("_") or not isinstance(pwd, str) or not pwd:
            continue
        roles = LEGACY_ROLE_MAP.get(uid, ["observe"])
        users.append(User(uid, pwd, roles,
                          name="管理员" if uid == "owner" else None))
    return users


def load_users(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return parse_users(json.load(f))


def passwords_of(users):
    """{uid: 口令} —— 供 encrypt_data 使用（每人一份密文）。"""
    return {u.uid: u.password for u in users}


def users_by_id(users):
    return {u.uid: u for u in users}
