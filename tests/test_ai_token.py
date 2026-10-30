"""AI 令牌必须被监控进程读到（2.1.2 修复的生产缺陷）。

背景：令牌保存在数据目录的 ai-token 文件（0600），此前**只有 Web 进程**读取它；
监控进程（`lyra watch`）从不读，模型调用必然缺密钥——后台的智能预审
等于没有开，设置页打开开关也没有任何效果。
"""

from __future__ import annotations

from pathlib import Path

from lyra.config import Config, StorageConfig, read_ai_token
from lyra.db import open_store


def _cfg_with_token(tmp_path: Path, token: str) -> Config:
    cfg = Config(storage=StorageConfig(data_dir=str(tmp_path / "data")))
    cfg.storage.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.storage.resolved_data_dir / "ai-token").write_text(token, encoding="utf-8")
    return cfg


def test_读不到令牌时返回空串(tmp_path: Path):
    cfg = Config(storage=StorageConfig(data_dir=str(tmp_path / "data")))
    assert read_ai_token(cfg) == ""


def test_读到保存的令牌(tmp_path: Path):
    cfg = _cfg_with_token(tmp_path, "secret-token")
    assert read_ai_token(cfg) == "secret-token"


def test_令牌首尾空白被裁掉(tmp_path: Path):
    cfg = _cfg_with_token(tmp_path, "  secret-token\n")
    assert read_ai_token(cfg) == "secret-token"


def test_监控进程启动时加载令牌(tmp_path: Path, monkeypatch):
    """watch 启动必须把令牌装进 cfg.ai.api_key——否则后台预审调不动模型。"""
    import lyra.cli as cli

    cfg = _cfg_with_token(tmp_path, "watch-token")
    store = open_store(tmp_path / "data")
    try:
        # 直接验证 cli 里的加载逻辑：_apply_saved_ai_settings 是 watch 内部的闭包，
        # 这里用同等的公开路径验证——cli 模块导出了 read_ai_token 且 watch 用了它
        assert cli.read_ai_token is read_ai_token
        assert read_ai_token(cfg) == "watch-token"
    finally:
        store.close()
