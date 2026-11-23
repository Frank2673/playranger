"""账号与会话 —— PBKDF2-HMAC-SHA256 + 随机 Token。

安全取舍：
- 密码只存 PBKDF2 派生值 + 随机盐，不存明文；
- Token 只存 SHA-256 摘要，数据库泄露也无法直接复用；
- 首个注册用户自动成为管理员（比赛 Demo 要求）。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from playranger.platform import db

PBKDF2_ITERATIONS = 200_000
TOKEN_TTL_DAYS = 7
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")


class AuthError(Exception):
    """鉴权/注册失败，status 供 API 映射 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PBKDF2_ITERATIONS,
    )
    return digest.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    digest, _ = hash_password(password, salt)
    return hmac.compare_digest(digest, expected_hash)


def public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user.get("role", "member"),
        "created_at": user.get("created_at"),
    }


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).isoformat(
        timespec="seconds"
    )
    db.create_session(_token_hash(token), user_id, expires)
    return token


def register(username: str, password: str) -> tuple[dict, str]:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AuthError("用户名需为 3-32 位字母、数字、下划线或短横线", 400)
    if len(password or "") < 8:
        raise AuthError("密码至少 8 位", 400)
    if db.get_user_by_username(username) is not None:
        raise AuthError("用户名已存在", 409)

    password_hash, salt = hash_password(password)
    try:
        user = db.create_user(username, password_hash, salt)
    except Exception as exc:  # 并发注册同名时由唯一索引兜底
        if "UNIQUE" in str(exc).upper():
            raise AuthError("用户名已存在", 409) from exc
        raise
    token = _issue_token(user["id"])
    return user, token


def login(username: str, password: str) -> tuple[dict, str]:
    user = db.get_user_by_username((username or "").strip())
    if user is None or not verify_password(password or "", user["salt"], user["password_hash"]):
        raise AuthError("用户名或密码错误", 401)
    return public_user(user), _issue_token(user["id"])


def authenticate(token: str | None) -> dict | None:
    if not token:
        return None
    return db.get_session_user(_token_hash(token))


def logout(token: str | None) -> None:
    if token:
        db.delete_session(_token_hash(token))
