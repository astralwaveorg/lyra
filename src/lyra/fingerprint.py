"""声学指纹：用音频本身确认「这条录音在 MusicBrainz 上是哪一个」。

## 为什么需要它

标签匹配的常规证据是标题、艺人、专辑名——这些**恰好就是可能出错的东西**：
曲库里既有把整串署名塞进 TITLE 的文件，也有繁体/简体、翻译名、上传者
自造的专辑名。拿坏标签去搜，搜出来的候选当然也不对。

AcoustID 的输入是**音频波形**，与文件名、目录、已有标签完全无关。
它给出的录音 ID 可以直接当作身份锚点，再用该 ID 去 MusicBrainz 取发行，
是「标签全烂但音频是真的」这类情况下唯一可靠的路径。

## 为什么不默认对全库跑

`fpcalc` 要把整首歌解码一遍，1800 首是几十分钟级 CPU；而 97% 的文件已经
有 MBID，按 MBID 查是零成本的。所以默认只在**缺 MBID / 匹配有歧义**时
兜底，并把指纹与结果缓存进库（文件大小+mtime 不变就复用）。

## 依赖

需要 `fpcalc`（Debian 包 `libchromaprint-tools`）。缺失时**明确不可用**，
不静默退化成「随便猜一个」——匹配层会据此走别的路径。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import __version__
from .config import AcoustIdProviderConfig

__all__ = [
    "AcoustIdClient",
    "AcoustIdMatch",
    "Fingerprint",
    "FingerprintError",
    "FpcalcFingerprinter",
]


class FingerprintError(RuntimeError):
    """指纹计算失败（工具缺失、文件损坏、超时）。"""


@dataclass(frozen=True, slots=True)
class Fingerprint:
    value: str
    duration: float

    def to_dict(self) -> dict:
        return {"fingerprint": self.value, "duration": self.duration}


@dataclass(frozen=True, slots=True)
class AcoustIdMatch:
    """AcoustID 的一条匹配结果。"""

    score: float
    recording_ids: tuple[str, ...] = ()
    release_group_ids: tuple[str, ...] = ()
    title: str = ""
    artists: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "recording_ids": list(self.recording_ids),
            "release_group_ids": list(self.release_group_ids),
            "title": self.title,
            "artists": list(self.artists),
        }


class FpcalcFingerprinter:
    """调用 `fpcalc` 计算 Chromaprint 指纹。

    用 `-json` 而不是解析文本输出：文本格式在不同版本间改过排版，
    而 JSON 的 `duration` / `fingerprint` 两个键是稳定的。
    """

    def __init__(
        self,
        command: str = "",
        *,
        timeout_seconds: float = 45.0,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self._command = command or "fpcalc"
        self._timeout = timeout_seconds
        self._runner = runner or subprocess.run

    @property
    def available(self) -> bool:
        if "/" in self._command:
            return Path(self._command).is_file()
        return shutil.which(self._command) is not None

    def compute(self, path: Path) -> Fingerprint:
        if not self.available:
            raise FingerprintError("未安装 fpcalc（libchromaprint-tools）")
        try:
            completed = self._runner(
                [self._command, "-json", str(path)],
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise FingerprintError("未安装 fpcalc（libchromaprint-tools）") from exc
        except subprocess.TimeoutExpired as exc:
            raise FingerprintError("指纹计算超时") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            raise FingerprintError(detail[:200] or "fpcalc 执行失败")
        try:
            data = json.loads((completed.stdout or b"").decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FingerprintError("fpcalc 输出无法解析") from exc
        fingerprint = str(data.get("fingerprint") or "").strip()
        try:
            duration = float(data.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if not fingerprint or duration <= 0:
            raise FingerprintError("fpcalc 没有给出有效指纹")
        return Fingerprint(value=fingerprint, duration=duration)


class AcoustIdClient:
    """AcoustID 查询客户端。只查询，不提交指纹。"""

    def __init__(
        self,
        config: AcoustIdProviderConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._last_request = 0.0
        self._client = httpx.Client(
            timeout=config.request_timeout_seconds,
            transport=transport,
            headers={"User-Agent": f"Lyra/{__version__} (+https://github.com/astralwaveorg/lyra)"},
        )

    @property
    def available(self) -> bool:
        return bool(
            self._config.enabled
            and self._config.api_key.strip()
            and self._config.base_url.strip()
        )

    def close(self) -> None:
        self._client.close()

    def lookup(
        self, fingerprint: str, duration: float
    ) -> Sequence[AcoustIdMatch]:
        """查询一条指纹。网络/限流失败抛 `httpx.HTTPError`，由调用方决定是否继续。"""
        if not self.available or not fingerprint.strip() or duration <= 0:
            return []
        wait = self._config.minimum_request_interval_seconds - (
            self._clock() - self._last_request
        )
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()
        url = self._config.base_url.rstrip("/") + "/v2/lookup"
        response = self._client.post(
            url,
            data={
                "client": self._config.api_key.strip(),
                "duration": str(int(round(duration))),
                "fingerprint": fingerprint,
                "meta": "recordings+releasegroups",
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return []
        matches: list[AcoustIdMatch] = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            try:
                score = float(item.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            recordings = [
                r for r in (item.get("recordings") or []) if isinstance(r, dict)
            ]
            recording_ids = tuple(
                str(r["id"]) for r in recordings if r.get("id")
            )
            release_groups = [
                rg for r in recordings for rg in (r.get("releasegroups") or [])
                if isinstance(rg, dict)
            ]
            rg_ids = tuple(
                str(rg["id"]) for rg in release_groups if rg.get("id")
            )
            title = str(recordings[0].get("title") or "") if recordings else ""
            artists: tuple[str, ...] = ()
            if recordings:
                credit = recordings[0].get("artists") or []
                artists = tuple(
                    str(a.get("name") or "") for a in credit
                    if isinstance(a, dict) and a.get("name")
                )
            matches.append(
                AcoustIdMatch(
                    score=score,
                    recording_ids=recording_ids,
                    release_group_ids=rg_ids,
                    title=title,
                    artists=artists,
                )
            )
        matches.sort(key=lambda m: -m.score)
        return matches
