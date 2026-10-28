"""前端构建产物与托管行为的守卫。

这一版前端是 Vue 3 + Vite 构建出来的（`web/` 是源码，`src/lyra/web/` 是产物）。
单文件内联脚本时代那套「用 node 执行整页脚本」的检查已经不再适用，
但**要防的问题没变**：产物缺失、子路由刷新 404、界面泄漏内部术语、
枚举值没有中文标签。下面按新的形态重新钉住它们。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.models import RootRole

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "lyra" / "web"
SOURCE_DIR = ROOT / "web" / "src"


@pytest.fixture
def client(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    with TestClient(create_app(cfg)) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def index_html() -> str:
    page = WEB_DIR / "index.html"
    assert page.is_file(), f"前端产物缺失: {page}（先在 web/ 里执行 npm run build）"
    return page.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def asset_paths(index_html: str) -> list[str]:
    return re.findall(r'(?:src|href)="(/assets/[^"]+)"', index_html)


def test_页面引用构建产物(index_html: str, asset_paths: list[str]):
    """产物必须是「入口脚本 + 样式」，而不是把代码内联回 HTML。"""
    assert asset_paths, "页面没有引用任何 /assets 资源"
    assert any(path.endswith(".js") for path in asset_paths), "缺少入口脚本"
    assert any(path.endswith(".css") for path in asset_paths), "缺少样式文件"
    assert '<div id="app">' in index_html, "缺少挂载点"

    # 内联脚本只允许主题引导那一小段；再出现大段逻辑说明构建配置退化了。
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index_html, re.S)
    for block in inline:
        assert len(block) < 600, "页面里出现了内联业务脚本，应当由构建产出"


def test_每个视图都产出了独立分包():
    """按路由懒加载的视图必须各自成包，否则首屏会一次性下载全部页面。

    重构后的对应关系：歌词审查/标签队列组件化进待办页（LyricsQueue /
    TagsQueue 作为 TodoView 的静态依赖与其同包），歌词刮削页升级为
    自动化控制台（AutomationView），新增待办页（TodoView）。
    断言口径是「路由级视图」——静态导入的组件与宿主视图同包。
    """
    names = {path.name for path in (WEB_DIR / "assets").glob("*.js")}
    for view in ("OverviewView", "LibraryView", "FilesView", "AutomationView",
                 "TodoView", "OrganizeView", "LogsView", "TrashView",
                 "SettingsView", "AboutView"):
        assert any(name.startswith(view) for name in names), f"缺少 {view} 的构建产物"


def test_子路由刷新回落到同一页面(client):
    """SPA 的地址栏会被收藏、会被分享，子路径刷新不能 404。"""
    for path in ("/overview", "/music", "/todo", "/automation", "/files",
                 "/activity", "/settings", "/about"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} 返回 {response.status_code}"
        assert "text/html" in response.headers["content-type"]
        assert '<div id="app">' in response.text


def test_未知接口仍然是JSON错误(client):
    """/api/ 前缀不能回退成 HTML：脚本会因此在 json.loads 处报更难懂的错。"""
    response = client.get("/api/v1/definitely-not-a-route")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()


def test_页面禁用缓存而资源长期缓存(client):
    """页面每次确认新鲜度，带哈希的资源永久缓存——两者目的相反，必须分开。"""
    page = client.get("/")
    assert "no-store" in page.headers.get("cache-control", "")

    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)
    assert assets
    asset = client.get(assets[0])
    assert asset.status_code == 200
    assert "immutable" in asset.headers.get("cache-control", "")


def test_图标接口可用(client):
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg")


def _enum_values(name: str) -> list[str]:
    source = (ROOT / "src" / "lyra" / "models.py").read_text(encoding="utf-8")
    block = re.search(rf"class {name}\(StrEnum\):(.*?)(?=\nclass )", source, re.S)
    assert block, f"models.py 里找不到 {name}"
    return re.findall(r'=\s*"([a-z_]+)"', block.group(1))


def test_每个问题码都有中文标签():
    """后端新增问题码时，界面不能退化成显示英文枚举。

    这是**最容易漏**的一类：新增一个 IssueCode，所有测试都过，
    只有使用者会看到 `missing_isrc` 这样的字样。
    """
    labels = (SOURCE_DIR / "utils" / "labels.ts").read_text(encoding="utf-8")
    missing = [
        code for code in _enum_values("IssueCode")
        if not re.search(rf"\b{re.escape(code)}\s*:", labels)
    ]
    assert not missing, f"以下问题码没有中文标签：{missing}"


def test_每个歌词状态都有中文标签():
    labels = (SOURCE_DIR / "utils" / "labels.ts").read_text(encoding="utf-8")
    missing = [
        state for state in _enum_values("LyricsState")
        if not re.search(rf"\b{re.escape(state)}\s*:", labels)
    ]
    assert not missing, f"以下歌词状态没有中文标签：{missing}"


def _template_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    template = raw.split("<template>", 1)[-1].split("</template>", 1)[0]
    # 插值与标签本身不是文案：先剥掉再查词
    template = re.sub(r"\{\{.*?\}\}", " ", template, flags=re.S)
    template = re.sub(r"<[^>]+>", " ", template)
    return template


#: 面向使用者的文案里不该出现的内部术语。
_INTERNAL_TERMS = ("sidecar", "provider", "staged", "mutagen", "vorbis", "id3", "pipeline")


def test_模板文案不含内部术语():
    offenders: list[str] = []
    for path in SOURCE_DIR.rglob("*.vue"):
        text = _template_text(path)
        for term in _INTERNAL_TERMS:
            if re.search(rf"\b{term}\b", text, re.I):
                offenders.append(f"{path.relative_to(ROOT)} ← {term}")
    assert not offenders, "模板文案里出现了内部术语：\n  " + "\n  ".join(offenders)


def test_模板文案不含Markdown标记():
    offenders = [
        str(path.relative_to(ROOT))
        for path in SOURCE_DIR.rglob("*.vue")
        if re.search(r"\*\*[^*\n]+\*\*", _template_text(path))
    ]
    assert not offenders, f"模板文案里出现了 Markdown 标记：{offenders}"


def test_入口脚本能被JS解析器读入():
    """构建产物语法错误在服务端完全不可见，只会在浏览器白屏。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，无法做产物语法校验（跳过不等于通过）")

    entry = next((WEB_DIR / "assets").glob("index-*.js"), None)
    assert entry is not None, "找不到入口脚本"
    # 产物是 ESM：用 .mjs 后缀让 node 按模块解析，否则 import 语法会被当成 CJS 报错
    target = entry.with_suffix(".mjs")
    target.write_bytes(entry.read_bytes())
    try:
        result = subprocess.run(
            [node, "--check", str(target)], capture_output=True, text=True, timeout=120
        )
    finally:
        target.unlink(missing_ok=True)
    assert result.returncode == 0, f"入口脚本语法错误：\n{result.stderr[:1500]}"


