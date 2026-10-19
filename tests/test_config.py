"""配置加载与校验的测试。

为什么测这些：

配置是**可移植性的唯一入口**，也是「静默失败」风险最高的地方。
一个拼错的键如果被静默忽略，用户会以为改了配置而实际没生效——
这是最危险的失败模式：把配置写错却不报错，实际行为与预期不符。

同时这里锁死几条安全红线：
- 数据目录不得落在曲库内部（备份/缓存/数据库不能污染音乐目录）
- 未声明的来源绝不能被使用（不许偷偷回退到其他歌词源）
- 歌词输出不能两个开关同时关掉（那等于什么也不做，却显示"成功"）
- 凭据在导出时必须掩码（否则会被写进日志、报告或截图）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.config import (
    CONFIG_VERSION,
    Config,
    ConfigError,
    LrcLibProviderConfig,
    load_config,
)


def _load(path: Path, env: dict[str, str] | None = None) -> Config:
    """始终显式传 env，避免本机真实环境变量污染测试。"""
    return load_config(path, env={} if env is None else env)


# ---------------------------------------------------------------------------
# 正常加载
# ---------------------------------------------------------------------------


def test_示例配置可加载(example_config_path: Path, tmp_path: Path, monkeypatch):
    """随包分发的示例配置必须始终是合法配置——它是用户复制的起点。"""
    monkeypatch.chdir(tmp_path)  # 让相对路径 ./data 落在 tmp，不依赖仓库位置
    cfg = _load(example_config_path)

    assert cfg.config_version == CONFIG_VERSION
    assert cfg.source_path == example_config_path
    # 至少一个受管根，且开启了主源
    assert cfg.roots, "示例配置必须至少定义一个受管根"
    assert cfg.roots[0].name == "main"
    assert "qq" in cfg.providers.enabled
    assert cfg.enabled_roots, "默认根必须可用"


def test_client_identity符合LRCLIB约定():
    """LRCLIB 要求 <AppName>/<version> (<URL>)；这是注册合规的一部分。"""
    with_url = LrcLibProviderConfig(
        client_name="Lyra", client_version="1.0.0", client_url="https://example.test"
    )
    assert with_url.client_identity() == "Lyra/1.0.0 (https://example.test)"

    # 没填 URL 时退化为不带括号的形式，而不是产生 "Lyra/1.0.0 ()" 这种脏串
    without_url = LrcLibProviderConfig(
        client_name="Lyra", client_version="1.0.0", client_url=""
    )
    assert without_url.client_identity() == "Lyra/1.0.0"


# ---------------------------------------------------------------------------
# 未知键必须报错，并给出拼写建议
# ---------------------------------------------------------------------------


def test_未知键报错并给出拼写建议(tmp_path: Path):
    """拼错的键静默忽略 → 用户以为配置生效了。必须报错并提示正确写法。"""
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{tmp_path / "lib"}"\n\n'
        '[storage]\ndata_dri = "./data"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)

    problems = "\n".join(excinfo.value.problems)
    assert "data_dri" in problems
    assert "是否想写 data_dir？" in problems, f"缺少拼写建议: {problems}"


def test_嵌套表的未知键也报错并给建议(tmp_path: Path):
    """提示不能只覆盖顶层表；嵌套表里的拼写错误同样致命。"""
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{tmp_path / "lib"}"\n\n'
        '[storage]\ndata_dir = "./data"\n\n[watch]\nstable_secondz = 5\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)

    problems = "\n".join(excinfo.value.problems)
    assert "stable_secondz" in problems
    assert "是否想写 stable_seconds？" in problems


def test_未知顶层键报错(tmp_path: Path):
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{tmp_path / "lib"}"\n\n'
        '[storage]\ndata_dir = "./data"\n\n[nonsense]\nx = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)
    assert any("nonsense" in p for p in excinfo.value.problems)


# ---------------------------------------------------------------------------
# 安全红线
# ---------------------------------------------------------------------------


def test_数据目录不得位于受管根内部(tmp_path: Path):
    """备份、缓存、数据库必须放在曲库之外；放进去会污染音乐目录。"""
    root = tmp_path / "lib"
    root.mkdir()
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{root}"\n\n'
        f'[storage]\ndata_dir = "{root / "data"}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)
    problems = "\n".join(excinfo.value.problems)
    assert "storage.data_dir" in problems
    assert "内部" in problems


def test_数据目录在根之外是允许的(tmp_path: Path):
    """反向用例：证明上一条不是「总是报错」。"""
    root = tmp_path / "lib"
    root.mkdir()
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{root}"\n\n'
        f'[storage]\ndata_dir = "{tmp_path / "data"}"\n',
        encoding="utf-8",
    )
    cfg = _load(cfg_file)
    assert cfg.storage.data_dir == str(tmp_path / "data")


def test_roots为空时报错(tmp_path: Path):
    """没有受管根就没有任何可做的事，必须显式报错而不是启动成一个空服务。"""
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\nroots = []\n\n[storage]\ndata_dir = "{tmp_path / "data"}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)
    assert any("roots" in p for p in excinfo.value.problems)


def test_缺少roots段也报错(tmp_path: Path):
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[storage]\ndata_dir = "{tmp_path / "data"}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)
    assert any("roots" in p for p in excinfo.value.problems)


def test_config_version不匹配时报错(tmp_path: Path):
    """版本不匹配必须拒绝，不能按旧语义猜着解析。"""
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = {CONFIG_VERSION + 1}\n\n[[roots]]\nname = "main"\n'
        f'path = "{tmp_path / "lib"}"\n\n[storage]\ndata_dir = "{tmp_path / "data"}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)
    problems = "\n".join(excinfo.value.problems)
    assert "config_version" in problems
    assert "不支持版本" in problems


def test_未实现来源在禁止其他来源时报错(tmp_path: Path):
    """allow_other_providers=false 时出现未实现的来源必须拒绝。

    覆盖： 设计约束：**不允许偷偷回退到未声明的来源**，
    否则报告会说「这是 QQ 歌词」，而实际来源完全不是。

    注意此处用 `kugou`（确实未实现）而不是 `netease`——netease 后来实现了，
    曾经拿它当反例的写法会随着功能增长而失效。**反例要选当前确实不存在的**。
    """
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{tmp_path / "lib"}"\n\n'
        f'[storage]\ndata_dir = "{tmp_path / "data"}"\n\n'
        '[providers]\nenabled = ["qq", "kugou"]\n\n'
        '[policy]\nallow_other_providers = false\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)
    problems = "\n".join(excinfo.value.problems)
    assert "kugou" in problems
    assert "未启用" in problems or "未知来源" in problems


def test_网易云已作为正式来源被接受(tmp_path: Path):
    """netease 现在是内置来源：出现在 enabled 里必须被接受，而不是报未知来源。

    同时它必须真的能被工厂构造出来——只改 `enabled` 白名单而忘记注册工厂，
    会让配置合法但运行时才炸。
    """
    from lyra.providers import SUPPORTED_PROVIDERS, create_providers

    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{tmp_path / "lib"}"\n\n'
        f'[storage]\ndata_dir = "{tmp_path / "data"}"\n\n'
        '[providers]\nenabled = ["qq", "lrclib", "netease"]\n',
        encoding="utf-8",
    )
    cfg = _load(cfg_file)
    assert cfg.providers.enabled == ["qq", "lrclib", "netease"]
    assert "netease" in SUPPORTED_PROVIDERS

    providers = create_providers(cfg.providers)
    assert [p.name for p in providers] == ["qq", "lrclib", "netease"]
    for provider in providers:
        provider.close()


def test_歌词输出两个开关同时关闭时报错(tmp_path: Path):
    """write_sidecar_lrc 与 embed_lyrics 都为 false = 不输出任何歌词。

    这是一个「配置看起来正常、实际什么也不做」的陷阱，必须在启动时挡下。
    """
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{tmp_path / "lib"}"\n\n'
        f'[storage]\ndata_dir = "{tmp_path / "data"}"\n\n'
        '[policy]\nwrite_sidecar_lrc = false\nembed_lyrics = false\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)
    problems = "\n".join(excinfo.value.problems)
    assert "policy" in problems
    assert "不会输出任何歌词" in problems


# ---------------------------------------------------------------------------
# 凭据掩码
# ---------------------------------------------------------------------------

_SECRET_CONFIG = """\
config_version = 1

