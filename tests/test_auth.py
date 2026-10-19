"""Authentication primitives and first-run administrator bootstrap."""

from __future__ import annotations

import stat
import time
from pathlib import Path

from lyra.auth import (
    AuthError,
    authenticate,
    create_token,
    ensure_admin,
    generate_password,
    hash_password,
    list_tokens,
    set_password,
    verify_password,
    verify_token,
)
from lyra.db import open_store


def test_password_hash_roundtrip() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)


def test_password_hash_rejects_empty_and_corrupt_values() -> None:
    try:
        hash_password("")
    except AuthError:
        pass
    else:
        raise AssertionError("empty passwords must be rejected")
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "bcrypt$1$2$3$4$5")


def test_generated_password_is_long_and_avoids_ambiguous_characters() -> None:
    password = generate_password(24)
    assert len(password) == 24
    assert not set(password) & set("0O1lI")


def test_first_run_writes_a_private_password_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    handle = open_store(data_dir)
    try:
        boot = ensure_admin(handle, data_dir)
        assert boot.created
        assert boot.password
        assert boot.password_file is not None
        assert boot.password_file.is_file()
        assert stat.S_IMODE(boot.password_file.stat().st_mode) == 0o600
        assert authenticate(handle, "admin", boot.password or "")
    finally:
        handle.close()


def test_bootstrap_is_idempotent_and_keeps_an_existing_account(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    handle = open_store(data_dir)
    try:
        first = ensure_admin(handle, data_dir)
        second = ensure_admin(handle, data_dir)
        assert first.created
        assert not second.created
        assert handle.user_count() == 1
        assert authenticate(handle, "admin", first.password or "")
    finally:
        handle.close()

    # Reopening the same data directory must not create a second account.
    reopened = open_store(data_dir)
    try:
        third = ensure_admin(reopened, data_dir)
        assert not third.created
        assert reopened.user_count() == 1
    finally:
        reopened.close()


def test_configured_password_does_not_create_a_password_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    handle = open_store(data_dir)
    try:
        boot = ensure_admin(handle, data_dir, username="owner", password="a-strong-password")
        assert boot.created
        assert boot.from_config
        assert boot.password_file is None
        assert not (data_dir / "initial-password.txt").exists()
        assert authenticate(handle, "owner", "a-strong-password")
    finally:
        handle.close()


def test_set_password_rejects_unknown_users(store) -> None:
    try:
        set_password(store, "nobody", "a-strong-password")
    except AuthError:
        pass
    else:
        raise AssertionError("unknown users must not be created implicitly")


def test_tokens_are_hashed_revocable_and_expire(store) -> None:
    token = create_token(store, label="test", ttl_days=1)
    assert token
    assert verify_token(store, token)
    assert not verify_token(store, "")
    assert not verify_token(store, "not-a-token")

    rows = list_tokens(store)
    assert len(rows) == 1
    assert rows[0]["label"] == "test"
    assert token not in str(rows)

    from lyra.auth import revoke_token

    revoke_token(store, int(rows[0]["id"]))
    assert not verify_token(store, token)


def test_expired_tokens_are_rejected(store) -> None:
    token = create_token(store, label="expired", ttl_days=-1)
    assert not verify_token(store, token)


def test_password_change_workflow(store) -> None:
    ensure_admin(store, Path("/tmp"), username="admin", password="old-password-123")
    assert authenticate(store, "admin", "old-password-123")
    set_password(store, "admin", "new-password-456")
    assert not authenticate(store, "admin", "old-password-123")
    assert authenticate(store, "admin", "new-password-456")
    assert time.time() > 0