def test_前端源码可以编译():
    """`web/` 源码必须存在且引用了 Element Plus —— 防止有人只提交产物。"""
    for name in ("package.json", "vite.config.ts", "src/main.ts", "src/App.vue"):
        assert (ROOT / "web" / name).is_file(), f"缺少前端源码 {name}"
    package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    assert "element-plus" in package["dependencies"]
    assert "vue" in package["dependencies"]


def test_构建产物会被打进wheel():
    """前端产物在 .gitignore 里，必须显式列为 hatch 的 artifacts。

    不加这行时，**在有 git 的工作区**里 `pip wheel .` 会按 .gitignore 排除
    `src/lyra/web/`，打出一个没有前端的包；而 Docker 里因为 /app 不是 git
    仓库，反而一切正常——这种「本机与实际运行不一致」最难查。
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "artifacts" in text, "pyproject 缺少 build artifacts 配置"
    assert "src/lyra/web" in text, "artifacts 里没有前端产物目录"


def test_单元测试目录存在():
    """前端工具函数有 vitest 覆盖（在 web/ 里执行 npm test）。"""
    specs = list((SOURCE_DIR).rglob("*.spec.ts"))
    assert specs, "缺少前端单元测试"


def test_两个候选接口的字段不同(tmp_path: Path, flac_factory):
    """`/tracks/candidates` 与 `/staged/{id}/alternatives` **不是同一个形状**。

    这条测试是一次真实事故换来的：前端按「候选列表」的类型去读对比接口的
    `option.gates.every(...)`，一打开对比抽屉就抛 TypeError，而页面只是变空白。
    两个接口都叫「候选」，字段却各自一套——不钉住，下一个人还会踩。
    """
    from lyra.compare import build_comparison, list_candidates
    from lyra.config import Config, RootConfig, StorageConfig
    from lyra.models import RootRole
    from lyra.providers.base import Candidate, RawLyrics

    lib = tmp_path / "lib"
    lib.mkdir()
    flac_factory(lib / "a.flac")
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    root = cfg.roots[0]

    class P:
        name = "qq"

        def search(self, query):
            return [Candidate(provider="qq", provider_id="x1", title="A", artists=("B",), duration=200.0)]

        def lyrics(self, candidate):
            return RawLyrics(text="[00:01.00]词\n", synced=True)

        def close(self):
            pass

    alternatives = build_comparison(cfg, root=root, rel_path="a.flac", providers=[P()])
    candidates = list_candidates(cfg, root=root, rel_path="a.flac", providers=[P()])

    alt_keys = set(alternatives["options"][0])
    cand_keys = set(candidates["options"][0])

    # 对比接口：有 is_current、没有 gates
    assert "is_current" in alt_keys
    assert "gates" not in alt_keys
    # 候选接口：有 gates 与 is_best、没有 is_current
    assert "gates" in cand_keys
    assert "is_best" in cand_keys
    assert "is_current" not in cand_keys

    # 界面对比抽屉读取的字段必须在里面
    for key in (
        "provider", "provider_id", "title", "artists", "duration",
        "duration_delta", "score", "synced", "rejected", "lines", "error",
    ):
        assert key in alt_keys, f"对比接口缺少界面需要的字段 {key}"
