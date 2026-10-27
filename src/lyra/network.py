"""出站代理：**按功能**决定走不走代理。

## 为什么不是「全局一个代理」

代理是给**境外**服务找一条能出去的路：LRCLIB、MusicBrainz、AcoustID
这些站点在国内直连经常超时或干脆连不上。但同一个进程里还有一堆**本来
直连就通**的国内服务（QQ 音乐、网易云、自建的网易云 API、自建的
MusicBrainz 镜像），它们走代理只会多一跳，还可能因为出口 IP 变了被拒。

更麻烦的是：代理通常跑在另一台机器上（笔记本上的代理软件）。那台机器
一停代理，若所有请求都死抱着代理不放，**连直连就通的国内接口也一起
挂掉**，界面上只剩一句「Connection refused」，使用者只会怀疑自己的
密钥。

所以这里的设计是：

- 代理地址只有一份（设置页 / 配置文件 / 环境变量三选一）；
- **每个境外功能各有一个开关**（`[network.use_proxy]`），决定它走不走代理；
- 国内来源（QQ、网易云）**永远直连**，没有开关——它们不需要代理；
- Lyra **不再改写进程的环境变量**。每个 HTTP 客户端按自己的功能决定
  用不用代理（见 `client_for`），「关掉开关」就一定不走代理。

## 代理地址从哪来

优先级从高到低：

1. 设置页保存的 ``data/network.json``
2. ``lyra.toml`` 的 ``[network] proxy``
3. 容器/系统环境变量 ``HTTP_PROXY``、``HTTPS_PROXY``、``ALL_PROXY``
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from .config import Config

__all__ = [
    "PROXY_SCHEMES",
    "PROXY_FEATURES",
    "DEFAULT_FEATURE_PROXY",
    "DEFAULT_DIRECT_HOSTS",
    "NETWORK_FILENAME",
    "NetworkState",
    "active_network",
    "client_for",
    "environment_proxy",
    "feature_uses_proxy",
    "load_network_settings",
    "local_no_proxy_hosts",
    "mask_proxy_url",
    "normalize_proxy_url",
    "proxy_bypass_hosts",
    "proxy_reachable",
    "read_saved_network",
    "set_active_network",
    "write_saved_network",
]

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
"""``httpx`` 支持的代理协议。SOCKS 需要安装 ``httpx[socks]``。"""

PROXY_FEATURES = ("lrclib", "musicbrainz", "acoustid", "cover")
"""可以单独决定是否走代理的功能。

- ``lrclib``：LRCLIB 歌词来源（境外）
- ``musicbrainz``：MusicBrainz 标签（境外）
- ``acoustid``：AcoustID 声学指纹（境外）
- ``cover``：按链接下载封面（看链接是境内还是境外，默认直连）

大模型不在这里：它的开关跟着大模型设置走（``[ai] use_proxy``），
因为接口地址本来就是使用者自己填的。
"""

DEFAULT_FEATURE_PROXY: dict[str, bool] = {
    "lrclib": True,
    "musicbrainz": True,
    "acoustid": True,
    "cover": False,
}
"""各功能默认是否走代理。

境外来源默认走代理（不然多半连不上）；按链接下载封面默认直连
（绝大多数封面链接是国内 CDN，绕一圈反而慢）。
"""

DEFAULT_DIRECT_HOSTS = ("qq.com", "163.com", "126.net", "gtimg.cn")
"""即使开了代理也直连的主机。

