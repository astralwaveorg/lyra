"""身份解析层（重构方案 §4.2）：这份文件是哪首歌、哪张专辑。

设计要点：

- **一次定身份，驱动三种补全 + 一次归位**。歌词支线、封面支线、归置支线
  都从这里取「这条文件是谁」的证据，而不是各自为战。
- 证据分级（E0..E4，见 REDESIGN §2.2）。本模块只做**本地零成本**的两级：
  E0（标签自证 MBID）与 E2（可信目录锚点，复用 tagscrape 的锚点证据）；
  E1 指纹 / E3 搜索 / E4 AI 由抓取与标签链路在网络阶段完成，并把结论
  **回写**到这里（`sync_from_proposals`）——网络身份判定只在需要联网的
  环节做一次，而不是每个支线各查一遍。
- 幂等：`input_hash` 是解析输入（标签关键字段 + 相对路径）的指纹，
  不变的行不重算，30 秒一轮的对账因此不会有写放大。
- 本模块**绝不联网**：它能安全地跑在对账热路径上。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .db import StateStore

__all__ = [
    "IdentityDecision",
    "identity_input_hash",
    "sync_identities",
    "resolve_track",
]

#: 提案来源 → 证据等级。tagscrape 的来源语义见其模块文档。
_SOURCE_EVIDENCE = {
    "mbid": "E0",
    "link": "E0",
    "album": "E2",
    "search": "E3",
    "fingerprint": "E1",
    "ai": "E4",
}

_MBID_TAG_KEYS = (
    "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ_RELEASETRACKID",
    "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ_RELEASEGROUPID",
)


@dataclass(slots=True)
class IdentityDecision:
    """一条文件的身份结论（identities 表的结构化视图）。"""

    state: str                    # resolved | ambiguous | unknown
    recording_id: str = ""
    release_id: str = ""
    release_group_id: str = ""
    evidence: str = ""            # E0..E4
    confidence: float | None = None
    source: str = ""
    query_used: str = ""

    @property
    def resolved(self) -> bool:
        return self.state == "resolved" and bool(self.recording_id or self.release_id)


def identity_input_hash(tags: dict[str, list[str]], rel_path: str) -> str:
    """解析输入指纹：关键字段或路径变了，身份结论就该重算。"""
    def first(key: str) -> str:
        values = tags.get(key) or []
        return values[0].strip() if values else ""

    parts = [
        first("TITLE"), first("ARTIST"), first("ALBUM"),
        first("MUSICBRAINZ_TRACKID"), first("MUSICBRAINZ_ALBUMID"),
        rel_path,
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _from_tags(tags: dict[str, list[str]]) -> IdentityDecision:
    """E0：文件自证。标签里带 MusicBrainz MBID 时无需任何网络证据。"""
    def first(key: str) -> str:
        values = tags.get(key) or []
        return values[0].strip() if values else ""

    recording = first("MUSICBRAINZ_TRACKID")
    release = first("MUSICBRAINZ_ALBUMID")
    group = first("MUSICBRAINZ_RELEASEGROUPID")
    if recording or release:
        return IdentityDecision(
            state="resolved",
            recording_id=recording,
            release_id=release,
            release_group_id=group,
            evidence="E0",
            confidence=1.0,
            source="tags_mbid",
        )
    return IdentityDecision(state="unknown")


def sync_identities(
    store: StateStore,
    *,
    root_id: int,
    locations: list,  # sequence[sqlite3.Row]
    tags_for,
) -> dict[str, int]:
    """把一批位置的身份结论同步到 identities 表（幂等）。

    `tags_for(location_row) -> dict[str, list[str]]` 由调用方提供（读标签
    的方式随上下文不同：审计批量读缓存，单文件现场读）。结论来源优先级：
    标签自证（E0）> 已有标签提案（E0/E1/E2/E3，来自标签链路的网络证据）。
    """
    report = {"resolved": 0, "unknown": 0, "unchanged": 0}
    for row in locations:
        location_id = int(row["id"])
        rel_path = str(row["rel_path"])
        tags = tags_for(row) or {}
        current_hash = identity_input_hash(tags, rel_path)
        existing = store.get_identity(location_id)
        if existing is not None and existing["input_hash"] == current_hash:
            report["unchanged"] += 1
            continue

        decision = _from_tags(tags)
        if not decision.resolved:
            proposal = store.conn.execute(
                "SELECT source, confidence, recording_id, release_id "
                "FROM tag_proposals WHERE location_id=?",
                (location_id,),
            ).fetchone()
            if proposal is not None and (proposal["recording_id"] or proposal["release_id"]):
                evidence = _SOURCE_EVIDENCE.get(str(proposal["source"]), "E3")
                decision = IdentityDecision(
                    state="resolved",
                    recording_id=str(proposal["recording_id"] or ""),
                    release_id=str(proposal["release_id"] or ""),
                    evidence=evidence,
                    confidence=float(proposal["confidence"]) if proposal["confidence"] is not None else None,
                    source=str(proposal["source"]),
                )
        store.upsert_identity(
            location_id=location_id,
            state=decision.state,
            recording_id=decision.recording_id,
            release_id=decision.release_id,
            release_group_id=decision.release_group_id,
            evidence=decision.evidence,
            confidence=decision.confidence,
            source=decision.source,
            query_used=decision.query_used,
            evidence_json=json.dumps(
                {"input": ["TITLE", "ARTIST", "ALBUM", "MBIDs", "rel_path"]},
                ensure_ascii=False,
            ),
            input_hash=current_hash,
        )
        report["resolved" if decision.resolved else "unknown"] += 1
    return report


def resolve_track(store: StateStore, location_id: int) -> IdentityDecision:
    """读取一条文件的身份结论（已缓存的视图，不做任何计算）。"""
    row = store.get_identity(location_id)
    if row is None:
        return IdentityDecision(state="unknown")
    return IdentityDecision(
        state=str(row["state"]),
        recording_id=str(row["recording_id"] or ""),
        release_id=str(row["release_id"] or ""),
        release_group_id=str(row["release_group_id"] or ""),
        evidence=str(row["evidence"] or ""),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        source=str(row["source"] or ""),
        query_used=str(row["query_used"] or ""),
    )
