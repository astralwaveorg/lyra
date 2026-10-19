"""盘点「.lrc 文件名与音频文件名不一致」的情况（只读）。

默认：lrc 名字必须和音频名字一样，后缀名不同。
这里把现状一次说清：哪些一致、哪些不一致、哪些是孤儿、哪些会撞名。
"""

from __future__ import annotations

from lyra import scan
from lyra.audit import _read_sidecar, pair_sidecars
from lyra.config import load_config

cfg = load_config()
root = cfg.enabled_roots[0]
base = root.resolved_path

audios = sorted(scan.iter_audio_files(base, root))
lrcs = sorted(p for p in base.rglob("*.lrc"))

# sidecar 目录扫描复用审计的规则（忽略规则一致）
findings = {p: _read_sidecar(p) for p in lrcs}
pairing = pair_sidecars(audios, findings)

owned = {side: audio for audio, (side, _how) in pairing.items() if side is not None}

print(f"音频 {len(audios)} 个｜.lrc {len(lrcs)} 个")
print()

exact = mismatched = 0
rows = []
for audio in audios:
    side, how = pairing.get(audio, (None, "none"))
    if side is None:
        continue
    want = scan.sidecar_path_for(audio)
    if side == want:
        exact += 1
    else:
        mismatched += 1
        rows.append((audio, side, want, how))

orphans = [p for p in lrcs if p not in owned]

print(f"名字一致的：{exact}")
print(f"名字不一致的：{mismatched}")
print(f"孤儿（无对应音频）：{len(orphans)}")
print()
if rows:
    print("== 名字不一致（需要改名）==")
    for audio, side, want, how in rows:
        collide = want.exists() and want != side
        print(f"  音频: {audio.relative_to(base)}")
        print(f"    现名: {side.name}   [{how}]")
        print(f"    应为: {want.name}" + ("   ⚠ 目标已存在（会撞名！）" if collide else ""))
if orphans:
    print()
    print("== 孤儿 lrc ==")
    for p in orphans:
        print(f"  {p.relative_to(base)}")
