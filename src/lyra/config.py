"""配置加载、校验与密钥处理。

分层（优先级从低到高）：
    内置默认值  →  TOML 配置文件  →  环境变量 LYRA_*

硬性规则：源码内不得出现任何用户的绝对路径、主机名、账号或凭据。
所有路径与凭据都来自本模块解析出的配置对象。

校验策略：**未知键直接报错**。静默忽略拼错的键，
会让用户以为改了配置而实际没生效。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import __version__
from .models import RootRole

__all__ = [
    "ConfigError",
    "RootConfig",
    "StorageConfig",
    "WatchConfig",
    "QQProviderConfig",
    "LrcLibProviderConfig",
    "AcoustIdProviderConfig",
    "ProvidersConfig",
    "MatchingPolicy",
    "TagPolicy",
    "PolicyConfig",
    "RetryConfig",
    "LyricsFormatConfig",
    "WebConfig",
    "WriteConfig",
    "Config",
    "load_config",
    "DEFAULT_CONFIG_FILENAME",
]

DEFAULT_CONFIG_FILENAME = "lyra.toml"
CONFIG_VERSION = 1

# 视为秘密的配置路径（点分），打印与导出时掩码
_SECRET_PATHS: tuple[str, ...] = (
    "providers.lrclib.authorization_ref",
    "providers.acoustid.api_key",
    "web.admin_password",
)


class ConfigError(Exception):
    """配置错误。所有问题一次性抛出，便于一次改完。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("配置无效:\n  - " + "\n  - ".join(problems))


# ---------------------------------------------------------------------------
# 分节
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RootConfig:
    name: str = "main"
    role: RootRole = RootRole.LIBRARY
    path: str = ""
    host_path: str = ""
    read_only: bool = False
    enabled: bool = True
    # 忽略的目录名（任意层级）
    ignore_dirs: list[str] = field(default_factory=list)
    # 忽略的文件名 glob
    ignore_globs: list[str] = field(default_factory=list)

    @property
    def resolved_path(self) -> Path:
        return Path(self.path).expanduser()


@dataclass(slots=True)
class StorageConfig:
    data_dir: str = "./data"

    @property
    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir).expanduser()


@dataclass(slots=True)
class WatchConfig:
    backend: str = "auto"
    stable_seconds: int = 60
    stable_samples: int = 3
    reconcile_interval_seconds: int = 900
    full_scan_local_time: str = "03:30"
    timezone: str = "Asia/Shanghai"


@dataclass(slots=True)
class QQProviderConfig:
    minimum_request_interval_seconds: float = 2.0
    query_limit_per_round: int = 3
    candidates_per_query: int = 20
    detail_and_lyric_limit_per_round: int = 6
    request_timeout_seconds: float = 20.0
    search_timeout_seconds: float = 180.0
    #: 上游给出 Retry-After 且等待很短时的有界内联重试次数（0 = 不重试）
    max_inline_retries: int = 2
    #: 超过这个秒数的 Retry-After 不再内联等待，直接交给外层退避与熔断
    max_inline_retry_wait_seconds: float = 5.0


@dataclass(slots=True)
class LrcLibProviderConfig:
    client_name: str = "Lyra"
    client_version: str = __version__
    client_url: str = ""
    authorization_ref: str = ""
    #: LRCLIB 公共 API 的官方限额约 20 次/秒，官方建议请求间延迟 200~500ms。
    #: 取 0.5s（=2 次/秒）落在建议区间的保守端，仍比 20/s 低一个数量级。
    #: 之前的 5.0s 比官方建议还保守 10 倍，纯属自我拖慢。
    minimum_request_interval_seconds: float = 0.5
    max_backoff_seconds: int = 3600
    circuit_breaker_threshold: int = 5
    circuit_breaker_initial_seconds: int = 1800
    request_timeout_seconds: float = 20.0
    #: 实际 LRCLIB 过载时返回 503 ServerOverloaded + `Retry-After: 1`。
    #: 等它说的 1 秒再试，收益远大于直接退避 60 秒；但必须有硬上限，
    #: 否则会变成对已然过载的服务端锤击。
    max_inline_retries: int = 2
    max_inline_retry_wait_seconds: float = 5.0

    def client_identity(self) -> str:
        """构造 LRCLIB 要求的客户端标识串。

        官方约定（从前端 bundle 提取）：
            <AppName>/<version> (<URL>)
        两个头 X-User-Agent 与 Lrclib-Client 使用同一串。
        """
        base = f"{self.client_name}/{self.client_version}"
        return f"{base} ({self.client_url})" if self.client_url else base


@dataclass(slots=True)
class NetEaseProviderConfig:
    """网易云音乐来源。

    实际（从本地网络发起）：搜索与歌词接口均返回 200，歌词为毫秒精度
    ``[00:00.000]``。它与 QQ 的曲库覆盖并不重合，同一首歌常只有一家有
    同步歌词，因此值得作为第三个来源。
    """

    minimum_request_interval_seconds: float = 1.5
    query_limit_per_round: int = 3
    candidates_per_query: int = 20
    detail_and_lyric_limit_per_round: int = 6
    request_timeout_seconds: float = 20.0
    max_inline_retries: int = 2
    max_inline_retry_wait_seconds: float = 5.0
    base_url: str = ""
    """自建网易云 API 的根地址（如 `http://127.0.0.1:3000`）。

    留空表示直接用 `music.163.com` 的公开 web 接口（实际可用）。
    自建版（NeteaseCloudMusicApi）的端点与参数名不同，见 providers/netease.py。
    自建的好处是稳定、不受公开接口风控影响，且能走内网。
    """


@dataclass(slots=True)
class MusicBrainzProviderConfig:
    """MusicBrainz 标签来源。

    默认使用公共 API。公共实例限制每个客户端每秒 1 次请求，因此默认
    间隔取 1.1 秒；自建镜像可以把 ``minimum_request_interval_seconds``
    调低到 0.2 左右。

    大规模扫描更推荐自建 MusicBrainz 镜像，配置项与公共 API 相同，
    只需把 ``base_url`` 指向镜像地址。
    """

    enabled: bool = True
    base_url: str = "https://musicbrainz.org"
    request_timeout_seconds: float = 20.0
    minimum_request_interval_seconds: float = 1.1
    search_limit: int = 8
    detail_limit: int = 3


@dataclass(slots=True)
class AcoustIdProviderConfig:
    """AcoustID 声学指纹。用**音频本身**确认「这条录音是谁」，

    与标签、文件名、目录结构完全独立。对「标签里没有 MBID」和
    「搜索结果有歧义」两类最难的情况，它是唯一可信的独立证据。
    """

    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://api.acoustid.org"
    request_timeout_seconds: float = 20.0
    minimum_request_interval_seconds: float = 0.34
    """AcoustID 限流为每秒 3 次；留一点余量，别在边界上被拒。"""


@dataclass(slots=True)
class ProvidersConfig:
    enabled: list[str] = field(default_factory=lambda: ["qq", "netease", "lrclib"])
    priority_zh: list[str] = field(default_factory=lambda: ["qq", "netease", "lrclib"])
    """华语圈曲目的来源顺序：QQ → 网易云 → LRCLIB（AI 兜底另行接入）。"""
    priority_foreign: list[str] = field(
        default_factory=lambda: ["lrclib", "netease", "qq"]
    )
    """外语圈曲目的来源顺序：LRCLIB → 网易云 → QQ。

    各源在不同语种上的覆盖差异极大，用一套固定顺序必然在另一半曲目上吃亏。
    """
    cjk_uses_zh_priority: bool = True
    """日韩是否也走华语优先级。默认是——实际 QQ 对 J-Pop/K-Pop 覆盖同样强。"""
    qq: QQProviderConfig = field(default_factory=QQProviderConfig)
    lrclib: LrcLibProviderConfig = field(default_factory=LrcLibProviderConfig)
    netease: NetEaseProviderConfig = field(default_factory=NetEaseProviderConfig)
    musicbrainz: MusicBrainzProviderConfig = field(
        default_factory=MusicBrainzProviderConfig
    )
    acoustid: AcoustIdProviderConfig = field(default_factory=AcoustIdProviderConfig)


