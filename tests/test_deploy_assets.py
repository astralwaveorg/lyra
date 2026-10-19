

def test_YAML注释里不写Markdown标记():
    """YAML 注释是**纯文本**，Markdown 的 `**加粗**` 会原样显示成星号。

    实际在 1Panel 的编排编辑器里，注释里的 `**路径与参数名**` 就是
    一串星号，看起来很像出错。这类标记只该出现在 .md 文件里。
    """
    import re
    from pathlib import Path

    offenders: list[str] = []
    for path in Path("deploy").glob("*.y*ml"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("#"):
                continue
            if re.search(r"\*\*.+?\*\*", line) or "`" in line:
                offenders.append(f"{path}:{lineno}: {stripped[:70]}")
    assert not offenders, (
        "YAML 注释里出现了 Markdown 标记（会在 1Panel 等界面原样显示）：\n  "
        + "\n  ".join(offenders)
    )


def test_存在dockerignore且排除macOS垃圾文件():
    """构建上下文必须排除 macOS 的 `._*` 资源分叉文件。

    实际：没有 .dockerignore 时，`COPY src ./src` 把 30 个 `._*.py`
    带进镜像，`pip install` 又把它烤进 site-packages。
    不影响运行，但属于产物污染——体积、审计、以及将来排查时多一层干扰。
    """
    from pathlib import Path

    path = Path(".dockerignore")
    assert path.is_file(), "缺少 .dockerignore，构建上下文会把无关文件带进镜像"

    content = path.read_text(encoding="utf-8")
    for pattern in ("._*", "__pycache__", ".venv", "tests/"):
        assert pattern in content, f".dockerignore 应排除 {pattern!r}"


def test_dockerignore不排除必需文件():
    """排除清单不能把构建需要的东西也排掉。"""
    from pathlib import Path

    content = Path(".dockerignore").read_text(encoding="utf-8")
    for needed in ("src", "pyproject.toml"):
        # 不应出现「排除 src」或「排除 pyproject.toml」这种写法
        assert f"\n{needed}\n" not in content, f"不能排除构建必需的 {needed}"
        assert f"\n{needed}/\n" not in content, f"不能排除构建必需的 {needed}/"
