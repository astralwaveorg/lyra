"""AI 设置页的持久化、token 掩码与协议校验。"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lyra.ai as ai_module
from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.models import RootRole


def _cfg(tmp_path: Path) -> Config:
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    return Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )


def test_保存token不落数据库且文件为0600(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        before = client.get("/api/v1/settings/ai")
        assert before.status_code == 200
        assert before.json()["has_api_key"] is False

        response = client.put(
            "/api/v1/settings/ai",
            json={
                "enabled": True,
                "protocol": "anthropic",
                "base_url": "https://api.anthropic.com",
                "model": "claude-test",
                "api_key": "secret-token-value",
                "timeout_seconds": 30,
                "max_requests_per_run": 5,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["has_api_key"] is True
        assert body["api_key_source"] == "settings"
        assert "secret-token-value" not in response.text

        token_file = tmp_path / "data" / "ai-token"
        assert token_file.is_file()
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert token_file.read_text() == "secret-token-value"
        assert "secret-token-value" not in str(
            client.get("/api/v1/settings/ai").json()
        )


def test_重启后设置和token仍然生效(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        assert client.put(
            "/api/v1/settings/ai",
            json={
                "enabled": True,
                "protocol": "openai_responses",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-test",
                "api_key": "restart-token",
            },
        ).status_code == 200

    cfg2 = _cfg(tmp_path)
    with TestClient(create_app(cfg2)) as client:
        data = client.get("/api/v1/settings/ai").json()
        assert data["enabled"] is True
        assert data["protocol"] == "openai_responses"
        assert data["model"] == "gpt-test"
        assert data["has_api_key"] is True
        assert data["api_key_source"] == "settings"


def test_非法协议和地址被拒绝(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        assert client.put(
            "/api/v1/settings/ai",
            json={"protocol": "unknown"},
        ).status_code == 400
        assert client.put(
            "/api/v1/settings/ai",
            json={"base_url": "file:///etc/passwd"},
        ).status_code == 400
        assert client.put(
            "/api/v1/settings/ai",
            json={"model": ""},
        ).status_code == 400


def test_启用但无token被拒绝(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        response = client.put(
            "/api/v1/settings/ai",
            json={"enabled": True, "protocol": "openai_chat", "model": "gpt-test"},
        )
        assert response.status_code == 400
        assert "Token" in response.json()["detail"]


def test_清除已保存token(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        client.put(
            "/api/v1/settings/ai",
            json={"model": "m", "api_key": "to-clear"},
        )
        response = client.put(
            "/api/v1/settings/ai",
            json={"model": "m", "clear_api_key": True},
        )
        assert response.status_code == 200
        assert response.json()["has_api_key"] is False
        assert not (tmp_path / "data" / "ai-token").exists()


def test_环境变量优先且不能从设置页覆盖(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LYRA_AI_API_KEY", "env-token")
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        data = client.get("/api/v1/settings/ai").json()
        assert data["api_key_source"] == "env"
        assert data["has_api_key"] is True
        response = client.put(
            "/api/v1/settings/ai",
            json={"model": "m", "api_key": "page-token"},
        )
        assert response.status_code == 400
        assert "环境变量" in response.json()["detail"]


def test_测试连接使用表单token但不写文件(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    called = {}

    def fake_test(**kwargs):
        called.update(kwargs)
        return {
            "ok": True,
            "protocol": kwargs["protocol"],
            "model": kwargs["model"],
            "endpoint": "https://api.test/v1/chat/completions",
            "sample": "OK",
            "latency_ms": 1,
        }

    monkeypatch.setattr(ai_module, "test_connection", fake_test)
    with TestClient(create_app(cfg)) as client:
        response = client.post(
            "/api/v1/settings/ai/test",
            json={
                "protocol": "openai_chat",
                "base_url": "https://api.test/v1",
                "model": "m",
                "api_key": "form-token",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert called["api_key"] == "form-token"
        assert not (tmp_path / "data" / "ai-token").exists()
