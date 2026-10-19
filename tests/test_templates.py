"""Configuration templates shipped with the repository must parse."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.config import load_config

ROOT = Path(__file__).resolve().parents[1]

TEMPLATES = [
    "src/lyra/templates/lyra.example.toml",
    "deploy/lyra.container.toml.example",
]


@pytest.mark.parametrize("name", TEMPLATES)
def test_shipped_config_templates_load(name: str) -> None:
    cfg = load_config(ROOT / name)
    assert cfg.roots
    assert cfg.web.port > 0
    assert cfg.storage.data_dir


def test_container_template_uses_container_paths() -> None:
    cfg = load_config(ROOT / "deploy/lyra.container.toml.example")
    assert cfg.roots[0].path == "/library"
    assert cfg.storage.data_dir == "/data"
    assert cfg.web.bind == "0.0.0.0"
