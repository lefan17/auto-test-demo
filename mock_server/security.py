"""鉴权与角色权限。

为什么不用 JWT 库：这里只需要一个"能被服务端验证、客户端改不了"的令牌。
用标准库 hmac + base64 手写一份，依赖更少，而且令牌怎么签的、怎么验的一眼可见——
测试项目里"能讲清楚"比"用了流行库"更重要。

令牌格式：  base64url(payload_json).base64url(hmac_sha256(secret, payload))
payload:    {"uid": 1, "username": "admin", "role": "ADMIN", "exp": 1767225600}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from mock_server.errors import PermissionDenied

# 密钥从环境变量取，缺省用固定值（Mock 服务，方便直接跑）。生产绝不允许有默认值。
SECRET = os.getenv("MOCK_SECRET", "dsh-mes-mock-secret-2024").encode("utf-8")
TOKEN_TTL_SECONDS = int(os.getenv("MOCK_TOKEN_TTL", "3600"))

PBKDF2_ROUNDS = 120_000

ROLES = ("ADMIN", "PLANNER", "OPERATOR", "QA")

# 权限矩阵：动作 -> 允许的角色。
# 把权限写成数据而不是一串 if，是为了"改权限只改这张表"，
# 也让用例可以对着这张表逐条覆盖（权限漏判是最典型的生产事故）。
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "material:create": ("ADMIN", "PLANNER"),
    "material:update": ("ADMIN", "PLANNER"),
    "material:delete": ("ADMIN",),
    "work_order:create": ("ADMIN", "PLANNER"),
    "work_order:read": ("ADMIN", "PLANNER", "OPERATOR", "QA"),
    "work_order:approve": ("ADMIN", "PLANNER"),
    "work_order:schedule": ("ADMIN", "PLANNER"),
    "work_order:cancel": ("ADMIN", "PLANNER"),
    "work_order:ship": ("ADMIN",),
    "batch:create": ("ADMIN", "PLANNER"),
    "batch:start": ("ADMIN", "OPERATOR"),
    "production:report": ("ADMIN", "OPERATOR"),
    "production:stock_in": ("ADMIN", "OPERATOR"),
    "inventory:issue": ("ADMIN", "OPERATOR"),
    "inventory:reverse": ("ADMIN", "OPERATOR"),
    "inspection:create": ("ADMIN", "QA"),
}


# ---------- 口令 ----------
def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, expected = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds)).hex()
    # compare_digest 而不是 ==：避免按字符提前返回造成的时序侧信道
    return hmac.compare_digest(digest, expected)


# ---------- 令牌 ----------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def create_token(user_id: int, username: str, role: str, ttl_seconds: int | None = None) -> tuple[str, int]:
    ttl = ttl_seconds or TOKEN_TTL_SECONDS
    payload = {
        "uid": user_id,
        "username": username,
        "role": role,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl,
        "jti": secrets.token_hex(8),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64e(hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", ttl


def decode_token(token: str) -> dict:
    """验签 + 验过期。失败返回 {}，由调用方转成 401。"""
    try:
        body, sig = token.split(".")
    except ValueError:
        return {}
    expected = _b64e(hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return {}
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return {}
    if int(payload.get("exp", 0)) < int(time.time()):
        return {}
    return payload


# ---------- 权限 ----------
def ensure_permission(role: str, action: str) -> None:
    allowed = PERMISSIONS.get(action)
    if allowed is None:
        raise RuntimeError(f"未定义的权限动作: {action}")
    if role not in allowed:
        raise PermissionDenied(
            f"角色 {role} 无权限执行 {action}",
            action=action,
            role=role,
            allowed=list(allowed),
        )


def expires_at(ttl: int) -> str:
    from mock_server.util import now_iso

    return now_iso()
