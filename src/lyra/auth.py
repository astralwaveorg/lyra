"""鉴权：scrypt 密码哈希 + Bearer Token。

设计取舍：

- **只存哈希。** 密码用 scrypt（内存硬），Token 存 SHA-256。数据库泄露
  不应等价于凭据泄露。
- **不内置默认密码。** 首次启动生成随机密码并写入数据目录（0600），
  由部署者读取后立即修改。避免「admin/admin」这种可被扫描的默认值。
- **Web 用会话 Cookie，API 用 Bearer Token。** 移动端只需要 Token，
  因此接口不依赖 Cookie 才能工作。
- **绝不在日志或页面输出凭据**（Token 只在创建时返回一次）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .db import StateStore

__all__ = [
    "DEFAULT_USERNAME",
    "AuthError",
    "hash_password",
    "verify_password",
    "ensure_admin",
    "authenticate",
    "create_token",
    "verify_token",
    "revoke_token",
    "list_tokens",
]

DEFAULT_USERNAME = "admin"

# scrypt 参数。n=2^14 在 NAS 的 Xeon 上约几十毫秒，足够挡住离线爆破。
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

_TOKEN_BYTES = 32


class AuthError(Exception):
    pass


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """生成自描述的 scrypt 哈希串，参数变更后可平滑升级。"""
    if not password:
        raise AuthError("密码不能为空")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常数时间比较，避免通过响应时间泄露信息。"""
    try:
        scheme, n_s, r_s, p_s, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(bytes.fromhex(dk_hex)),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(derived.hex(), dk_hex)


def generate_password(length: int = 20) -> str:
    """易读但仍高熵的随机密码（避免易混字符）。"""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# 初始账户
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AdminBootstrap:
    created: bool
    password: str | None
    password_file: Path | None
    from_config: bool = False
    """True 表示凭据来自配置/环境变量，因此没有生成随机密码、也没写任何文件。"""


def ensure_admin(
    store: StateStore,
    data_dir: Path,
    *,
    username: str = DEFAULT_USERNAME,
    password: str | None = None,
) -> AdminBootstrap:
    """确保存在一个管理员账户。

    **凭据应当由配置提供**（`web.admin_password`）或环境变量注入。
    只有两者都没有、且库里还没有任何账户时，才退化为随机密码，
    并写入 `data_dir/initial-password.txt`（0600）——这样做只是为了避免
    把部署者锁在门外，不是推荐做法。

    配置了密码时：不写文件、不打印任何提示。密码本身也绝不进日志。
    """
    if store.user_count() > 0:
        return AdminBootstrap(created=False, password=None, password_file=None)

    configured = bool(password)
    plain = password or generate_password()
    store.upsert_user(username=username, password_hash=hash_password(plain))

    if configured:
        return AdminBootstrap(
            created=True, password=None, password_file=None, from_config=True
        )

    target = data_dir / "initial-password.txt"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"username: {username}\npassword: {plain}\n"
            "建议改为在配置里设置 web.admin_password，然后删除本文件。\n",
            encoding="utf-8",
        )
        target.chmod(0o600)
    except OSError:
        target = None
    return AdminBootstrap(created=True, password=plain, password_file=target)


def set_password(store: StateStore, username: str, new_password: str) -> None:
    if store.get_user(username) is None:
        raise AuthError(f"用户不存在: {username}")
    store.upsert_user(username=username, password_hash=hash_password(new_password))


def authenticate(store: StateStore, username: str, password: str) -> bool:
    row = store.get_user(username)
    if row is None:
        # 即使用户不存在也做一次哈希运算，避免通过响应时间枚举用户名
        hash_password(password, salt=b"\x00" * 16)
        return False
    return verify_password(password, row["password_hash"])


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(
    store: StateStore,
    *,
    label: str | None = None,
    ttl_days: int | None = 365,
) -> str:
    """创建一个 Bearer Token。**明文只在此刻返回一次**。"""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = time.time() + ttl_days * 86400 if ttl_days else None
    store.add_token(token_hash=_token_hash(token), label=label, expires_at=expires_at)
    return token


def verify_token(store: StateStore, token: str) -> bool:
    if not token:
        return False
    row = store.find_token(_token_hash(token))
    if row is None or row["revoked_at"] is not None:
        return False
    if row["expires_at"] is not None and row["expires_at"] < time.time():
        return False
    store.touch_token(int(row["id"]))
    return True


def revoke_token(store: StateStore, token_id: int) -> None:
    store.revoke_token(token_id)


def list_tokens(store: StateStore) -> list[dict[str, object]]:
    """列出 Token 元信息。**绝不返回 Token 本身或它的哈希。**"""
    out: list[dict[str, object]] = []
    for row in store.iter_tokens():
        out.append(
            {
                "id": row["id"],
                "label": row["label"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "last_used_at": row["last_used_at"],
            }
        )
    return out