主要是**自建服务**与国内站点：自建的网易云 API、自建的 MusicBrainz
镜像都在内网，送到代理端再绕回来只会失败。也可以自己改。
"""

NETWORK_FILENAME = "network.json"

_probe_cache: dict[str, tuple[float, bool]] = {}
_PROBE_TTL_SECONDS = 10.0


# ---------------------------------------------------------------------------
# 代理地址的读写
# ---------------------------------------------------------------------------


def normalize_proxy_url(raw: Any) -> str:
    """校验并规范化代理地址。

    允许省略协议：``10.0.1.8:7890`` 会按 HTTP 代理处理。必须显式带端口，
    避免 ``http://proxy`` 被误当成 80 端口而连到完全不同的服务。
    """
    if not isinstance(raw, str):
        raise ValueError("代理地址必须是字符串")
    text = raw.strip()
    if not text:
        return ""
    if any(ch.isspace() for ch in text):
        raise ValueError("代理地址不能包含空白字符")
    if "://" not in text:
        text = "http://" + text

    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError(
            "代理地址只支持 http、https、socks5、socks5h"
        )
    if not parsed.hostname:
        raise ValueError("代理地址缺少主机名")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口不合法") from exc
    if port is None:
        raise ValueError("代理地址必须包含端口，例如 http://10.0.1.8:7890")
    if not (1 <= port <= 65535):
        raise ValueError("代理端口必须在 1 到 65535 之间")
    if parsed.path not in ("", "/"):
        raise ValueError("代理地址不能带路径")
    if parsed.query or parsed.fragment:
        raise ValueError("代理地址不能带查询参数或片段")

    return text


def mask_proxy_url(url: str) -> str:
    """隐藏代理地址里的用户名密码，保留主机与端口便于排障。"""
    if not url:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "***"
    if "@" not in parsed.netloc:
        return url
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://***@{host}{port}"


def _settings_path(data_dir: Path) -> Path:
    return data_dir / NETWORK_FILENAME


def read_saved_network(data_dir: Path) -> tuple[str | None, dict[str, bool] | None]:
    """读取设置页保存的代理与各功能开关。

    返回 (代理地址, 功能开关)。代理为 ``None`` 表示没有保存过（或文件
    损坏），调用方应回退到配置文件；空字符串表示用户明确保存了
    「不使用代理」，会压过配置文件里的值。开关为 ``None`` 表示没保存过。
    """
    path = _settings_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None

    raw = payload.get("proxy")
    proxy: str | None = None
    if isinstance(raw, str):
        try:
            proxy = normalize_proxy_url(raw)
        except ValueError:
            proxy = None

    features: dict[str, bool] | None = None
    raw_features = payload.get("use_proxy")
    if isinstance(raw_features, dict):
        features = {
            name: bool(value)
            for name, value in raw_features.items()
            if name in PROXY_FEATURES and isinstance(value, bool)
        }
    return proxy, features


def write_saved_network(
    data_dir: Path, proxy: str, features: Mapping[str, bool]
) -> None:
    """原子写入设置文件。含代理凭据，权限固定为 0600。"""
    proxy = normalize_proxy_url(proxy)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _settings_path(data_dir)
    tmp = path.with_name(path.name + ".tmp")
    payload = {
        "proxy": proxy,
        "use_proxy": {
            name: bool(features.get(name, False))
            for name in PROXY_FEATURES
        },
    }
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def environment_proxy() -> str:
    """当前环境里第一个可用的代理地址。"""
    for key in (
        "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
        "HTTP_PROXY", "http_proxy",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# 哪些主机必须直连
# ---------------------------------------------------------------------------


def _is_local_host(host: str) -> bool:
    name = host.strip().lower()
    if name in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
        return True
    if name.endswith(".local") or name.endswith(".internal"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        # 没有点的短主机名通常是 Docker Compose 服务名
        return "." not in name
    return address.is_private or address.is_loopback or address.is_link_local


def local_no_proxy_hosts(urls: Iterable[str]) -> list[str]:
    """收集必须直连的主机：回环、内网 IP、Docker 服务名、内网镜像。"""
    hosts = ["localhost", "127.0.0.1", "::1"]
    seen = set(hosts)
    for url in urls:
        text = (url or "").strip()
        if not text:
            continue
        try:
            host = urlsplit(text).hostname or ""
        except ValueError:
            continue
        if host and _is_local_host(host) and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


# ---------------------------------------------------------------------------
# 代理是否连得上
# ---------------------------------------------------------------------------


def proxy_reachable(proxy: str, timeout: float = 1.5, *, force: bool = False) -> bool:
    """代理端口能不能连上。

    只做 TCP 探测：代理是什么协议、转发到哪里，都不重要——连不上就是
    连不上，后面所有请求都会失败。SOCKS5 同样是 TCP，探测方式一致。

    结果缓存十秒：一次界面刷新不该连着探好几次。`force` 用于「测试」
    按钮——使用者刚点完就是要立刻知道结果。
    """
    try:
        parsed = urlsplit(normalize_proxy_url(proxy))
    except ValueError:
        return False
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return False

    now = time.monotonic()
    cached = _probe_cache.get(proxy)
    if not force and cached and now - cached[0] < _PROBE_TTL_SECONDS:
        return cached[1]

    ok = False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except OSError:
        ok = False

    _probe_cache[proxy] = (now, ok)
    return ok


# ---------------------------------------------------------------------------
# 当前生效的网络状态
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NetworkState:
    """进程内当前生效的网络设置。"""

    proxy: str = ""
    """代理地址（空 = 没有代理）。"""

    features: dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_FEATURE_PROXY)
    )
    """各功能是否走代理。"""

    bypass_hosts: list[str] = field(default_factory=list)
    """即使走代理也要直连的主机（内网服务、直连名单）。"""


_state = NetworkState()


def set_active_network(
    proxy: str,
    features: Mapping[str, bool] | None = None,
    bypass_hosts: Sequence[str] = (),
) -> NetworkState:
    """更新进程内生效的网络设置，并返回当前状态。"""
    merged = dict(DEFAULT_FEATURE_PROXY)
    for name in PROXY_FEATURES:
        merged[name] = bool(
            (features or {}).get(name, DEFAULT_FEATURE_PROXY[name])
        )
    _state.proxy = normalize_proxy_url(proxy)
    _state.features = merged
    _state.bypass_hosts = list(bypass_hosts)
    return _state


def active_network() -> NetworkState:
    """当前生效的网络设置。"""
    return _state


def feature_uses_proxy(feature: str) -> bool:
    """这个功能是否走代理。"""
    return bool(_state.features.get(feature, False))


def client_for(
    feature: str,
    *,
    use_proxy: bool | None = None,
    **kwargs: Any,
) -> httpx.Client:
    """按功能建立 HTTP 客户端：只有开了这个功能的代理才走代理。

    - 走代理：显式传 ``proxy``，内网与直连名单用 ``mounts`` 排除；
    - 不走代理：**连环境变量里的代理也不认**（``trust_env=False``），
      否则「关掉开关」却仍在走代理，界面上的开关就成了摆设。

    注入 ``transport`` 的测试路径不受影响：那时不该连真实代理。
    """
    enabled = feature_uses_proxy(feature) if use_proxy is None else bool(use_proxy)
    proxy = _state.proxy if enabled else ""
    if proxy and kwargs.get("transport") is None:
        kwargs["proxy"] = proxy
        mounts = {f"all://*{host}": None for host in _state.bypass_hosts}
        if mounts:
            kwargs["mounts"] = mounts
    else:
        kwargs.setdefault("trust_env", False)
    return httpx.Client(**kwargs)


# ---------------------------------------------------------------------------
# 与配置文件的衔接
# ---------------------------------------------------------------------------


def _all_urls(cfg: Config) -> list[str]:
    return [
        cfg.providers.netease.base_url,
        cfg.providers.musicbrainz.base_url,
        cfg.providers.acoustid.base_url,
        cfg.ai.base_url,
    ]


def proxy_bypass_hosts(cfg: Config) -> list[str]:
    """必须直连的主机：本地服务 + 配置里指定的直连名单。"""
    hosts = local_no_proxy_hosts(_all_urls(cfg))
    seen = set(hosts)
    for host in cfg.network.direct_hosts:
        name = (host or "").strip().lower()
        if name and name not in seen:
            seen.add(name)
            hosts.append(name)
    return hosts


def load_network_settings(cfg: Config) -> None:
    """加载保存值/配置文件，并把结果写进进程内状态。

    启动时调用一次；设置页保存后也会调用。``proxy_source`` 只用于界面
    提示，不写进配置文件。
    """
    saved_proxy, saved_features = read_saved_network(
        cfg.storage.resolved_data_dir
    )
    if saved_proxy is not None:
        cfg.network.proxy = saved_proxy
        cfg.network.proxy_source = (
            "settings" if saved_proxy else ("env" if environment_proxy() else "none")
        )
    elif cfg.network.proxy:
        cfg.network.proxy_source = "config"
    else:
        cfg.network.proxy_source = "env" if environment_proxy() else "none"

    if saved_features is not None:
        for name, value in saved_features.items():
            cfg.network.use_proxy[name] = value

    set_active_network(
        # 没有显式配置时沿用环境变量里的代理地址——但**只**给开了开关的功能。
        cfg.network.proxy or environment_proxy(),
        cfg.network.use_proxy,
        proxy_bypass_hosts(cfg),
    )
