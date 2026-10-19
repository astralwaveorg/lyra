"""文件预览接口的安全边界测试。

为什么单独测这些：

预览功能把「按相对路径读文件」暴露成了 HTTP 接口，这是整个应用里**唯一
接受用户指定路径**的地方。一旦这里出漏洞，任何登录用户都能读取服务器上
任意文件（数据库、配置、私钥）。所以它的每一条限制都必须被固定下来，
而不是「看起来写了检查」。

被守护的边界：
1. 拒绝绝对路径
2. 拒绝任何形式的 `..`（按路径分量判断，不做字符串替换）
3. 拒绝符号链接——否则根内的链接可以指向根外
4. resolve 之后必须仍在根内（软链、`..`、挂载绕道都在这里被挡住）
5. 只允许白名单类型（音频/歌词/图片/文本）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from lyra.api import _kind_of, _safe_root_file
from lyra.config import Config, RootConfig, StorageConfig
from lyra.models import RootRole


@pytest.fixture
def root(tmp_path: Path) -> tuple[Config, Path]:
    base = tmp_path / "lib"
    (base / "album").mkdir(parents=True)
    (base / "album" / "01 歌.flac").write_bytes(b"fLaC" + b"\x00" * 32)
    (base / "album" / "01 歌.lrc").write_text("[00:01.00]词\n", encoding="utf-8")
    (base / "album" / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (base / "album" / "notes.nfo").write_text("<album/>", encoding="utf-8")
    # 根外的敏感文件，用来验证越界一定失败
    (tmp_path / "secret.db").write_text("SECRET", encoding="utf-8")
    (tmp_path / "secret.lrc").write_text("[00:01.00]SECRET\n", encoding="utf-8")

    cfg = Config(
        roots=[RootConfig(name="main", role=RootRole.LIBRARY, path=str(base))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    return cfg, base


def test_正常相对路径可以读取(root):
    cfg, _base = root
    _cfg_root, resolved = _safe_root_file(cfg, "main", "album/01 歌.lrc")
    assert resolved.is_file()
    assert resolved.name == "01 歌.lrc"


def test_拒绝绝对路径(root):
    cfg, _base = root
    for rel in ("/etc/passwd", "/tmp/secret.lrc"):
        with pytest.raises(HTTPException) as e:
            _safe_root_file(cfg, "main", rel)
        assert e.value.status_code == 400


def test_拒绝任何形式的上级跳转(root):
    cfg, _base = root
    for rel in (
        "../secret.lrc",
        "album/../../secret.lrc",
        "album/../album/../../secret.lrc",
        "..",
    ):
        with pytest.raises(HTTPException) as e:
            _safe_root_file(cfg, "main", rel)
        assert e.value.status_code == 400, rel


def test_拒绝符号链接指向根外(root, tmp_path: Path):
    """根内的软链可以指向根外——必须拒绝，否则检查形同虚设。"""
    cfg, base = root
    link = base / "album" / "evil.lrc"
    link.symlink_to(tmp_path / "secret.lrc")
    with pytest.raises(HTTPException) as e:
        _safe_root_file(cfg, "main", "album/evil.lrc")
    assert e.value.status_code == 400


def test_拒绝符号链接目录绕道(root, tmp_path: Path):
    cfg, base = root
    (base / "trap").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(HTTPException):
        _safe_root_file(cfg, "main", "trap/secret.lrc")


def test_拒绝未知受管根(root):
    cfg, _base = root
    with pytest.raises(HTTPException) as e:
        _safe_root_file(cfg, "nonexistent", "album/01 歌.lrc")
    assert e.value.status_code == 400


def test_不存在的文件返回404(root):
    cfg, _base = root
    with pytest.raises(HTTPException) as e:
        _safe_root_file(cfg, "main", "album/没有这个.lrc")
    assert e.value.status_code == 404


def test_拒绝空路径与空字节(root):
    cfg, _base = root
    for rel in ("", "album/01 歌.lrc\x00.txt"):
        with pytest.raises(HTTPException) as e:
            _safe_root_file(cfg, "main", rel)
        assert e.value.status_code == 400


def test_只允许白名单类型(root):
    """数据库、脚本这类文件既不该出现在列表里，也不该能被读出来。"""
    cfg, base = root
    (base / "album" / "lyra.db").write_bytes(b"SQLite format 3\x00")
    (base / "album" / "run.py").write_text("print(1)", encoding="utf-8")
    for rel in ("album/lyra.db", "album/run.py"):
        with pytest.raises(HTTPException) as e:
            _safe_root_file(cfg, "main", rel)
        assert e.value.status_code == 415, rel


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.flac", "audio"),
        ("a.mp3", "audio"),
        ("a.ogg", "audio"),
        ("a.opus", "audio"),
        ("a.lrc", "lyrics"),
        ("a.LRC", "lyrics"),
        ("a.jpg", "image"),
        ("a.PNG", "image"),
        ("a.nfo", "text"),
        ("a.m3u8", "text"),
        ("a.cue", "text"),
        ("a.db", "other"),
        ("a.py", "other"),
        ("noext", "other"),
    ],
)
def test_类型判定(name: str, expected: str):
    assert _kind_of(Path(name)) == expected
