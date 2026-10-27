"""出站代理配置。

代理地址来自三处，优先级从高到低：

1. 设置页保存的 ``data/network.json``
2. ``lyra.toml`` 的 ``[network] proxy``
3. 容器/系统环境变量 ``HTTP_PROXY``、``HTTPS_PROXY``、``ALL_PROXY``

设置页保存后立即改写当前进程的环境变量；``lyra-watch`` 会周期性读取
同一个文件，换掉已经建立的 HTTP 客户端。这样 Web 与监控两个容器不必
重启就能共用一份代理设置。

本地回环与内网服务必须绕过代理，否则自建网易云 API、MusicBrainz 镜像
会被送到代理端再绕回来。这里按“私有地址、无点主机名、Docker 内部名”
自动收集例外，不把公网域名写死。
"""

from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .config import Config

__all__ = [
    "PROXY_SCHEMES",
    "DEFAULT_DIRECT_HOSTS",
    "NETWORK_FILENAME",
    "apply_proxy_environment",
    "environment_proxy",
    "load_network_settings",
    "local_no_proxy_hosts",
    "mask_proxy_url",
    "normalize_proxy_url",
    "proxy_bypass_hosts",
    "read_saved_proxy",
    "restore_proxy_environment",
    "write_saved_proxy",
]

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
"""``httpx`` 支持的代理协议。SOCKS 需要安装 ``httpx[socks]``。"""

DEFAULT_DIRECT_HOSTS = ("qq.com", "163.com", "126.net", "gtimg.cn")
"""默认直连的国内歌词来源与封面站点。

代理的意义是给境外服务（LRCLIB、MusicBrainz、AcoustID、大模型）找一条
能出去的路；国内来源本来就通，绕一圈反而多一跳，还可能因为出口 IP
变了被拒。所以默认让它们直连，用户可以在 ``[network] direct_hosts``
里改。
"""

NETWORK_FILENAME = "network.json"

_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
"""会被设置页接管的变量。只接管这些，不碰用户的其他环境变量。"""

_original_env: dict[str, str | None] | None = None


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


def read_saved_proxy(data_dir: Path) -> str | None:
    """读取设置页保存的代理。

    ``None`` 表示没有保存过（或文件损坏），调用方应回退到配置文件；
    空字符串表示用户明确保存了“不使用代理”，会压过配置文件里的值。
    """
    path = _settings_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("proxy")
    if not isinstance(raw, str):
        return None
    try:
        return normalize_proxy_url(raw)
    except ValueError:
        return None


def write_saved_proxy(data_dir: Path, proxy: str) -> None:
    """原子写入设置文件。含代理凭据，权限固定为 0600。"""
    proxy = normalize_proxy_url(proxy)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _settings_path(data_dir)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"proxy": proxy}, handle, ensure_ascii=False)
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
    for key in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


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


def _ensure_original_env() -> dict[str, str | None]:
    global _original_env
    if _original_env is None:
        _original_env = {key: os.environ.get(key) for key in _ENV_KEYS}
    return _original_env


def _merged_no_proxy(extra_hosts: Sequence[str]) -> str:
    original = _ensure_original_env().get("NO_PROXY") or os.environ.get("no_proxy") or ""
    hosts = [item.strip() for item in original.split(",") if item.strip()]
    for host in extra_hosts:
        host = host.strip()
        if host and host not in hosts:
            hosts.append(host)
    return ",".join(hosts)


def apply_proxy_environment(
    proxy: str,
    no_proxy_hosts: Sequence[str] = (),
) -> None:
    """让后续创建的 httpx 客户端使用指定代理。

    ``proxy`` 为空时恢复调用前的环境变量，也就是继续沿用系统原有设置。
    只用 ``ALL_PROXY`` 也能覆盖 http/https；SOCKS 时额外清掉 HTTP(S)_PROXY，
    避免旧的 HTTP 代理抢在 SOCKS 前面。
    """
    proxy = normalize_proxy_url(proxy)
    original = _ensure_original_env()

    if not proxy:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return

    scheme = urlsplit(proxy).scheme.lower()
    for key in ("ALL_PROXY", "all_proxy"):
        os.environ[key] = proxy
    if scheme in ("http", "https"):
        for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            os.environ[key] = proxy
    else:
        for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            os.environ.pop(key, None)

    merged = _merged_no_proxy(no_proxy_hosts)
    if merged:
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged
    else:
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)


def restore_proxy_environment() -> None:
    """把环境变量恢复到本进程第一次接管代理之前的状态。"""
    global _original_env
    if _original_env is None:
        return
    for key, value in _original_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _original_env = None


def _all_urls(cfg: Config) -> list[str]:
    return [
        cfg.providers.netease.base_url,
        cfg.providers.musicbrainz.base_url,
        cfg.providers.acoustid.base_url,
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
    """加载保存值/配置文件，并把结果写入当前进程环境。

    启动时调用一次；设置页保存后也会调用。``proxy_source`` 只用于界面
    提示，不写进配置文件。
    """
    saved = read_saved_proxy(cfg.storage.resolved_data_dir)
    if saved is not None:
        cfg.network.proxy = saved
        if saved:
            cfg.network.proxy_source = "settings"
        else:
            cfg.network.proxy_source = "env" if environment_proxy() else "none"
    elif cfg.network.proxy:
        cfg.network.proxy_source = "config"
    else:
        cfg.network.proxy_source = "env" if environment_proxy() else "none"

    apply_proxy_environment(cfg.network.proxy, proxy_bypass_hosts(cfg))