@dataclass(slots=True)
class MatchingPolicy:
    #: 硬闸门：时长差超过此值的候选**直接否决**（不是降权）。
    #: 从 2.0 放宽到 5.0——默认不要过度保守；实际曲库里大量曲目
    #: 只有略微不同的版本时长，2 秒会把它们全部推给人工。
    max_duration_delta_seconds: float = 5.0
    min_score_margin: float = 0.15
    min_confidence: float = 0.75
    #: 时长差 ≤ auto_accept_within_seconds 时，分数只需达到 auto_accept_min_confidence
    auto_accept_within_seconds: float = 1.5
    auto_accept_min_confidence: float = 0.45
    #: 时长差 ≤ auto_accept_relaxed_seconds 时，分数需达到 auto_accept_relaxed_confidence
    auto_accept_relaxed_seconds: float = 3.0
    auto_accept_relaxed_confidence: float = 0.65
    #: 时长差在硬闸门内、分数达到此值且明显优于次优时也自动接受
    auto_accept_wide_confidence: float = 0.70
    #: 自动接受的**独立**下限：标题与艺人相似度。
    #: 为什么必需：时长权重占总分 0.40，Δt=0 就能拿满。实际一个完全不相干的
    #: 标题配上正确时长也能得到 0.679 分——只按总分放行就会写错歌词。
    #: 这两个下限把「时长凑巧相同但根本不是同一首歌」挡在门外。
    auto_accept_min_title_score: float = 0.85
    auto_accept_min_artist_score: float = 0.50
    """时长差在此范围内时，即使「最优与次优接近」也直接接受，不送复核。

    为什么这条是安全的：LRCLIB 上同一首歌常有**多条元数据完全相同**的记录，
    把它们判为「歧义」纯属浪费人工——它们指向同一录音。

    为什么**不能**放宽成「不看版本」：时长相等并不区分版本。实际曲库里
    `喜欢你 (Live)` 与录音室版时长完全相同；`A.I.N.Y.` 更是反例——
    重生版时长差 0.20s，而正确的原版是 0.80s。若只按时长挑最近的，
    会选中重生版。因此这条规则**不绕过版本校验，也不绕过置信度下限**。
    """


@dataclass(slots=True)
class TagPolicy:
    """标签刮削的自动化策略。

    自动写入只做补缺失或按可信目录簇纠正发行级字段；普通冲突进入
    人工复核。界面里冲突默认勾选，但那是人工确认后的动作，不属于自动流程。
    """

    auto_scan: bool = True
    """监控进程按间隔自动扫描全库并补标签。"""
    scan_interval_hours: float = 24.0
    auto_write: bool = True
    """扫描后自动写入「补空且高置信」的那部分。"""
    fingerprint: bool = True
    """缺 MBID 或匹配有歧义时，用 AcoustID 指纹做独立确认。"""
    max_auto_write_per_run: int = 300
    max_duration_delta_seconds: float = 5.0
    """曲目时长与来源相差超过此值时，不自动写入（交人工看一眼）。"""


@dataclass(slots=True)
class PolicyConfig:
    write_enabled: bool = True
    write_mode: str = "auto"
    write_sidecar_lrc: bool = True
    embed_lyrics: bool = True
    fill_empty_lrc: bool = True
    skip_if_valid_synced: bool = True
    clear_plain_embedded: bool = True
    allow_other_providers: bool = False
    matching: MatchingPolicy = field(default_factory=MatchingPolicy)
    tags: TagPolicy = field(default_factory=TagPolicy)


@dataclass(slots=True)
class RetryConfig:
    not_found_intervals_days: list[int] = field(default_factory=lambda: [7, 30, 90])
    no_synced_intervals_days: list[int] = field(default_factory=lambda: [30, 90, 180])
    transient_backoff_seconds: list[int] = field(
        default_factory=lambda: [60, 300, 1800, 7200, 21600]
    )


@dataclass(slots=True)
class LyricsFormatConfig:
    encoding: str = "utf-8"
    bom: bool = False
    line_ending: str = "lf"
    format: str = "line_lrc"
    translation: bool = False
    romanization: bool = False


AI_PROTOCOLS = ("openai_chat", "openai_responses", "anthropic")
"""支持的大模型协议。

- openai_chat: OpenAI Chat Completions，以及兼容它的服务
- openai_responses: OpenAI Responses API
- anthropic: Anthropic Messages API
"""

AI_SETTINGS_FIELDS = (
    "enabled",
    "protocol",
    "base_url",
    "model",
    "timeout_seconds",
    "max_requests_per_run",
    "auto_clean_duplicates",
)

AI_SETTINGS_BOUNDS: dict[str, tuple[float, float]] = {
    "timeout_seconds": (5.0, 300.0),
    "max_requests_per_run": (0, 100),
}


@dataclass(slots=True)
class AIConfig:
    """AI 辅助判定（可选）。

    只用于**语义层面**的判断：「这两份歌词是不是同一首」「哪一份更完整」。
    模型只给带理由的判断，动作仍由 Lyra 执行。
    """

    enabled: bool = False
    protocol: str = "openai_chat"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    """密钥。设置页保存的 token 会写入数据目录下的 ai-token（0600）。"""
    timeout_seconds: float = 30.0
    max_requests_per_run: int = 12
    """一轮最多问几次：既防超支，也防一次点击把上游打爆。"""
    auto_clean_duplicates: bool = False
    """高置信度的重复歌词是否自动移入回收站（可还原）。"""


@dataclass(slots=True)
class WebConfig:
    bind: str = "127.0.0.1"
    port: int = 8080
    auth_required: bool = True
    #: 管理员用户名与密码。
    #:
    #: 密码**有意不写进源码**：源码会被提交、可能公开，硬编码口令等于公开口令。
    #: 由配置文件（已在 .gitignore 中）或环境变量提供；两处都没有时才退化为
    #: 随机密码，并写入数据目录以免把自己锁在外面。
    admin_username: str = "admin"
    admin_password: str = ""


@dataclass(slots=True)
class WriteConfig:
    backup_before_write: bool = True
    backup_auto_prune: bool = False
    backup_max_bytes: int = 0
    backup_review_only: bool = True
    """只给「需要人工确认过」的写入做备份。

    为什么默认开：**自动接受**的写入是「时长吻合 + 分数达标 + 标题艺人达标」
    的结果，写错概率低；而每个文件都要整份复制一份（实际单文件 24~142MB，
    205 首就是十几 GB），代价极高、收益很低。默认不做。
    反过来，复核/手动选定的写入是人有疑虑才做的那一次，备份很值。

    置 false 即「凡是写入都备份」。
    """

    auto_clean_orphan_sidecars: bool = False
    """自动清理「确实多余」的孤儿 .lrc。

    只清理两类：内容与现有 .lrc 重复的、以及整个曲库里没有对应音频的。
    动作是移入回收站（保留相对路径、可还原），不是删除。
    默认关闭：这是会动用户文件的行为，应当显式打开。
    """


