"""A fresh data directory must boot into a usable, authenticated service."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.models import RootRole


def _config(tmp_path: Path, **web) -> Config:
    lib = tmp_path / "library"
    lib.mkdir()
    return Config(
        roots=[RootConfig(name="library", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(**web),
    )


def test_fresh_install_creates_database_admin_and_password_file(tmp_path: Path) -> None:
    cfg = _config(tmp_path, auth_required=True)
    with TestClient(create_app(cfg)) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["schema_version"] >= 1

        password_file = tmp_path / "data" / "initial-password.txt"
        assert password_file.is_file()
        text = password_file.read_text(encoding="utf-8")
        match = re.search(r"^password: (.+)$", text, re.MULTILINE)
        assert match, text
        password = match.group(1).strip()

        # Unauthenticated API calls are rejected.
        assert client.get("/api/v1/summary").status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": password},
        )
        assert login.status_code == 200, login.text
        assert login.json()["username"] == "admin"
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["username"] == "admin"
        assert client.get("/api/v1/summary").status_code == 200

        client.post("/api/v1/auth/logout")
        assert client.get("/api/v1/summary").status_code == 401


def test_configured_admin_password_is_used_without_a_file(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        auth_required=True,
        admin_username="owner",
        admin_password="configured-password",
    )
    with TestClient(create_app(cfg)) as client:
        assert not (tmp_path / "data" / "initial-password.txt").exists()
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "configured-password"},
        ).status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": "configured-password"},
        )
        assert login.status_code == 200
        assert client.get("/api/v1/auth/me").json()["username"] == "owner"


def test_open_mode_does_not_require_a_login(tmp_path: Path) -> None:
    cfg = _config(tmp_path, auth_required=False)
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/v1/summary").status_code == 200
