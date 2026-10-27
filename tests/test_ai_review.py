"""AI 辅助判定：只判断「两份歌词是不是同一首」，动作仍由 Lyra 执行。

## 为什么要有测试

这条路径把「删哪个文件」的建议交给了模型，风险点很具体：

1. 模型输出不是 JSON、或请求失败 → 必须当作**没有判断**，绝不动作；
2. 判断「不是同一首」→ 不能清理；
3. 判断「是同一首」但置信度不高 → 不能自动清理；
4. 真要清理时，动作是**移入回收站**（可还原），不是删除。

固定这些语义，避免以后为了"更智能"把边界放宽。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import lyra.ai as ai_module
from lyra.ai import AIError, Judgement, extract_json, review_duplicate
from lyra.api import create_app
from lyra.audit import audit_root
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import sync_state

_LRC = "[ti:童年时]\n[ar:张国荣]\n[00:01.00]童年时 我与你家乡中相见天未亮\n"


def test_能从带代码块的输出里取出_JSON():
    assert extract_json('```json\n{"same_song": true}\n```') == {"same_song": True}
    assert extract_json('废话{"a": 1}尾巴') == {"a": 1}
    with pytest.raises(AIError):
        extract_json("没有 JSON")


def test_调用成功时解析成判断():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "same_song": True, "confidence": 0.93, "keep": "A",
                "reason": "正文高度重复，仅前奏行不同",
            }, ensure_ascii=False)}}]
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    j = review_duplicate(
        base_url="https://api.test/v1/chat", model="m", api_key="k", timeout=5,
        audio={"title": "童年时", "artists": ["张国荣"], "album": "Salute",
               "duration": 200.0},
        existing_name="a.lrc", existing_text=_LRC,
        orphan_name="b.lrc", orphan_text=_LRC, client=client,
    )
    assert (j.same_song, j.confidence, j.keep) == (True, 0.93, "existing")
    assert "正文" in j.reason


def test_上游失败时抛错():
    def handler(request):
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AIError):
        review_duplicate(
            base_url="https://api.test/v1/chat", model="m", api_key="k", timeout=5,
            audio={}, existing_name="a", existing_text="", orphan_name="b",
            orphan_text="", client=client)


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, flac_factory, monkeypatch):
    lib = tmp_path / "lib"
    album = lib / "張國榮" / "Salute"
    flac_factory(album / "01 童年時.flac",
                 entries=(("TITLE", "童年時"), ("ARTIST", "張國榮")))
    (album / "01 童年時.lrc").write_text("[00:02.00]另一份\n", encoding="utf-8")
    (album / "01 - 张国荣 - 童年时.lrc").write_text(_LRC, encoding="utf-8")
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    cfg.ai.enabled = True
    cfg.ai.api_key = "test-key"
    store = open_store(cfg.storage.resolved_data_dir)
    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]))
    store.close()

    verdict = {"same_song": True, "confidence": 0.95, "keep": "existing"}

    def fake_review(**_kwargs):
        return Judgement(verdict["same_song"], verdict["confidence"],
                         verdict["keep"], "同一首歌", "fake",
                         overlap=verdict.get("overlap", 0.99),
                         orphan_covered=verdict.get("orphan_covered", 1.0))

    # 接口内部是 `from .ai import review_duplicate`，所以要替换 ai 模块本身
    monkeypatch.setattr(ai_module, "review_duplicate", fake_review)
    with TestClient(create_app(cfg)) as client:
        yield client, album, cfg, verdict


def test_默认只给建议不动文件(env):
    client, album, cfg, _v = env
    r = client.post("/api/v1/orphans/ai-review", json={"root": "lib"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "cleanup"
    assert items[0]["applied"] is False
    assert (album / "01 - 张国荣 - 童年时.lrc").exists(), "默认不能动文件"


def test_高置信度且明确_apply_才清理(env):
    client, album, cfg, _v = env
    r = client.post("/api/v1/orphans/ai-review",
                    json={"root": "lib", "apply": True})
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["applied"] is True
    assert not (album / "01 - 张国荣 - 童年时.lrc").exists()
    assert (album / "01 童年時.lrc").exists(), "现有那份必须留着"


def test_置信度不够时不清理(env):
    client, album, cfg, verdict = env
    verdict["confidence"] = 0.4
    r = client.post("/api/v1/orphans/ai-review",
                    json={"root": "lib", "apply": True})
    assert r.json()["items"][0]["applied"] is False
    assert (album / "01 - 张国荣 - 童年时.lrc").exists()


def test_判断不是同一首时不动(env):
    client, album, cfg, verdict = env
    verdict["same_song"] = False
    r = client.post("/api/v1/orphans/ai-review",
                    json={"root": "lib", "apply": True})
    assert r.json()["items"][0]["action"] == "keep"
    assert (album / "01 - 张国荣 - 童年时.lrc").exists()


def test_正文重合度不高时不清理(env):
    """模型说「同一首」也不行：少一行歌词都是用户的东西，留着让人看。

    实际《迷路》两份各 36 行，去时间轴后只有 14 行重合——不同转录。
    模型（0.95 置信）说"正文高度一致"，靠它就会把有用的那份删掉。
    """
    client, album, cfg, verdict = env
    verdict["orphan_covered"] = 0.78
    r = client.post("/api/v1/orphans/ai-review",
                    json={"root": "lib", "apply": True})
    item = r.json()["items"][0]
    assert item["action"] == "keep"
    assert item["applied"] is False
    assert (album / "01 - 张国荣 - 童年时.lrc").exists()


def test_未启用时明确报错(env):
    _client, _album, cfg, _v = env
    cfg.ai.enabled = False
    with TestClient(create_app(cfg)) as client:
        r = client.post("/api/v1/orphans/ai-review", json={"root": "lib"})
    assert r.status_code == 503


def test_GBK_编码的歌词要按真实编码交给_AI(env, monkeypatch):
    """下载来的 .lrc 常是 GBK；用 utf-8 读成乱码交给模型，会得到错误判断。

    实际：两份 30 行的歌词，用 utf-8 读出来"共有行 0"，按真实编码读出来
    "共有 24 行"。模型拿到乱码照样会一本正经地给结论——这正是危险之处。
    """
    client, album, cfg, _v = env
    gbk = ("[ti:童年时]\n[00:01.00]童年时 我与你家乡中相见天未亮\n").encode("gbk")
    (album / "01 - 张国荣 - 童年时.lrc").write_bytes(gbk)

    captured = {}

    def fake_review(**kwargs):
        captured["orphan_text"] = kwargs["orphan_text"]
        return ai_module.Judgement(True, 0.9, "existing", "同一首", "fake")

    monkeypatch.setattr(ai_module, "review_duplicate", fake_review)
    r = client.post("/api/v1/orphans/ai-review", json={"root": "lib"})
    assert r.status_code == 200, r.text
    assert "童年时" in captured["orphan_text"], captured
    assert "\ufffd" not in captured["orphan_text"], "不能把乱码交给模型"


def test_只多出作词作曲行的孤儿算纯重复():
    """元信息不同不算内容不同。实际《童年时》两份各 19 行，孤儿只多出
    「作词/作曲/编曲」三行，歌词一行没多——清掉它不丢内容。"""
    from lyra.ai import orphan_coverage

    existing = ("[ti:x]\n[00:01.00]词：郑国江\n[00:02.00]童年时 我与你家乡中相见天未亮\n"
                "[00:03.00]你与我 永远心意也一样\n")
    orphan = ("[00:00.0]作词: 郑国江\n[00:01.0]作曲: 根田成一\n"
              "[00:02.0]童年时\u00a0我与你家乡中相见天未亮\n"
              "[00:03.0]你与我\u00a0永远心意也一样\n"
              "[00:04.0]编曲: 袁卓凡\n")
    assert orphan_coverage(existing, orphan) == 1.0


def test_多出真歌词行就不算纯重复():
    """《迷路》孤儿多出 4 行真歌词——清掉就丢内容了。"""
    from lyra.ai import orphan_coverage

    existing = "[00:01.0]你和我走在爱情地图\n[00:02.0]想找到通往永恒的路\n"
    orphan = ("[00:01.0]你和我走在爱情地图\n[00:02.0]想找到通往永恒的路\n"
              "[00:03.0]后退是迷途\n[00:04.0]向前是末路\n")
    assert orphan_coverage(existing, orphan) < 0.999


def test_清理接口默认只预演(tmp_path, flac_factory):
    """`dry_run` 默认 true：点错也不该动文件。"""
    from lyra.config import Config, RootConfig, StorageConfig, WebConfig
    from lyra.models import RootRole

    lib = tmp_path / "lib"
    album = lib / "x"
    flac_factory(album / "01 歌.flac", entries=(("TITLE", "歌"),))
    (album / "没有这首歌.lrc").write_text("[00:01.00]词\n", encoding="utf-8")
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    with TestClient(create_app(cfg)) as client:
        r = client.post("/api/v1/orphans/clean", json={"root": "lib"})
        assert r.status_code == 200, r.text
        assert r.json()["dry_run"] is True
        assert (album / "没有这首歌.lrc").exists(), "预演不能动文件"


def test_批量删除走回收站且逐项容错(tmp_path, flac_factory):
    """多选删除：**一个失败不能拖累整批**，且必须保留相对路径。"""
    from lyra.config import Config, RootConfig, StorageConfig, WebConfig
    from lyra.models import RootRole

    lib = tmp_path / "lib"
    a = lib / "A"
    a.mkdir(parents=True)
    for name in ("1.flac", "2.flac"):
        flac_factory(a / name, entries=(("TITLE", name),))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    with TestClient(create_app(cfg)) as client:
        r = client.post("/api/v1/files/batch-delete", json={
            "root": "lib", "paths": ["A/1.flac", "A/不存在.flac", "A/2.flac"],
        })
        assert r.status_code == 200, r.text
        d = r.json()
    assert sorted(d["moved"]) == ["A/1.flac", "A/2.flac"]
    assert d["skipped"] == ["A/不存在.flac"]
    trash = tmp_path / "data" / "trash" / d["batch"]
    assert (trash / "A" / "1.flac").is_file(), "回收站里必须保留原相对路径"


# ---------------------------------------------------------------------------
# 协议适配
# ---------------------------------------------------------------------------


def _payload_json() -> str:
    return json.dumps(
        {"same_song": True, "confidence": 0.9, "keep": "A", "reason": "同一首"},
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    ("protocol", "response_json", "expected_path"),
    [
        (
            "openai_chat",
            {"choices": [{"message": {"content": _payload_json()}}]},
            "/v1/chat/completions",
        ),
        (
            "openai_responses",
            {"output_text": _payload_json()},
            "/v1/responses",
        ),
        (
            "anthropic",
            {"content": [{"type": "text", "text": _payload_json()}]},
            "/v1/messages",
        ),
    ],
)
def test_三种协议都能请求并解析(protocol: str, response_json: dict, expected_path: str):
    seen: dict = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response_json)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    judgement = review_duplicate(
        protocol=protocol,
        base_url="https://api.test/v1",
        model="test-model",
        api_key="test-key",
        timeout=5,
        audio={"title": "t", "artists": ["a"]},
        existing_name="a.lrc",
        existing_text=_LRC,
        orphan_name="b.lrc",
        orphan_text=_LRC,
        client=client,
    )
    assert judgement.same_song is True
    assert seen["url"].endswith(expected_path)
    assert seen["body"]["model"] == "test-model"
    if protocol == "anthropic":
        assert seen["headers"]["x-api-key"] == "test-key"
        assert seen["headers"]["anthropic-version"] == "2023-06-01"
        assert "system" not in seen["body"]
    else:
        assert seen["headers"]["authorization"] == "Bearer test-key"


def test_Responses数组返回也能解析():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": _payload_json()},
                        ]
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    judgement = review_duplicate(
        protocol="openai_responses",
        base_url="https://api.test/v1",
        model="m",
        api_key="k",
        timeout=5,
        audio={},
        existing_name="a",
        existing_text=_LRC,
        orphan_name="b",
        orphan_text=_LRC,
        client=client,
    )
    assert judgement.keep == "existing"


def test_连接测试返回endpoint和延迟():
    def handler(request: httpx.Request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "OK"}}]}
        )

    from lyra.ai import test_connection

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = test_connection(
        protocol="openai_chat",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key="k",
        timeout=5,
        client=client,
    )
    assert result["ok"] is True
    assert result["endpoint"] == "https://api.openai.com/v1/chat/completions"
    assert result["sample"] == "OK"
    assert result["latency_ms"] >= 0


def test_连接失败要点名代理而不是只说连接被拒(monkeypatch):
    """代理跑在另一台机器上，它一停，错误就只剩「Connection refused」。

    使用者看到那句话只会怀疑自己的密钥。报错里必须写明是代理连不上。
    """
    import httpx

    import lyra.ai as ai_module
    from lyra.ai import AIError, test_connection

    def handler(request: httpx.Request):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    from lyra.network import NetworkState

    monkeypatch.setattr(
        ai_module, "active_network",
        lambda: NetworkState(proxy="http://10.0.1.8:7890"),
    )
    monkeypatch.setattr(ai_module, "proxy_reachable", lambda *_a, **_k: False)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AIError) as excinfo:
        test_connection(
            protocol="openai_chat",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="k",
            timeout=5,
            client=client,
            use_proxy=True,
        )
    message = str(excinfo.value)
    assert "10.0.1.8:7890" in message, message
    assert "代理" in message, message


def test_关掉代理后错误不再提代理(monkeypatch):
    """关了代理就是直连，错误信息里不该再出现代理地址。"""
    import httpx

    import lyra.ai as ai_module
    from lyra.ai import AIError, test_connection

    def handler(request: httpx.Request):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    from lyra.network import NetworkState

    monkeypatch.setattr(
        ai_module, "active_network",
        lambda: NetworkState(proxy="http://10.0.1.8:7890"),
    )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AIError) as excinfo:
        test_connection(
            protocol="openai_chat",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="k",
            timeout=5,
            client=client,
            use_proxy=False,
        )
    assert "10.0.1.8" not in str(excinfo.value)
