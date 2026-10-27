"""网络代理设置：校验、持久化、环境变量与真实出站行为。"""

from __future__ import annotations

import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, ConfigError, RootConfig, StorageConfig, WebConfig, load_config
from lyra.models import RootRole
from lyra.network import restore_proxy_environment

_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """本机已有的代理变量不能影响“默认无代理”的断言。"""
    restore_proxy_environment()
    for key in _PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    restore_proxy_environment()


def _cfg(tmp_path: Path) -> Config:
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    return Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )


def test_保存代理并重启后仍然生效(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        before = client.get("/api/v1/settings/network").json()
        assert before["proxy"] == ""
        assert before["source"] == "none"

        response = client.put(
            "/api/v1/settings/network",
            json={"proxy": "http://10.0.1.8:7890"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["proxy"] == "http://10.0.1.8:7890"
        assert body["source"] == "settings"
        assert os.environ["ALL_PROXY"] == "http://10.0.1.8:7890"

        path = tmp_path / "data" / "network.json"
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "10.0.1.8:7890" in path.read_text(encoding="utf-8")

    cfg2 = _cfg(tmp_path)
    with TestClient(create_app(cfg2)) as client:
        data = client.get("/api/v1/settings/network").json()
        assert data["proxy"] == "http://10.0.1.8:7890"
        assert data["source"] == "settings"


def test_省略协议按http处理(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        response = client.put(
            "/api/v1/settings/network",
            json={"proxy": "10.0.1.8:7890"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["proxy"] == "http://10.0.1.8:7890"


@pytest.mark.parametrize(
    "value",
    [
        "ftp://10.0.1.8:7890",
        "http://10.0.1.8",
        "http://10.0.1.8:0",
        "http://10.0.1.8:70000",
        "http://10.0.1.8:7890/path",
        "http://10.0.1.8:7890?x=1",
        "socks4://10.0.1.8:7890",
    ],
)
def test_非法代理地址被拒绝(tmp_path: Path, value: str) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        response = client.put("/api/v1/settings/network", json={"proxy": value})
        assert response.status_code == 400, response.text
        assert not (tmp_path / "data" / "network.json").exists()


def test_支持socks5并安装socks依赖(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        response = client.put(
            "/api/v1/settings/network",
            json={"proxy": "socks5://10.0.1.8:7890"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["proxy"] == "socks5://10.0.1.8:7890"
        # 真正构造一次，确认 httpx[socks] 的依赖没有漏装。
        httpx.Client(proxy="socks5://10.0.1.8:7890").close()


def test_未保存时沿用环境变量(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALL_PROXY", "http://env-proxy:8080")
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        data = client.get("/api/v1/settings/network").json()
        assert data["proxy"] == ""
        assert data["source"] == "env"
        assert data["effective_proxy"] == "http://env-proxy:8080"


def test_清除设置后恢复环境变量(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALL_PROXY", "http://env-proxy:8080")
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        assert client.put(
            "/api/v1/settings/network",
            json={"proxy": "http://10.0.1.8:7890"},
        ).status_code == 200
        assert os.environ["ALL_PROXY"] == "http://10.0.1.8:7890"

        response = client.put("/api/v1/settings/network", json={"proxy": ""})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["proxy"] == ""
        assert data["source"] == "env"
        assert os.environ["ALL_PROXY"] == "http://env-proxy:8080"


def test_设置页代理真正接管httpx(tmp_path: Path) -> None:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 的约定
            seen.append(self.path)
            body = b"proxied"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = _cfg(tmp_path)
        with TestClient(create_app(cfg)) as client:
            proxy = f"http://127.0.0.1:{server.server_port}"
            assert client.put(
                "/api/v1/settings/network", json={"proxy": proxy}
            ).status_code == 200

            response = httpx.get("http://proxy-target.invalid/hello", timeout=5)
            assert response.status_code == 200
            assert response.text == "proxied"
            assert seen == ["http://proxy-target.invalid/hello"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_国内歌词来源默认直连境外走代理(tmp_path: Path) -> None:
    """开了代理也不该让国内来源换出口 IP。"""
    from httpx._utils import get_environment_proxies

    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        assert client.put(
            "/api/v1/settings/network",
            json={"proxy": "http://10.0.1.8:7890"},
        ).status_code == 200
        mounts = get_environment_proxies()
        assert "all://*qq.com" in mounts
        assert "all://*163.com" in mounts
        assert "all://*lrclib.net" not in mounts


def test_直连名单可以清空(tmp_path: Path) -> None:
    from httpx._utils import get_environment_proxies

    cfg = _cfg(tmp_path)
    cfg.network.direct_hosts = []
    with TestClient(create_app(cfg)) as client:
        assert client.put(
            "/api/v1/settings/network",
            json={"proxy": "http://10.0.1.8:7890"},
        ).status_code == 200
        mounts = get_environment_proxies()
        assert "all://*qq.com" not in mounts
        assert "all://localhost" in mounts


def test_配置文件里的代理被加载并校验(tmp_path: Path) -> None:
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        "config_version = 1\n"
        "\n[[roots]]\n"
        'name = "main"\n'
        f'path = "{tmp_path / "lib"}"\n'
        "\n[storage]\n"
        'data_dir = "./data"\n'
        "\n[network]\n"
        'proxy = "socks5://10.0.1.8:7890"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_file, env={})
    assert cfg.network.proxy == "socks5://10.0.1.8:7890"
    assert cfg.network.direct_hosts == ["qq.com", "163.com", "126.net", "gtimg.cn"]

    cfg_file.write_text(
        cfg_file.read_text(encoding="utf-8").replace(
            "socks5://10.0.1.8:7890", "ftp://10.0.1.8:7890"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_file, env={})
    assert any("network.proxy" in item for item in excinfo.value.problems)


def test_LYRA_HTTP_PROXY环境变量(tmp_path: Path) -> None:
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        "config_version = 1\n"
        "\n[[roots]]\n"
        'name = "main"\n'
        f'path = "{tmp_path / "lib"}"\n'
        "\n[storage]\n"
        'data_dir = "./data"\n',
        encoding="utf-8",
    )
    cfg = load_config(
        cfg_file, env={"LYRA_HTTP_PROXY": "http://10.0.1.8:7890"}
    )
    assert cfg.network.proxy == "http://10.0.1.8:7890"
    assert cfg.network.proxy_source == "config"