[[roots]]
name = "main"
path = "{root}"

[storage]
data_dir = "{data}"

[providers.lrclib]
authorization_ref = "REF-DO-NOT-LEAK"

[providers.acoustid]
api_key = "KEY-DO-NOT-LEAK"
"""


def _secret_config(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        _SECRET_CONFIG.format(root=root, data=tmp_path / "data"), encoding="utf-8"
    )
    return cfg_file


def test_凭据默认被打码(tmp_path: Path):
    """to_dict 会被用于日志/报告/Web 页面；凭据一旦泄漏就很难收回。"""
    cfg = _load(_secret_config(tmp_path))
    masked = cfg.to_dict(mask_secrets=True)

    assert masked["providers"]["lrclib"]["authorization_ref"] == "***"
    assert masked["providers"]["acoustid"]["api_key"] == "***"
    # 掩码后的结构里不能残留原值
    assert "REF-DO-NOT-LEAK" not in repr(masked)
    assert "KEY-DO-NOT-LEAK" not in repr(masked)


def test_显式要求时可取回原值(tmp_path: Path):
    """需要真正使用凭据时必须能拿到原值，否则功能没法用。"""
    cfg = _load(_secret_config(tmp_path))
    raw = cfg.to_dict(mask_secrets=False)

    assert raw["providers"]["lrclib"]["authorization_ref"] == "REF-DO-NOT-LEAK"
    assert raw["providers"]["acoustid"]["api_key"] == "KEY-DO-NOT-LEAK"


def test_空凭据不会被掩码成星号(tmp_path: Path):
    """空值保持为空，避免把「没配置」伪装成「已配置」。"""
    root = tmp_path / "lib"
    root.mkdir()
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{root}"\n\n'
        f'[storage]\ndata_dir = "{tmp_path / "data"}"\n\n'
        '[providers.lrclib]\nauthorization_ref = ""\n',
        encoding="utf-8",
    )
    masked = _load(cfg_file).to_dict(mask_secrets=True)
    assert masked["providers"]["lrclib"]["authorization_ref"] == ""


# ---------------------------------------------------------------------------
# 环境变量覆盖
# ---------------------------------------------------------------------------


def _base_config(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text(
        f'config_version = 1\n\n[[roots]]\nname = "main"\npath = "{root}"\n\n'
        f'[storage]\ndata_dir = "{tmp_path / "from-file"}"\n\n'
        '[policy]\nwrite_enabled = true\n\n[web]\nport = 8080\n',
        encoding="utf-8",
    )
    return cfg_file


def test_环境变量优先于配置文件(tmp_path: Path):
    """容器部署主要靠环境变量；覆盖关系错了会导致「改了 .env 却没生效」。"""
    cfg_file = _base_config(tmp_path)
    env = {
        "LYRA_DATA_DIR": str(tmp_path / "from-env"),
        "LYRA_WRITE_ENABLED": "false",
        "LYRA_WEB_PORT": "9999",
    }
    cfg = _load(cfg_file, env=env)

    assert cfg.storage.data_dir == str(tmp_path / "from-env")
    assert cfg.policy.write_enabled is False
    assert cfg.web.port == 9999


def test_无环境变量时沿用配置文件(tmp_path: Path):
    """反向用例：证明上一条不是「总是用环境变量」。"""
    cfg_file = _base_config(tmp_path)
    cfg = _load(cfg_file, env={})

    assert cfg.storage.data_dir == str(tmp_path / "from-file")
    assert cfg.policy.write_enabled is True
    assert cfg.web.port == 8080


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("yes", True),
                                          ("on", True), ("0", False), ("false", False),
                                          ("no", False), ("off", False)])
def test_布尔环境变量的多种写法(tmp_path: Path, raw: str, expected: bool):
    """容忍常见写法；否则用户在 .env 里写 true/1 会莫名失效。"""
    cfg_file = _base_config(tmp_path)
    cfg = _load(cfg_file, env={"LYRA_WRITE_ENABLED": raw})
    assert cfg.policy.write_enabled is expected


def test_无法解析的布尔环境变量报错(tmp_path: Path):
    """解析不了必须报错，不能静默取默认值。"""
    cfg_file = _base_config(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file, env={"LYRA_WRITE_ENABLED": "maybe"})
    assert any("LYRA_WRITE_ENABLED" in p for p in excinfo.value.problems)


def test_环境变量可注入凭据(tmp_path: Path):
    """凭据走环境变量是推荐的部署方式（不进 Git、不进配置文件）。"""
    cfg_file = _base_config(tmp_path)
    cfg = _load(
        cfg_file,
        env={
            "LYRA_LRCLIB_AUTHORIZATION_REF": "REF-FROM-ENV",
            "LYRA_ACOUSTID_API_KEY": "KEY-FROM-ENV",
        },
    )
    assert cfg.providers.lrclib.authorization_ref == "REF-FROM-ENV"
    assert cfg.providers.acoustid.api_key == "KEY-FROM-ENV"
    # 给了 AcoustID key 就应自动启用该来源，否则用户会以为它在工作
    assert cfg.providers.acoustid.enabled is True


def test_配置文件不存在时报错(tmp_path: Path):
    """显式指定的配置文件不存在必须报错，不能静默回退到默认值。"""
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path / "nope.toml")
    assert any("不存在" in p for p in excinfo.value.problems)


def test_TOML语法错误时报错(tmp_path: Path):
    cfg_file = tmp_path / "lyra.toml"
    cfg_file.write_text("this is not = = toml\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        _load(cfg_file)
    assert any("TOML" in p for p in excinfo.value.problems)


def test_写节允许_auto_clean_orphan_sidecars(tmp_path):
    """新增配置键必须进白名单，否则监控会因「未知配置键」整个起不来。

    实际：漏了这一行，容器直接 crash-loop——配置校验是严格的。
    """
    from lyra.config import load_config

    cfg_path = tmp_path / "lyra.toml"
    cfg_path.write_text(
        '[[roots]]\nname = "lib"\nrole = "library"\npath = "/tmp/lib"\n\n'
        "[write]\nauto_clean_orphan_sidecars = true\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.write.auto_clean_orphan_sidecars is True
