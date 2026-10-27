"""网络代理设置：校验、持久化、按功能路由与真实出站行为。

## 重点：代理是**按功能**走的

代理多半跑在另一台机器上，那台机器一停代理，走代理的功能会一起失败。
所以：

- 每个境外功能各有一个开关（LRCLIB、MusicBrainz、AcoustID、封面）；
- 国内来源（QQ、网易云）永远直连；
- Lyra 不再改写进程的环境变量——「关掉开关」就一定不走代理。
"""

from __future__ import annotations

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
from lyra.network import (
    PROXY_FEATURES,
    active_network,
    client_for,
    set_active_network,
)

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
    set_active_network("", {}, [])
    for key in _PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    set_active_network("", {}, [])


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


def test_环境变量里的代理地址也能被功能用上(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没在设置页配置地址时，环境变量里的地址仍然给开了开关的功能用。"""
    monkeypatch.setenv("ALL_PROXY", "http://env-proxy:8080")
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)):
        state = active_network()
        assert state.proxy == "http://env-proxy:8080"
        assert state.features["lrclib"] is True
        # 关掉的功能即使有代理地址也不走
        assert state.features["cover"] is False


def test_各功能开关可以单独保存与恢复(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        response = client.put(
            "/api/v1/settings/network",
            json={"use_proxy": {"lrclib": False, "cover": True}},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["use_proxy"]["lrclib"] is False
        assert body["use_proxy"]["cover"] is True
        assert body["use_proxy"]["musicbrainz"] is True

    cfg2 = _cfg(tmp_path)
    with TestClient(create_app(cfg2)) as client:
        data = client.get("/api/v1/settings/network").json()
        assert data["use_proxy"]["lrclib"] is False
        assert data["use_proxy"]["cover"] is True


def test_未知功能被拒绝(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        response = client.put(
            "/api/v1/settings/network",
            json={"use_proxy": {"qq": True}},
        )
        assert response.status_code == 400, response.text
        assert "qq" in response.json()["detail"]


def test_代理只作用于开了开关的功能(tmp_path: Path) -> None:
    """核心行为：开了的功能走代理，没开的直连。

    代理是应用**按功能**加到客户端上的，不再是环境变量里的全局设置。
    """
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
        proxy = f"http://127.0.0.1:{server.server_port}"
        set_active_network(proxy, {"lrclib": True, "musicbrainz": False}, [])

        with client_for("lrclib", timeout=5) as client:
            response = client.get("http://proxy-target.invalid/hello")
            assert response.status_code == 200
            assert response.text == "proxied"
        assert seen == ["http://proxy-target.invalid/hello"]

        # 关掉的功能不经过代理：目标主机解析失败就是「没走代理」的证据
        with client_for("musicbrainz", timeout=5) as client, pytest.raises(httpx.HTTPError):
            client.get("http://proxy-target.invalid/hello")
        assert len(seen) == 1, "只有开了开关的功能才该走代理"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_国内来源永远直连(tmp_path: Path) -> None:
    """QQ、网易云这些国内来源没有开关，一律直连。"""
    set_active_network("http://10.0.1.8:7890", dict.fromkeys(PROXY_FEATURES, True), [])
    for feature in ("qq", "netease", "shortlink"):
        client = client_for(feature, timeout=5)
        try:
            assert client._mounts == {}, feature
        finally:
            client.close()


def test_内网服务即使开代理也直连(tmp_path: Path) -> None:
    """自建的网易云 API、自建的 MusicBrainz 镜像都在内网，不能绕代理。"""
    set_active_network(
        "http://10.0.1.8:7890",
        dict.fromkeys(PROXY_FEATURES, True),
        ["lyra-netease-api", "192.168.1.10"],
    )
    with client_for("musicbrainz", timeout=5) as client:
        patterns = {pattern.pattern for pattern in client._mounts}
        assert "all://*lyra-netease-api" in patterns
        assert "all://*192.168.1.10" in patterns


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
        'proxy = "socks5://10.0.1.8:7890"\n'
        "\n[network.use_proxy]\n"
        "lrclib = false\n"
        "cover = true\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file, env={})
    assert cfg.network.proxy == "socks5://10.0.1.8:7890"
    assert cfg.network.use_proxy["lrclib"] is False
    assert cfg.network.use_proxy["cover"] is True
    assert cfg.network.use_proxy["musicbrainz"] is True

    cfg_file.write_text(
        cfg_file.read_text(encoding="utf-8").replace(
            "socks5://10.0.1.8:7890", "ftp://10.0.1.8:7890"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_file, env={})
    assert any("network.proxy" in item for item in excinfo.value.problems)


def test_配置里的未知功能被拒绝(tmp_path: Path) -> None:
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        "config_version = 1\n"
        "\n[[roots]]\n"
        'name = "main"\n'
        f'path = "{tmp_path / "lib"}"\n'
        "\n[storage]\n"
        'data_dir = "./data"\n'
        "\n[network.use_proxy]\n"
        "qq = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_file, env={})
    assert any("network.use_proxy" in item for item in excinfo.value.problems)


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


def test_代理状态对使用者可见(tmp_path: Path) -> None:
    """代理连不上时要在界面上看得见，不能只等请求失败。"""
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        client.put(
            "/api/v1/settings/network",
            json={"proxy": "http://127.0.0.1:1"},  # 一定连不上
        )
        data = client.get("/api/v1/settings/network").json()
        assert data["proxy_status"] == "unreachable"
        assert data["proxy_reachable"] is False

        result = client.post("/api/v1/settings/network/test").json()
        assert result["reachable"] is False
        assert "连不上" in result["message"]


def test_生效状态在进程内可查(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg)) as client:
        client.put(
            "/api/v1/settings/network",
            json={"proxy": "http://10.0.1.8:7890", "use_proxy": {"lrclib": False}},
        )
    state = active_network()
    assert state.proxy == "http://10.0.1.8:7890"
    assert state.features["lrclib"] is False