@dataclass(slots=True)
class Config:
    config_version: int = CONFIG_VERSION
    roots: list[RootConfig] = field(default_factory=list)
    storage: StorageConfig = field(default_factory=StorageConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    lyrics: LyricsFormatConfig = field(default_factory=LyricsFormatConfig)
    web: WebConfig = field(default_factory=WebConfig)
    write: WriteConfig = field(default_factory=WriteConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    source_path: Path | None = None
    """实际读取的配置文件路径；None 表示全部来自默认值与环境变量。"""

    # -- 便捷视图 ---------------------------------------------------------

    @property
    def enabled_roots(self) -> list[RootConfig]:
        return [r for r in self.roots if r.enabled]

    @property
    def writable_roots(self) -> list[RootConfig]:
        if not self.policy.write_enabled:
            return []
        return [r for r in self.enabled_roots if not r.read_only]

    def to_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data.pop("source_path", None)
        data["roots"] = [
            {**r, "role": str(r["role"])} for r in data.get("roots", [])
        ]
        if mask_secrets:
            for dotted in _SECRET_PATHS:
                _mask_in_place(data, dotted)
        return data


#: 设置页可修改的标签策略字段。
TAG_POLICY_FIELDS = (
    "auto_scan",
    "scan_interval_hours",
    "auto_write",
    "fingerprint",
    "max_auto_write_per_run",
    "max_duration_delta_seconds",
)

#: 取值范围。放宽到能覆盖实际使用，但拒绝 0 间隔、负上限这类危险值。
TAG_POLICY_BOUNDS: dict[str, tuple[float, float]] = {
    "scan_interval_hours": (0.5, 168.0),
    "max_auto_write_per_run": (0, 10000),
    "max_duration_delta_seconds": (0.0, 120.0),
}


def apply_tag_policy_values(cfg: Config, values: Mapping[str, Any]) -> TagPolicy:
    """把设置页/store 里的标签策略写进配置对象。

    只接受白名单字段并做范围校验；非法值直接抛 `ValueError`，不静默
    截断——设置页需要把错误原样显示给使用者。
    """
    if not isinstance(values, Mapping):
        raise ValueError("设置必须是对象")
    # 先校验到副本：中途遇到非法值不能把内存里的配置改一半。
    policy = replace(cfg.policy.tags)
    for key, raw in values.items():
        if key not in TAG_POLICY_FIELDS:
            raise ValueError(f"未知设置项：{key}")
        if key in ("auto_scan", "auto_write", "fingerprint"):
            if not isinstance(raw, bool):
                raise ValueError(f"{key} 必须是布尔值")
            setattr(policy, key, raw)
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{key} 必须是数字")
        low, high = TAG_POLICY_BOUNDS[key]
        value = int(raw) if key == "max_auto_write_per_run" else float(raw)
        if not (low <= value <= high):
            raise ValueError(f"{key} 必须在 {low} 到 {high} 之间")
        setattr(policy, key, value)
    cfg.policy.tags = policy
    return policy


def apply_ai_settings_values(cfg: Config, values: Mapping[str, Any]) -> AIConfig:
    """把设置页保存的非敏感 AI 配置写进配置对象。

    token 单独存放在数据目录，不进入这里，也不进入数据库 meta。
    """
    if not isinstance(values, Mapping):
        raise ValueError("设置必须是对象")
    ai = replace(cfg.ai)
    for key, raw in values.items():
        if key not in AI_SETTINGS_FIELDS:
            raise ValueError(f"未知设置项：{key}")
        if key in ("enabled", "auto_clean_duplicates"):
            if not isinstance(raw, bool):
                raise ValueError(f"{key} 必须是布尔值")
            setattr(ai, key, raw)
            continue
        if key == "protocol":
            if raw not in AI_PROTOCOLS:
                raise ValueError(f"不支持的协议：{raw}")
            ai.protocol = str(raw)
            continue
        if key == "base_url":
            url = str(raw or "").strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError("API 地址必须是 http:// 或 https://")
            ai.base_url = url.rstrip("/")
            continue
        if key == "model":
            model = str(raw or "").strip()
            if not model:
                raise ValueError("模型 ID 不能为空")
            ai.model = model
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{key} 必须是数字")
        low, high = AI_SETTINGS_BOUNDS[key]
        value = int(raw) if key == "max_requests_per_run" else float(raw)
        if not (low <= value <= high):
            raise ValueError(f"{key} 必须在 {low} 到 {high} 之间")
        setattr(ai, key, value)
    if ai.enabled and not ai.model.strip():
        raise ValueError("启用 AI 时必须填写模型 ID")
    cfg.ai = ai
    return ai


def _mask_in_place(data: dict[str, Any], dotted: str) -> None:
    node: Any = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    leaf = parts[-1]
    if isinstance(node, dict) and node.get(leaf):
        node[leaf] = "***"


# ---------------------------------------------------------------------------
# 校验工具
# ---------------------------------------------------------------------------


class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, msg: str) -> None:
        self.items.append(f"{where}: {msg}")

    def unknown_keys(self, where: str, table: Mapping[str, Any], allowed: set[str]) -> None:
        for key in table:
            if key not in allowed:
                close = _suggest(key, allowed)
                hint = f"（是否想写 {close}？）" if close else ""
                self.add(where, f"未知配置键 {key!r}{hint}")

    def raise_if_any(self) -> None:
        if self.items:
            raise ConfigError(self.items)


def _suggest(key: str, allowed: set[str]) -> str | None:
    import difflib

    matches = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.7)
    return matches[0] if matches else None


def _as_bool(problems: _Problems, where: str, value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    problems.add(where, f"应为布尔值，实际是 {type(value).__name__}")
    return default


def _as_int(problems: _Problems, where: str, value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        problems.add(where, f"应为整数，实际是 {type(value).__name__}")
        return default
    return value


def _as_float(problems: _Problems, where: str, value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.add(where, f"应为数字，实际是 {type(value).__name__}")
        return default
    return float(value)


def _as_str(problems: _Problems, where: str, value: Any, default: str) -> str:
    if not isinstance(value, str):
        problems.add(where, f"应为字符串，实际是 {type(value).__name__}")
        return default
    return value


def _as_str_list(problems: _Problems, where: str, value: Any, default: list[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        problems.add(where, "应为字符串数组")
        return list(default)
    return list(value)


def _as_int_list(problems: _Problems, where: str, value: Any, default: list[int]) -> list[int]:
    if not isinstance(value, list) or any(
        isinstance(v, bool) or not isinstance(v, int) for v in value
    ):
        problems.add(where, "应为整数数组")
        return list(default)
    return list(value)


# 允许的表结构
_ROOT_KEYS = {
    "name", "role", "path", "host_path", "read_only", "enabled",
    "ignore_dirs", "ignore_globs",
}
_STORAGE_KEYS = {"data_dir"}
_WATCH_KEYS = {
    "backend", "stable_seconds", "stable_samples", "reconcile_interval_seconds",
    "full_scan_local_time", "timezone",
}
_PROVIDERS_KEYS = {
    "enabled", "qq", "lrclib", "netease", "musicbrainz", "acoustid",
    "priority_zh", "priority_foreign", "cjk_uses_zh_priority",
}
_QQ_KEYS = {
    "minimum_request_interval_seconds", "query_limit_per_round", "candidates_per_query",
    "detail_and_lyric_limit_per_round", "request_timeout_seconds", "search_timeout_seconds",
    "max_inline_retries", "max_inline_retry_wait_seconds",
}
_LRCLIB_KEYS = {
    "client_name", "client_version", "client_url", "authorization_ref",
    "minimum_request_interval_seconds", "max_backoff_seconds",
    "circuit_breaker_threshold", "circuit_breaker_initial_seconds", "request_timeout_seconds",
    "max_inline_retries", "max_inline_retry_wait_seconds",
}
_NETEASE_KEYS = {
    "base_url",
    "minimum_request_interval_seconds", "query_limit_per_round", "candidates_per_query",
    "detail_and_lyric_limit_per_round", "request_timeout_seconds",
    "max_inline_retries", "max_inline_retry_wait_seconds",
}
_MUSICBRAINZ_KEYS = {
    "enabled", "base_url", "request_timeout_seconds",
    "minimum_request_interval_seconds", "search_limit", "detail_limit",
}
_ACOUSTID_KEYS = {
    "enabled", "api_key", "base_url", "request_timeout_seconds",
    "minimum_request_interval_seconds",
}
_POLICY_KEYS = {
    "write_enabled", "write_mode", "write_sidecar_lrc", "embed_lyrics", "fill_empty_lrc",
    "skip_if_valid_synced", "clear_plain_embedded", "allow_other_providers", "matching",
    "tags",
}
_TAG_KEYS = {
    "auto_scan", "scan_interval_hours", "auto_write", "fingerprint",
    "max_auto_write_per_run", "max_duration_delta_seconds",
}
_MATCHING_KEYS = {
    "max_duration_delta_seconds", "min_score_margin", "min_confidence",
    "auto_accept_within_seconds", "auto_accept_min_confidence",
    "auto_accept_relaxed_seconds", "auto_accept_relaxed_confidence",
    "auto_accept_wide_confidence",
    "auto_accept_min_title_score", "auto_accept_min_artist_score",
}
_RETRY_KEYS = {"not_found_intervals_days", "no_synced_intervals_days", "transient_backoff_seconds"}
_LYRICS_KEYS = {"encoding", "bom", "line_ending", "format", "translation", "romanization"}
_WEB_KEYS = {"bind", "port", "auth_required", "admin_username", "admin_password"}
_WRITE_KEYS = {
    "backup_before_write", "backup_auto_prune", "backup_max_bytes",
    "backup_review_only", "auto_clean_orphan_sidecars",
}
_AI_KEYS = {
    "enabled", "protocol", "base_url", "model", "api_key",
    "timeout_seconds", "max_requests_per_run", "auto_clean_duplicates",
}
_TOP_KEYS = {
    "config_version", "roots", "storage", "watch", "providers",
    "policy", "retry", "lyrics", "web", "write", "ai",
}

_WRITE_MODES = {"auto", "verified_only", "off"}
_WATCH_BACKENDS = {"auto", "inotify", "fsevents", "poll"}
_LYRIC_FORMATS = {"line_lrc"}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def load_config(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Config:
    """加载并校验配置。

    path 为 None 时尝试环境变量 LYRA_CONFIG，再尝试当前目录 lyra.toml；
    两者都不存在则使用纯默认值（便于 `lyra config check` 与测试）。
    """
    env = dict(os.environ if env is None else env)
    problems = _Problems()
    cfg = Config()

    resolved = _resolve_config_path(path, env)
    raw: dict[str, Any] = {}
    if resolved is not None:
        try:
            with resolved.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError([f"{resolved}: TOML 语法错误: {exc}"]) from exc
        except OSError as exc:
            raise ConfigError([f"{resolved}: 无法读取: {exc}"]) from exc
        cfg.source_path = resolved

    _parse_top(problems, cfg, raw)
    _apply_env(problems, cfg, env)
    _parse_ai(problems, cfg, raw)
    _validate_semantics(problems, cfg)
    problems.raise_if_any()
    return cfg


def _resolve_config_path(path: str | Path | None, env: Mapping[str, str]) -> Path | None:
    if path is not None:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ConfigError([f"配置文件不存在: {p}"])
        return p
    from_env = env.get("LYRA_CONFIG")
    if from_env:
        p = Path(from_env).expanduser()
        if not p.is_file():
            raise ConfigError([f"LYRA_CONFIG 指向的文件不存在: {p}"])
        return p
    default = Path.cwd() / DEFAULT_CONFIG_FILENAME
    return default if default.is_file() else None


def _parse_top(problems: _Problems, cfg: Config, raw: Mapping[str, Any]) -> None:
    problems.unknown_keys("顶层", raw, _TOP_KEYS)

    if "config_version" in raw:
        version = _as_int(problems, "config_version", raw["config_version"], CONFIG_VERSION)
        if version != CONFIG_VERSION:
            problems.add(
                "config_version",
                f"不支持版本 {version}（本程序支持 {CONFIG_VERSION}）",
            )
        cfg.config_version = version

    # ---- roots ----
    if "roots" in raw:
        raw_roots = raw["roots"]
        if not isinstance(raw_roots, list) or not raw_roots:
            problems.add("roots", "应为非空的表数组 [[roots]]")
        else:
            parsed: list[RootConfig] = []
            seen_names: set[str] = set()
            for idx, item in enumerate(raw_roots):
                where = f"roots[{idx}]"
                if not isinstance(item, dict):
                    problems.add(where, "应为表")
                    continue
                problems.unknown_keys(where, item, _ROOT_KEYS)
                name = _as_str(problems, f"{where}.name", item.get("name", f"root{idx}"), f"root{idx}")
                if name in seen_names:
                    problems.add(f"{where}.name", f"根名称重复: {name!r}")
                seen_names.add(name)

                role_raw = _as_str(problems, f"{where}.role", item.get("role", "library"), "library")
                try:
                    role = RootRole(role_raw)
                except ValueError:
                    problems.add(
                        f"{where}.role",
                        f"未知角色 {role_raw!r}（可选: {', '.join(r.value for r in RootRole)}）",
                    )
                    role = RootRole.LIBRARY

                root_path = _as_str(problems, f"{where}.path", item.get("path", ""), "")
                if not root_path:
                    problems.add(f"{where}.path", "必须提供受管根的路径")

                parsed.append(
                    RootConfig(
                        name=name,
                        role=role,
                        path=root_path,
                        host_path=_as_str(
                            problems, f"{where}.host_path", item.get("host_path", ""), ""
                        ),
                        read_only=_as_bool(
                            problems, f"{where}.read_only", item.get("read_only", False), False
                        ),
                        enabled=_as_bool(
                            problems, f"{where}.enabled", item.get("enabled", True), True
                        ),
                        ignore_dirs=_as_str_list(
                            problems, f"{where}.ignore_dirs", item.get("ignore_dirs", []), []
                        ),
                        ignore_globs=_as_str_list(
                            problems, f"{where}.ignore_globs", item.get("ignore_globs", []), []
                        ),
                    )
                )
            cfg.roots = parsed
    else:
        problems.add("roots", "缺少 [[roots]]。至少需要一个受管根。")

    # ---- storage ----
    if "storage" in raw:
        table = _unwrap(problems, "storage", raw["storage"])
        problems.unknown_keys("storage", table, _STORAGE_KEYS)
        cfg.storage = StorageConfig(
            data_dir=_as_str(
                problems, "storage.data_dir", table.get("data_dir", "./data"), "./data"
            )
        )

    # ---- watch ----
    if "watch" in raw:
        table = _unwrap(problems, "watch", raw["watch"])
        problems.unknown_keys("watch", table, _WATCH_KEYS)
        backend = _as_str(problems, "watch.backend", table.get("backend", "auto"), "auto")
        if backend not in _WATCH_BACKENDS:
            problems.add(
                "watch.backend", f"未知后端 {backend!r}（可选: {', '.join(sorted(_WATCH_BACKENDS))}）"
            )
        stable_seconds = _as_int(
            problems, "watch.stable_seconds", table.get("stable_seconds", 60), 60
        )
        if stable_seconds < 0:
            problems.add("watch.stable_seconds", "不能为负")
        stable_samples = _as_int(
            problems, "watch.stable_samples", table.get("stable_samples", 3), 3
        )
        if stable_samples < 1:
            problems.add("watch.stable_samples", "至少为 1")
        cfg.watch = WatchConfig(
            backend=backend,
            stable_seconds=stable_seconds,
            stable_samples=stable_samples,
            reconcile_interval_seconds=_as_int(
                problems,
                "watch.reconcile_interval_seconds",
                table.get("reconcile_interval_seconds", 900),
                900,
            ),
            full_scan_local_time=_as_str(
                problems,
                "watch.full_scan_local_time",
                table.get("full_scan_local_time", "03:30"),
                "03:30",
            ),
            timezone=_as_str(
                problems, "watch.timezone", table.get("timezone", "Asia/Shanghai"), "Asia/Shanghai"
            ),
        )

    # ---- providers ----
    if "providers" in raw:
        table = _unwrap(problems, "providers", raw["providers"])
        problems.unknown_keys("providers", table, _PROVIDERS_KEYS)
        providers = ProvidersConfig()

        if "enabled" in table:
            enabled = _as_str_list(
                problems, "providers.enabled", table["enabled"], providers.enabled
            )
            known = {"qq", "lrclib", "netease"}
            for name in enabled:
                if name not in known:
                    problems.add(
                        "providers.enabled",
                        f"未知来源 {name!r}（当前已实现: {', '.join(sorted(known))}）",
                    )
            providers.enabled = enabled

        for key, attr in (
            ("priority_zh", "priority_zh"),
            ("priority_foreign", "priority_foreign"),
        ):
            if key in table:
                values = _as_str_list(
                    problems, f"providers.{key}", table[key], getattr(providers, attr)
                )
                for name in values:
                    if name not in known:
                        problems.add(
                            f"providers.{key}",
                            f"未知来源 {name!r}（当前已实现: {', '.join(sorted(known))}）",
                        )
                setattr(providers, attr, values)

        if "cjk_uses_zh_priority" in table:
            providers.cjk_uses_zh_priority = _as_bool(
                problems, "providers.cjk_uses_zh_priority",
                table["cjk_uses_zh_priority"], providers.cjk_uses_zh_priority,
            )

        if "musicbrainz" in table:
            mb_t = _unwrap(problems, "providers.musicbrainz", table["musicbrainz"])
            problems.unknown_keys("providers.musicbrainz", mb_t, _MUSICBRAINZ_KEYS)
            dmb = MusicBrainzProviderConfig()
            providers.musicbrainz = MusicBrainzProviderConfig(
                enabled=_as_bool(problems, "providers.musicbrainz.enabled",
                                 mb_t.get("enabled", dmb.enabled), dmb.enabled),
                base_url=_as_str(problems, "providers.musicbrainz.base_url",
                                 mb_t.get("base_url", dmb.base_url), dmb.base_url),
                request_timeout_seconds=_as_float(
                    problems, "providers.musicbrainz.request_timeout_seconds",
                    mb_t.get("request_timeout_seconds", dmb.request_timeout_seconds),
                    dmb.request_timeout_seconds),
                minimum_request_interval_seconds=_as_float(
                    problems, "providers.musicbrainz.minimum_request_interval_seconds",
                    mb_t.get("minimum_request_interval_seconds",
                             dmb.minimum_request_interval_seconds),
                    dmb.minimum_request_interval_seconds),
                search_limit=_as_int(problems, "providers.musicbrainz.search_limit",
                                     mb_t.get("search_limit", dmb.search_limit),
                                     dmb.search_limit),
                detail_limit=_as_int(problems, "providers.musicbrainz.detail_limit",
                                     mb_t.get("detail_limit", dmb.detail_limit),
                                     dmb.detail_limit),
            )

        if "netease" in table:
            ne_t = _unwrap(problems, "providers.netease", table["netease"])
            problems.unknown_keys("providers.netease", ne_t, _NETEASE_KEYS)
            dn = NetEaseProviderConfig()
            providers.netease = NetEaseProviderConfig(
                base_url=_as_str(
                    problems, "providers.netease.base_url",
                    ne_t.get("base_url", dn.base_url), dn.base_url,
                ),
                minimum_request_interval_seconds=_as_float(
                    problems, "providers.netease.minimum_request_interval_seconds",
                    ne_t.get("minimum_request_interval_seconds",
                             dn.minimum_request_interval_seconds),
                    dn.minimum_request_interval_seconds,
                ),
                query_limit_per_round=_as_int(
                    problems, "providers.netease.query_limit_per_round",
                    ne_t.get("query_limit_per_round", dn.query_limit_per_round),
                    dn.query_limit_per_round,
                ),
                candidates_per_query=_as_int(
                    problems, "providers.netease.candidates_per_query",
                    ne_t.get("candidates_per_query", dn.candidates_per_query),
                    dn.candidates_per_query,
                ),
                detail_and_lyric_limit_per_round=_as_int(
                    problems, "providers.netease.detail_and_lyric_limit_per_round",
                    ne_t.get("detail_and_lyric_limit_per_round",
                             dn.detail_and_lyric_limit_per_round),
                    dn.detail_and_lyric_limit_per_round,
                ),
                request_timeout_seconds=_as_float(
                    problems, "providers.netease.request_timeout_seconds",
                    ne_t.get("request_timeout_seconds", dn.request_timeout_seconds),
                    dn.request_timeout_seconds,
                ),
                max_inline_retries=_as_int(
                    problems, "providers.netease.max_inline_retries",
                    ne_t.get("max_inline_retries", dn.max_inline_retries),
                    dn.max_inline_retries,
                ),
                max_inline_retry_wait_seconds=_as_float(
                    problems, "providers.netease.max_inline_retry_wait_seconds",
                    ne_t.get("max_inline_retry_wait_seconds",
                             dn.max_inline_retry_wait_seconds),
                    dn.max_inline_retry_wait_seconds,
                ),
            )

        if "qq" in table:
            qq_t = _unwrap(problems, "providers.qq", table["qq"])
            problems.unknown_keys("providers.qq", qq_t, _QQ_KEYS)
            d = QQProviderConfig()
            providers.qq = QQProviderConfig(
                minimum_request_interval_seconds=_as_float(
                    problems,
                    "providers.qq.minimum_request_interval_seconds",
                    qq_t.get("minimum_request_interval_seconds", d.minimum_request_interval_seconds),
                    d.minimum_request_interval_seconds,
                ),
                query_limit_per_round=_as_int(
                    problems,
                    "providers.qq.query_limit_per_round",
                    qq_t.get("query_limit_per_round", d.query_limit_per_round),
                    d.query_limit_per_round,
                ),
                candidates_per_query=_as_int(
                    problems,
                    "providers.qq.candidates_per_query",
                    qq_t.get("candidates_per_query", d.candidates_per_query),
                    d.candidates_per_query,
                ),
                detail_and_lyric_limit_per_round=_as_int(
                    problems,
                    "providers.qq.detail_and_lyric_limit_per_round",
                    qq_t.get(
                        "detail_and_lyric_limit_per_round",
                        d.detail_and_lyric_limit_per_round,
                    ),
                    d.detail_and_lyric_limit_per_round,
                ),
                request_timeout_seconds=_as_float(
                    problems,
                    "providers.qq.request_timeout_seconds",
                    qq_t.get("request_timeout_seconds", d.request_timeout_seconds),
                    d.request_timeout_seconds,
                ),
                search_timeout_seconds=_as_float(
                    problems,
                    "providers.qq.search_timeout_seconds",
                    qq_t.get("search_timeout_seconds", d.search_timeout_seconds),
                    d.search_timeout_seconds,
                ),
                max_inline_retries=_as_int(
                    problems,
                    "providers.qq.max_inline_retries",
                    qq_t.get("max_inline_retries", d.max_inline_retries),
                    d.max_inline_retries,
                ),
                max_inline_retry_wait_seconds=_as_float(
                    problems,
                    "providers.qq.max_inline_retry_wait_seconds",
                    qq_t.get(
                        "max_inline_retry_wait_seconds", d.max_inline_retry_wait_seconds
                    ),
                    d.max_inline_retry_wait_seconds,
                ),
            )

        if "lrclib" in table:
            lr_t = _unwrap(problems, "providers.lrclib", table["lrclib"])
            problems.unknown_keys("providers.lrclib", lr_t, _LRCLIB_KEYS)
            d2 = LrcLibProviderConfig()
            providers.lrclib = LrcLibProviderConfig(
                client_name=_as_str(
                    problems, "providers.lrclib.client_name",
                    lr_t.get("client_name", d2.client_name), d2.client_name,
                ),
                client_version=_as_str(
                    problems, "providers.lrclib.client_version",
                    lr_t.get("client_version", d2.client_version), d2.client_version,
                ),
                client_url=_as_str(
                    problems, "providers.lrclib.client_url",
                    lr_t.get("client_url", d2.client_url), d2.client_url,
                ),
                authorization_ref=_as_str(
                    problems, "providers.lrclib.authorization_ref",
                    lr_t.get("authorization_ref", d2.authorization_ref), d2.authorization_ref,
                ),
                minimum_request_interval_seconds=_as_float(
                    problems, "providers.lrclib.minimum_request_interval_seconds",
                    lr_t.get("minimum_request_interval_seconds",
                             d2.minimum_request_interval_seconds),
                    d2.minimum_request_interval_seconds,
                ),
                max_backoff_seconds=_as_int(
                    problems, "providers.lrclib.max_backoff_seconds",
                    lr_t.get("max_backoff_seconds", d2.max_backoff_seconds),
                    d2.max_backoff_seconds,
                ),
                circuit_breaker_threshold=_as_int(
                    problems, "providers.lrclib.circuit_breaker_threshold",
                    lr_t.get("circuit_breaker_threshold", d2.circuit_breaker_threshold),
                    d2.circuit_breaker_threshold,
                ),
                circuit_breaker_initial_seconds=_as_int(
                    problems, "providers.lrclib.circuit_breaker_initial_seconds",
                    lr_t.get("circuit_breaker_initial_seconds",
                             d2.circuit_breaker_initial_seconds),
                    d2.circuit_breaker_initial_seconds,
                ),
                request_timeout_seconds=_as_float(
                    problems, "providers.lrclib.request_timeout_seconds",
                    lr_t.get("request_timeout_seconds", d2.request_timeout_seconds),
                    d2.request_timeout_seconds,
                ),
                max_inline_retries=_as_int(
                    problems, "providers.lrclib.max_inline_retries",
                    lr_t.get("max_inline_retries", d2.max_inline_retries),
                    d2.max_inline_retries,
                ),
                max_inline_retry_wait_seconds=_as_float(
                    problems, "providers.lrclib.max_inline_retry_wait_seconds",
                    lr_t.get(
                        "max_inline_retry_wait_seconds",
                        d2.max_inline_retry_wait_seconds,
                    ),
                    d2.max_inline_retry_wait_seconds,
                ),
            )

        if "acoustid" in table:
            ac_t = _unwrap(problems, "providers.acoustid", table["acoustid"])
            problems.unknown_keys("providers.acoustid", ac_t, _ACOUSTID_KEYS)
            d3 = AcoustIdProviderConfig()
            providers.acoustid = AcoustIdProviderConfig(
                enabled=_as_bool(
                    problems, "providers.acoustid.enabled",
                    ac_t.get("enabled", d3.enabled), d3.enabled,
                ),
                api_key=_as_str(
                    problems, "providers.acoustid.api_key",
                    ac_t.get("api_key", d3.api_key), d3.api_key,
                ),
                base_url=_as_str(
                    problems, "providers.acoustid.base_url",
                    ac_t.get("base_url", d3.base_url), d3.base_url,
                ),
                request_timeout_seconds=_as_float(
                    problems, "providers.acoustid.request_timeout_seconds",
                    ac_t.get("request_timeout_seconds", d3.request_timeout_seconds),
                    d3.request_timeout_seconds,
                ),
                minimum_request_interval_seconds=_as_float(
                    problems, "providers.acoustid.minimum_request_interval_seconds",
                    ac_t.get(
                        "minimum_request_interval_seconds",
                        d3.minimum_request_interval_seconds,
                    ),
                    d3.minimum_request_interval_seconds,
                ),
            )
        cfg.providers = providers

    # ---- policy ----
    if "policy" in raw:
        table = _unwrap(problems, "policy", raw["policy"])
        problems.unknown_keys("policy", table, _POLICY_KEYS)
        mode = _as_str(problems, "policy.write_mode", table.get("write_mode", "auto"), "auto")
        if mode not in _WRITE_MODES:
            problems.add(
                "policy.write_mode",
                f"未知模式 {mode!r}（可选: {', '.join(sorted(_WRITE_MODES))}）",
            )
        policy = PolicyConfig(
            write_enabled=_as_bool(
                problems, "policy.write_enabled", table.get("write_enabled", True), True
            ),
            write_mode=mode,
            write_sidecar_lrc=_as_bool(
                problems, "policy.write_sidecar_lrc", table.get("write_sidecar_lrc", True), True
            ),
            embed_lyrics=_as_bool(
                problems, "policy.embed_lyrics", table.get("embed_lyrics", True), True
            ),
            fill_empty_lrc=_as_bool(
                problems, "policy.fill_empty_lrc", table.get("fill_empty_lrc", True), True
            ),
            skip_if_valid_synced=_as_bool(
                problems,
                "policy.skip_if_valid_synced",
                table.get("skip_if_valid_synced", True),
                True,
            ),
            clear_plain_embedded=_as_bool(
                problems,
                "policy.clear_plain_embedded",
                table.get("clear_plain_embedded", True),
                True,
            ),
            allow_other_providers=_as_bool(
                problems,
                "policy.allow_other_providers",
                table.get("allow_other_providers", False),
                False,
            ),
        )
        if "tags" in table:
            t_t = _unwrap(problems, "policy.tags", table["tags"])
            problems.unknown_keys("policy.tags", t_t, _TAG_KEYS)
            dt = TagPolicy()
            policy.tags = TagPolicy(
                auto_scan=_as_bool(
                    problems, "policy.tags.auto_scan",
                    t_t.get("auto_scan", dt.auto_scan), dt.auto_scan,
                ),
                scan_interval_hours=_as_float(
                    problems, "policy.tags.scan_interval_hours",
                    t_t.get("scan_interval_hours", dt.scan_interval_hours),
                    dt.scan_interval_hours,
                ),
                auto_write=_as_bool(
                    problems, "policy.tags.auto_write",
                    t_t.get("auto_write", dt.auto_write), dt.auto_write,
                ),
                fingerprint=_as_bool(
                    problems, "policy.tags.fingerprint",
                    t_t.get("fingerprint", dt.fingerprint), dt.fingerprint,
                ),
                max_auto_write_per_run=_as_int(
                    problems, "policy.tags.max_auto_write_per_run",
                    t_t.get("max_auto_write_per_run", dt.max_auto_write_per_run),
                    dt.max_auto_write_per_run,
                ),
                max_duration_delta_seconds=_as_float(
                    problems, "policy.tags.max_duration_delta_seconds",
                    t_t.get(
                        "max_duration_delta_seconds", dt.max_duration_delta_seconds
                    ),
                    dt.max_duration_delta_seconds,
                ),
            )
        if "matching" in table:
            m_t = _unwrap(problems, "policy.matching", table["matching"])
            problems.unknown_keys("policy.matching", m_t, _MATCHING_KEYS)
            dm = MatchingPolicy()
            policy.matching = MatchingPolicy(
                max_duration_delta_seconds=_as_float(
                    problems, "policy.matching.max_duration_delta_seconds",
                    m_t.get("max_duration_delta_seconds", dm.max_duration_delta_seconds),
                    dm.max_duration_delta_seconds,
                ),
                min_score_margin=_as_float(
                    problems, "policy.matching.min_score_margin",
                    m_t.get("min_score_margin", dm.min_score_margin), dm.min_score_margin,
                ),
                min_confidence=_as_float(
                    problems, "policy.matching.min_confidence",
                    m_t.get("min_confidence", dm.min_confidence), dm.min_confidence,
                ),
                auto_accept_within_seconds=_as_float(
                    problems, "policy.matching.auto_accept_within_seconds",
                    m_t.get(
                        "auto_accept_within_seconds", dm.auto_accept_within_seconds
                    ),
                    dm.auto_accept_within_seconds,
                ),
                auto_accept_min_confidence=_as_float(
                    problems, "policy.matching.auto_accept_min_confidence",
                    m_t.get("auto_accept_min_confidence",
                            dm.auto_accept_min_confidence),
                    dm.auto_accept_min_confidence,
                ),
                auto_accept_relaxed_seconds=_as_float(
                    problems, "policy.matching.auto_accept_relaxed_seconds",
                    m_t.get("auto_accept_relaxed_seconds",
                            dm.auto_accept_relaxed_seconds),
                    dm.auto_accept_relaxed_seconds,
                ),
                auto_accept_relaxed_confidence=_as_float(
                    problems, "policy.matching.auto_accept_relaxed_confidence",
                    m_t.get("auto_accept_relaxed_confidence",
                            dm.auto_accept_relaxed_confidence),
                    dm.auto_accept_relaxed_confidence,
                ),
                auto_accept_wide_confidence=_as_float(
                    problems, "policy.matching.auto_accept_wide_confidence",
                    m_t.get("auto_accept_wide_confidence",
                            dm.auto_accept_wide_confidence),
                    dm.auto_accept_wide_confidence,
                ),
                auto_accept_min_title_score=_as_float(
                    problems, "policy.matching.auto_accept_min_title_score",
                    m_t.get("auto_accept_min_title_score",
                            dm.auto_accept_min_title_score),
                    dm.auto_accept_min_title_score,
                ),
                auto_accept_min_artist_score=_as_float(
                    problems, "policy.matching.auto_accept_min_artist_score",
                    m_t.get("auto_accept_min_artist_score",
                            dm.auto_accept_min_artist_score),
                    dm.auto_accept_min_artist_score,
                ),
            )
        cfg.policy = policy

    # ---- retry ----
    if "retry" in raw:
        table = _unwrap(problems, "retry", raw["retry"])
        problems.unknown_keys("retry", table, _RETRY_KEYS)
        dr = RetryConfig()
        cfg.retry = RetryConfig(
            not_found_intervals_days=_as_int_list(
                problems, "retry.not_found_intervals_days",
                table.get("not_found_intervals_days", dr.not_found_intervals_days),
                dr.not_found_intervals_days,
            ),
            no_synced_intervals_days=_as_int_list(
                problems, "retry.no_synced_intervals_days",
                table.get("no_synced_intervals_days", dr.no_synced_intervals_days),
                dr.no_synced_intervals_days,
            ),
            transient_backoff_seconds=_as_int_list(
                problems, "retry.transient_backoff_seconds",
                table.get("transient_backoff_seconds", dr.transient_backoff_seconds),
                dr.transient_backoff_seconds,
            ),
        )

    # ---- lyrics ----
    if "lyrics" in raw:
        table = _unwrap(problems, "lyrics", raw["lyrics"])
        problems.unknown_keys("lyrics", table, _LYRICS_KEYS)
        dl = LyricsFormatConfig()
        fmt = _as_str(problems, "lyrics.format", table.get("format", dl.format), dl.format)
        if fmt not in _LYRIC_FORMATS:
            problems.add(
                "lyrics.format", f"未知格式 {fmt!r}（可选: {', '.join(sorted(_LYRIC_FORMATS))}）"
            )
        cfg.lyrics = LyricsFormatConfig(
            encoding=_as_str(problems, "lyrics.encoding", table.get("encoding", dl.encoding),
                             dl.encoding),
            bom=_as_bool(problems, "lyrics.bom", table.get("bom", dl.bom), dl.bom),
            line_ending=_as_str(problems, "lyrics.line_ending",
                                table.get("line_ending", dl.line_ending), dl.line_ending),
            format=fmt,
            translation=_as_bool(problems, "lyrics.translation",
                                 table.get("translation", dl.translation), dl.translation),
            romanization=_as_bool(problems, "lyrics.romanization",
                                  table.get("romanization", dl.romanization), dl.romanization),
        )

    # ---- web ----
    if "web" in raw:
        table = _unwrap(problems, "web", raw["web"])
        problems.unknown_keys("web", table, _WEB_KEYS)
        dw = WebConfig()
        port = _as_int(problems, "web.port", table.get("port", dw.port), dw.port)
        if not (1 <= port <= 65535):
            problems.add("web.port", f"端口越界: {port}")
        cfg.web = WebConfig(
            bind=_as_str(problems, "web.bind", table.get("bind", dw.bind), dw.bind),
            port=port,
            auth_required=_as_bool(problems, "web.auth_required",
                                   table.get("auth_required", dw.auth_required), dw.auth_required),
            admin_username=_as_str(
                problems, "web.admin_username",
                table.get("admin_username", dw.admin_username), dw.admin_username,
            ),
            admin_password=_as_str(
                problems, "web.admin_password",
                table.get("admin_password", dw.admin_password), dw.admin_password,
            ),
        )

    # ---- write ----
    if "write" in raw:
        table = _unwrap(problems, "write", raw["write"])
        problems.unknown_keys("write", table, _WRITE_KEYS)
        dww = WriteConfig()
        cfg.write = WriteConfig(
            backup_before_write=_as_bool(
                problems, "write.backup_before_write",
                table.get("backup_before_write", dww.backup_before_write),
                dww.backup_before_write,
            ),
            backup_auto_prune=_as_bool(
                problems, "write.backup_auto_prune",
                table.get("backup_auto_prune", dww.backup_auto_prune),
                dww.backup_auto_prune,
            ),
            backup_max_bytes=_as_int(
                problems, "write.backup_max_bytes",
                table.get("backup_max_bytes", dww.backup_max_bytes), dww.backup_max_bytes,
            ),
            backup_review_only=_as_bool(
                problems, "write.backup_review_only",
                table.get("backup_review_only", dww.backup_review_only),
                dww.backup_review_only,
            ),
            auto_clean_orphan_sidecars=_as_bool(
                problems, "write.auto_clean_orphan_sidecars",
                table.get(
                    "auto_clean_orphan_sidecars", dww.auto_clean_orphan_sidecars),
                dww.auto_clean_orphan_sidecars,
            ),
        )


def _parse_ai(problems: _Problems, cfg: Config, raw: Mapping[str, Any]) -> None:
    if "ai" not in raw:
        return
    table = _unwrap(problems, "ai", raw["ai"])
    problems.unknown_keys("ai", table, _AI_KEYS)
    base = cfg.ai
    cfg.ai = AIConfig(
        enabled=_as_bool(problems, "ai.enabled", table.get("enabled", base.enabled), base.enabled),
        protocol=_as_str(
            problems, "ai.protocol", table.get("protocol", base.protocol), base.protocol
        ),
        base_url=_as_str(problems, "ai.base_url", table.get("base_url", base.base_url), base.base_url),
        model=_as_str(problems, "ai.model", table.get("model", base.model), base.model),
        api_key=_as_str(problems, "ai.api_key", table.get("api_key", base.api_key), base.api_key),
        timeout_seconds=_as_float(
            problems, "ai.timeout_seconds",
            table.get("timeout_seconds", base.timeout_seconds), base.timeout_seconds),
        max_requests_per_run=_as_int(
            problems, "ai.max_requests_per_run",
            table.get("max_requests_per_run", base.max_requests_per_run),
            base.max_requests_per_run),
        auto_clean_duplicates=_as_bool(
            problems, "ai.auto_clean_duplicates",
            table.get("auto_clean_duplicates", base.auto_clean_duplicates),
            base.auto_clean_duplicates),
    )
    if cfg.ai.protocol not in AI_PROTOCOLS:
        problems.add("ai.protocol", f"不支持的协议：{cfg.ai.protocol}")
    if not cfg.ai.base_url.startswith(("http://", "https://")):
        problems.add("ai.base_url", "API 地址必须以 http:// 或 https:// 开头")
    if not cfg.ai.model.strip():
        problems.add("ai.model", "模型 ID 不能为空")


def _unwrap(problems: _Problems, where: str, value: Any) -> Mapping[str, Any]:
    if isinstance(value, dict):
        return value
    problems.add(where, f"应为表，实际是 {type(value).__name__}")
    return {}


# ---------------------------------------------------------------------------
# 环境变量覆盖
# ---------------------------------------------------------------------------


def _apply_env(problems: _Problems, cfg: Config, env: Mapping[str, str]) -> None:
    def flag(key: str) -> bool | None:
        raw = env.get(key)
        if raw is None:
            return None
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        problems.add(key, f"无法解析为布尔值: {raw!r}")
        return None

    if (v := env.get("LYRA_DATA_DIR")) :
        cfg.storage.data_dir = v
    if (v := flag("LYRA_WRITE_ENABLED")) is not None:
        cfg.policy.write_enabled = v
    if (v := env.get("LYRA_LIBRARY_ROOT")):
        if not cfg.roots:
            cfg.roots = [RootConfig(name="main", path=v, host_path=v)]
        else:
            cfg.roots[0].path = v
    if (v := env.get("LYRA_LRCLIB_AUTHORIZATION_REF")):
        cfg.providers.lrclib.authorization_ref = v
    if (v := env.get("LYRA_ACOUSTID_API_KEY")):
        cfg.providers.acoustid.api_key = v
        cfg.providers.acoustid.enabled = True
    if (v := env.get("LYRA_WEB_PORT")):
        try:
            cfg.web.port = int(v)
        except ValueError:
            problems.add("LYRA_WEB_PORT", f"无法解析为整数: {v!r}")
    if (v := env.get("LYRA_WEB_BIND")):
        cfg.web.bind = v
    if (v := env.get("LYRA_ADMIN_USERNAME")):
        cfg.web.admin_username = v
    if (v := env.get("LYRA_ADMIN_PASSWORD")):
        cfg.web.admin_password = v
    if (v := env.get("LYRA_AI_API_KEY")):
        cfg.ai.api_key = v
        cfg.ai.enabled = True


# ---------------------------------------------------------------------------
# 语义校验
# ---------------------------------------------------------------------------

_SUPPORTED_PROVIDERS = {"qq", "lrclib", "netease"}


def _validate_semantics(problems: _Problems, cfg: Config) -> None:
    if not cfg.roots:
        problems.add("roots", "至少需要一个受管根")

    # 数据目录不得位于任何受管根内部：备份/缓存/数据库不能污染曲库
    if cfg.roots and cfg.storage.data_dir:
        data_dir = _safe_resolve(cfg.storage.resolved_data_dir)
        for root in cfg.roots:
            if not root.path:
                continue
            root_path = _safe_resolve(root.resolved_path)
            if _is_within(data_dir, root_path):
                problems.add(
                    "storage.data_dir",
                    f"数据目录 {data_dir} 位于受管根 {root_path} 内部。"
                    "备份、缓存、数据库必须放在曲库之外。",
                )

    # 根路径重复
    seen: dict[str, str] = {}
    for root in cfg.roots:
        key = str(_safe_resolve(root.resolved_path)) if root.path else ""
        if not key:
            continue
        if key in seen:
            problems.add("roots", f"根路径重复: {root.path}（{seen[key]} 与 {root.name}）")
        seen[key] = root.name

    # 未实现来源
    if not cfg.policy.allow_other_providers:
        for name in cfg.providers.enabled:
            if name not in _SUPPORTED_PROVIDERS:
                problems.add(
                    "providers.enabled",
                    f"来源 {name!r} 未启用（allow_other_providers=false）。"
                    "不允许静默使用未声明的来源。",
                )
    if not cfg.providers.enabled:
        problems.add("providers.enabled", "至少需要启用一个来源")

    # 歌词输出：两者都关掉等于什么也不做
    if not cfg.policy.write_sidecar_lrc and not cfg.policy.embed_lyrics:
        problems.add(
            "policy",
            "write_sidecar_lrc 与 embed_lyrics 同时为 false，将不会输出任何歌词",
        )

    # 负值
    if cfg.watch.reconcile_interval_seconds < 0:
        problems.add("watch.reconcile_interval_seconds", "不能为负")
    if any(d <= 0 for d in cfg.retry.not_found_intervals_days):
        problems.add("retry.not_found_intervals_days", "间隔必须为正整数天")
    if any(d <= 0 for d in cfg.retry.no_synced_intervals_days):
        problems.add("retry.no_synced_intervals_days", "间隔必须为正整数天")
    if cfg.policy.matching.max_duration_delta_seconds <= 0:
        problems.add("policy.matching.max_duration_delta_seconds", "必须为正数")
    if not (0.0 <= cfg.policy.matching.min_confidence <= 1.0):
        problems.add("policy.matching.min_confidence", "必须在 0~1 之间")
    if not (0.0 <= cfg.policy.matching.min_score_margin <= 1.0):
        problems.add("policy.matching.min_score_margin", "必须在 0~1 之间")

    # skip_if_valid_synced 与 write_enabled 的组合提示（非错误）
    if cfg.policy.write_enabled and cfg.policy.write_mode == "off":
        problems.add(
            "policy.write_mode",
            "write_enabled=true 时 write_mode 不应为 'off'；若要只读请设 write_enabled=false",
        )


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True
