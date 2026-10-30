"""Lyra 命令行入口。

命令语义（对应 架构约束；中文模块名见 docs）：
    config check   校验配置并打印生效值（凭据掩码）  —— 校勘
    config init    生成带注释的 lyra.toml 模板
    audit          只读库审计：分类、统计、健康检查   —— 校勘（不联网）

`audit` 是本阶段最安全的入口：**绝不修改任何文件、绝不联网**。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .audit import AuditResult, audit_root, render_audit_text
from .config import (
    DEFAULT_CONFIG_FILENAME,
    Config,
    ConfigError,
    apply_ai_settings_values,
    apply_cover_policy_values,
    apply_lyrics_settings_values,
    apply_organize_policy_values,
    apply_tag_policy_values,
    load_config,
)
from .db import open_store
from .network import (
    active_network,
    environment_proxy,
    load_network_settings,
    mask_proxy_url,
    proxy_bypass_hosts,
    read_saved_network,
    set_active_network,
)

__all__ = ["main", "build_parser"]

TEMPLATE_RELATIVE = Path("templates") / "lyra.example.toml"


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lyra",
        description="Lyra — 音乐库歌词整理与刮削服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "所有路径与凭据都来自配置文件，源码内不含任何绝对路径。\n"
            "配置优先级：环境变量 LYRA_* > 配置文件 > 内置默认值\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"lyra {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help=f"配置文件路径（默认 $LYRA_CONFIG，再默认 ./{DEFAULT_CONFIG_FILENAME}）",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # -- config --
    p_config = sub.add_parser("config", help="配置管理")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)

    p_check = config_sub.add_parser("check", help="校验配置并打印生效值")
    p_check.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_check.set_defaults(func=cmd_config_check)

    p_init = config_sub.add_parser("init", help="生成带注释的配置模板")
    p_init.add_argument(
        "-o", "--output", default=DEFAULT_CONFIG_FILENAME, help="输出路径"
    )
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p_init.set_defaults(func=cmd_config_init)

    # -- audit --
    p_audit = sub.add_parser("audit", help="只读库审计（不修改文件、不联网）")
    p_audit.add_argument("--root", metavar="NAME", help="只审计指定名称的受管根")
    p_audit.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_audit.add_argument(
        "--show", type=int, default=15, metavar="N", help="文本报告显示的待处理曲目数"
    )
    p_audit.set_defaults(func=cmd_audit)

    # -- fetch --
    p_fetch = sub.add_parser(
        "fetch",
        help="抓取歌词到服务暂存区（不修改任何音乐文件）",
        description=(
            "只下载与匹配，结果落到服务数据目录。此命令**绝不修改音乐**，"
            "因此可以放心常开。真正落盘用 `lyra apply`。"
        ),
    )
    p_fetch.add_argument("--root", metavar="NAME", help="只处理指定受管根")
    p_fetch.add_argument("--limit", type=int, metavar="N", help="最多处理 N 首")
    p_fetch.add_argument(
        "--force", action="store_true",
        help="即使已有有效同步歌词也重新搜索（默认跳过，因为此时写入无收益）",
    )
    p_fetch.add_argument("--json", action="store_true")
    p_fetch.set_defaults(func=cmd_fetch)

    # -- apply --
    p_apply = sub.add_parser(
        "apply",
        help="把已抓取的歌词写入音乐（备份 + 事务 + 写入后复核）",
        description=(
            "对暂存区里「已接受」的结果执行写入：先完整备份原文件，"
            "再校验压缩音频与非歌词元数据不变，任一步失败即回滚。"
        ),
    )
    p_apply.add_argument("--root", metavar="NAME")
    p_apply.add_argument("--limit", type=int, metavar="N", help="最多写入 N 首")
    p_apply.set_defaults(func=cmd_apply)

    # -- tags --
    p_tags = sub.add_parser(
        "tags",
        help="标签刮削：按 MusicBrainz 扫描、对照并补齐标签",
        description=(
            "扫描曲库，生成逐字段提案；默认只补空、不覆盖已有值。"
            "有冲突的留在待审查里，由人工确认。"
        ),
    )
    tags_sub = p_tags.add_subparsers(dest="tags_command")
    p_tags_scan = tags_sub.add_parser("scan", help="扫描并按策略自动写入")
    p_tags_scan.add_argument("--root", metavar="NAME")
    p_tags_scan.add_argument("--limit", type=int, metavar="N")
    p_tags_scan.add_argument(
        "--dry-run", action="store_true", help="只生成提案，不写文件"
    )
    p_tags_scan.add_argument(
        "--no-fingerprint", action="store_true", help="不调用 AcoustID 指纹"
    )
    p_tags_scan.add_argument("--json", action="store_true")
    p_tags_scan.set_defaults(func=cmd_tags_scan)

    # -- watch --
    p_watch = sub.add_parser(
        "watch",
        help="常驻：递归监听 + 稳定判定 + 周期对账 + 每日完整核对",
        description=(
            "监听到稳定的新文件后自动抓取，并按配置决定是否自动写入。"
            "写开关关闭时仍然抓取与暂存，不碰音乐。"
        ),
    )
    p_watch.add_argument("--root", metavar="NAME", help="只监听指定受管根")
    p_watch.add_argument(
        "--fetch-only", action="store_true", help="只抓取不写入（即使写开关是开的）"
    )
    p_watch.set_defaults(func=cmd_watch)

    # -- status --
    p_status = sub.add_parser("status", help="查看状态库、队列与事务健康")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    # -- serve --
    p_serve = sub.add_parser(
        "serve",
        help="启动 Web 界面与 JSON API",
        description=(
            "只监听配置里的 bind（默认 127.0.0.1），由反向代理对外提供 HTTPS。"
            "首次启动会生成随机管理员密码，写入数据目录 initial-password.txt。"
        ),
    )
    p_serve.add_argument("--bind", help="覆盖配置里的监听地址")
    p_serve.add_argument("--port", type=int, help="覆盖配置里的端口")
    p_serve.set_defaults(func=cmd_serve)

    # -- admin-password --
    p_pw = sub.add_parser("admin-password", help="查看/重置管理员密码")
    p_pw.add_argument("--username", default=None, help="默认取配置里的 web.admin_username")
    p_pw.add_argument(
        "--password",
        help="直接设置为指定值（省缺则用配置里的 web.admin_password；都没有才生成随机密码）",
    )
    p_pw.add_argument(
        "--show-file", action="store_true",
        help="只显示初始密码文件的内容（首次部署后忘记密码时用）",
    )
    p_pw.set_defaults(func=cmd_admin_password)

    # -- token --
    p_tok = sub.add_parser("token", help="签发/管理 API Bearer Token")
    tok_sub = p_tok.add_subparsers(dest="token_command", required=True)
    p_tok_new = tok_sub.add_parser("create", help="签发一个新 Token")
    p_tok_new.add_argument("--label", default="api")
    p_tok_new.add_argument("--ttl-days", type=int, default=365)
    p_tok_new.set_defaults(func=cmd_token_create)
    p_tok_list = tok_sub.add_parser("list", help="列出 Token（不显示 Token 本身）")
    p_tok_list.set_defaults(func=cmd_token_list)
    p_tok_revoke = tok_sub.add_parser("revoke", help="吊销 Token")
    p_tok_revoke.add_argument("token_id", type=int)
    p_tok_revoke.set_defaults(func=cmd_token_revoke)

    # -- stats --
    p_stats = sub.add_parser(
        "stats",
        help="全库统计：音乐文件数、LRC 歌词文件数、覆盖率与问题分布",
    )
    p_stats.add_argument("--root", metavar="NAME", help="只统计指定受管根")
    p_stats.add_argument("--json", action="store_true")
    p_stats.set_defaults(func=cmd_stats)

    # -- fix-ext --
    p_fix = sub.add_parser(
        "fix-ext",
        help="修正扩展名与真实容器不符的音频（默认只预览，不改动）",
        description=(
            "只做一件事：让扩展名与**实际容器**一致。"
            "不按刮削结果重命名，不改文件名主体，因此同名 .lrc 配对不受影响。"
        ),
    )
    p_fix.add_argument("--root", metavar="NAME", help="只处理指定名称的受管根")
    p_fix.add_argument(
        "--apply", action="store_true", help="真正执行改名（不加此参数则仅预览）"
    )
    p_fix.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_fix.set_defaults(func=cmd_fix_ext)

    # -- fill-lrc --
    p_fill = sub.add_parser(
        "fill-lrc",
        help="把内嵌歌词补成同名 .lrc（默认只预览，不改动）",
        description=(
            "库里很多文件整理时就把带时间轴的歌词写进了标签，而播放器、车机"
            "只认外挂 .lrc。这条命令把内嵌的那份原样落成 .lrc："
            "不联网、不改音频，已有内容的一律不覆盖。"
        ),
    )
    p_fill.add_argument("--root", metavar="NAME", help="只处理指定名称的受管根")
    p_fill.add_argument(
        "--apply", action="store_true", help="真正写文件（不加此参数则仅预览）"
    )
    p_fill.add_argument("--limit", type=int, metavar="N", help="本次最多处理多少首")
    p_fill.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_fill.set_defaults(func=cmd_fill_lrc)

    # -- undo-rename --
    p_undo = sub.add_parser("undo-rename", help="撤销一次修正性改名")
    p_undo.add_argument("rename_id", type=int, help="renames 表里的记录 id")
    p_undo.set_defaults(func=cmd_undo_rename)

    return parser


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def cmd_config_check(args: argparse.Namespace, cfg: Config) -> int:
    # 设置页保存的大模型设置优先于配置文件；读不到就算了，不影响检查。
    # **只读打开**：检查命令不该创建或迁移数据库。
    ai_use_proxy = cfg.ai.use_proxy
    ai_from_settings = False
    db_path = cfg.storage.resolved_data_dir / "lyra.db"
    if db_path.is_file():
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='settings.ai'"
                ).fetchone()
            finally:
                conn.close()
            if row:
                saved = json.loads(row[0])
                if isinstance(saved, dict) and isinstance(saved.get("use_proxy"), bool):
                    ai_use_proxy = bool(saved["use_proxy"])
                    ai_from_settings = True
        except Exception:  # noqa: BLE001 — 检查命令不该因为读不到数据库就失败
            pass

    if args.json:
        print(json.dumps(cfg.to_dict(mask_secrets=True), ensure_ascii=False, indent=2))
        return 0

    print(f"配置文件: {cfg.source_path if cfg.source_path else '（未使用配置文件，全部为默认值/环境变量）'}")
    print(f"配置版本: {cfg.config_version}")
    print()
    print("受管根:")
    for root in cfg.roots:
        flags = []
        flags.append("启用" if root.enabled else "停用")
        flags.append("只读" if root.read_only else "可写")
        print(f"  - {root.name} [{root.role.value}] {flags[0]}/{flags[1]}")
        print(f"      path      = {root.path}")
        if root.host_path:
            print(f"      host_path = {root.host_path}")
        if root.ignore_dirs:
            print(f"      ignore_dirs = {', '.join(root.ignore_dirs)}")
    print()
    print(f"数据目录: {cfg.storage.data_dir}")
    if cfg.network.proxy:
        print(f"网络代理: {mask_proxy_url(cfg.network.proxy)}（{cfg.network.proxy_source}）")
        enabled = "、".join(
            name for name in sorted(cfg.network.use_proxy) if cfg.network.use_proxy[name]
        )
        print(f"  走代理的功能: {enabled or '无'}")
        print(
            f"  大模型走代理: {'是' if ai_use_proxy else '否'}"
            + ("（设置页）" if ai_from_settings else "（配置文件）")
        )
    elif cfg.network.proxy_source == "env":
        print("网络代理: 沿用环境变量")
    else:
        print("网络代理: 未设置")
    print(f"歌词来源: {', '.join(cfg.providers.enabled)}")
    lr = cfg.providers.lrclib
    print(f"  lrclib 标识: {lr.client_identity()}")
    print(f"  lrclib 授权: {'已配置' if lr.authorization_ref else '未配置'}")
    print(f"  acoustid: {'已配置' if cfg.providers.acoustid.api_key else '未配置'}")
    print()
    print(
        "写入策略: "
        f"write_enabled={cfg.policy.write_enabled} "
        f"mode={cfg.policy.write_mode} "
        f"sidecar={cfg.policy.write_sidecar_lrc} "
        f"embed={cfg.policy.embed_lyrics} "
        f"skip_valid_synced={cfg.policy.skip_if_valid_synced}"
    )
    print(
        "匹配闸门: "
        f"时长差<={cfg.policy.matching.max_duration_delta_seconds}s "
        f"最低置信={cfg.policy.matching.min_confidence} "
        f"最小分差={cfg.policy.matching.min_score_margin}"
    )
    print(f"Web: {cfg.web.bind}:{cfg.web.port} auth={cfg.web.auth_required}")
    print()
    print("✅ 配置校验通过")
    return 0


def cmd_config_init(args: argparse.Namespace, cfg: Config | None) -> int:
    template = _template_path()
    if not template.is_file():
        print(f"错误: 找不到配置模板 {template}", file=sys.stderr)
        return 2

    target = Path(args.output).expanduser()
    if target.exists() and not args.force:
        print(f"错误: {target} 已存在（用 --force 覆盖）", file=sys.stderr)
        return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, target)
    print(f"已生成配置模板: {target}")
    print()
    print("接下来：")
    print(f"  1. 编辑 {target}，至少设置 roots[].path 与 storage.data_dir")
    print(f"  2. lyra --config {target} config check")
    print(f"  3. lyra --config {target} audit")
    print()
    print("注意：该文件可能包含授权凭据，已在 .gitignore 中排除，请勿提交到公开仓库。")
    return 0


def _template_path() -> Path:
    return Path(__file__).resolve().parent / TEMPLATE_RELATIVE


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def cmd_audit(args: argparse.Namespace, cfg: Config) -> int:
    roots = cfg.enabled_roots
    if args.root:
        roots = [r for r in roots if r.name == args.root]
        if not roots:
            print(f"错误: 没有名为 {args.root!r} 的受管根", file=sys.stderr)
            return 2
    if not roots:
        print("错误: 没有启用的受管根", file=sys.stderr)
        return 2

    results: list[AuditResult] = []
    exit_code = 0
    for root in roots:
        base = root.resolved_path
        if not base.is_dir():
            print(f"错误: 受管根不存在或不是目录: {base}", file=sys.stderr)
            exit_code = 1
            continue
        result = audit_root(root)
        results.append(result)

    if args.json:
        payload = {
            "version": __version__,
            "config": str(cfg.source_path) if cfg.source_path else None,
            "roots": [_audit_to_dict(r) for r in results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(render_audit_text(result, max_tracks=args.show))

    return exit_code


def _audit_to_dict(result: AuditResult) -> dict[str, Any]:
    s = result.summary
    return {
        "root_name": result.root_name,
        "root_path": result.root_path,
        "summary": {
            "tracks_total": s.tracks_total,
            "sidecar_total": s.sidecar_total,
            "sidecar_empty": s.sidecar_empty,
            "tracks_without_lyrics": s.tracks_without_lyrics,
            "parse_failures": s.parse_failures,
            "by_format": s.by_format,
            "by_lyrics_state": s.by_lyrics_state,
            "by_issue": s.by_issue,
        },
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "empty_sidecars": result.empty_sidecars,
        "orphan_sidecars": result.orphan_sidecars,
        "duplicate_sidecars": [
            {"directory": d, "files": files} for d, files in result.duplicate_sidecars
        ],
        "bom_sidecars": result.bom_sidecars,
        "unreadable": [{"path": p, "reason": r} for p, r in result.unreadable],
        "tracks": [
            {
                "rel_path": t.audio.rel_path,
                "format": t.audio.media_format.value,
                "extension": t.audio.extension,
                "duration": t.audio.duration,
                "size": t.audio.size,
                "effective_state": t.effective_state.value,
                "embedded_state": t.embedded.state.value,
                "embedded_fields": list(t.embedded.fields),
                "sidecar_state": t.sidecar.state.value if t.sidecar.exists else None,
                "sidecar_size": t.sidecar.size if t.sidecar.exists else None,
                "tags": t.tags,
                "issues": [i.value for i in t.issues],
                "lyrics_problems": list(t.embedded.parsed.problems) if t.embedded.parsed else [],
            }
            for t in result.tracks
        ],
    }


# ---------------------------------------------------------------------------
# fetch / apply / status
# ---------------------------------------------------------------------------


def _make_providers(cfg: Config):
    from .providers import create_providers

    if not cfg.providers.enabled:
        print("错误: 配置里没有启用任何歌词来源", file=sys.stderr)
        return None
    return create_providers(cfg.providers)


def _print_summary(label: str, s) -> None:  # noqa: ANN001
    print()
    print(f"-- {label} --")
    print(f"  扫描目标        {s.scanned}")
    print(f"  实际处理        {s.considered}")
    if label == "抓取结果":
        print(f"  成功抓取        {s.fetched}")
        print(f"    ├ 高置信接受  {s.accepted}")
        print(f"    └ 待审查      {s.needs_review}")
        print(f"  未找到          {s.not_found}")
        print(f"  无同步歌词      {s.no_synced}")
        print(f"  命中负缓存跳过  {s.skipped_cached}")
        print(f"  已有同步歌词跳过 {s.skipped_satisfied}")
        print(f"  被规则排除      {s.skipped_excluded}")
    else:
        print(f"  已写入          {s.applied}")
        print(f"  写入失败        {s.apply_failed}")
        print(f"  已满足跳过      {s.skipped_satisfied}")
    if s.provider_disabled:
        print("  本轮停用的来源（避免请求风暴）:")
        for name, why in s.provider_disabled.items():
            print(f"    - {name}: {why[:100]}")
    if s.errors:
        print(f"  错误 {len(s.errors)} 条:")
        for e in s.errors[:5]:
            print(f"    - {e[:140]}")
    print(f"  耗时            {s.elapsed_seconds:.1f}s")


def cmd_fetch(args: argparse.Namespace, cfg: Config) -> int:
    from .pipeline import run_fetch

    providers = _make_providers(cfg)
    if providers is None:
        return 2
    roots = _select_roots(cfg, args.root)
    if not roots:
        print("错误: 没有匹配的受管根", file=sys.stderr)
        return 2

    store = open_store(cfg.storage.resolved_data_dir)
    payload = []
    try:
        for root in roots:
            summary = run_fetch(
                store, cfg, root, providers,
                limit=args.limit, force=args.force,
                progress=lambda m: print(f"  {m}", file=sys.stderr),
            )
            if not args.json:
                print(f"===== 抓取 root={root.name} =====")
                _print_summary("抓取结果", summary)
            payload.append(
                {
                    "root": root.name,
                    "scanned": summary.scanned,
                    "considered": summary.considered,
                    "fetched": summary.fetched,
                    "accepted": summary.accepted,
                    "needs_review": summary.needs_review,
                    "not_found": summary.not_found,
                    "no_synced": summary.no_synced,
                    "skipped_cached": summary.skipped_cached,
                    "skipped_satisfied": summary.skipped_satisfied,
                    "skipped_excluded": summary.skipped_excluded,
                    "provider_disabled": summary.provider_disabled,
                    "errors": summary.errors,
                }
            )
    finally:
        store.close()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print()
        print("注意：本次未修改任何音乐文件。确认结果后用 `lyra apply` 落盘。")
    return 0


def cmd_apply(args: argparse.Namespace, cfg: Config) -> int:
    from .pipeline import run_apply

    roots = _select_roots(cfg, args.root)
    if not roots:
        print("错误: 没有匹配的受管根", file=sys.stderr)
        return 2
    if not cfg.policy.write_enabled:
        print(
            "错误: policy.write_enabled=false，全库只读。\n"
            "      这是总开关，优先于任何批准记录。若确实要写入，"
            "请在配置里显式打开。",
            file=sys.stderr,
        )
        return 2

    store = open_store(cfg.storage.resolved_data_dir)
    had_failure = False
    try:
        for root in roots:
            if root.read_only:
                print(f"  跳过 {root.name}: 该根标记为只读")
                continue
            summary = run_apply(
                store, cfg, root, limit=args.limit,
                progress=lambda m: print(f"  {m}", file=sys.stderr),
            )
            print(f"===== 写入 root={root.name} =====")
            _print_summary("写入结果", summary)
            had_failure = had_failure or summary.apply_failed > 0
    finally:
        store.close()
    return 1 if had_failure else 0


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        counts = store.table_counts()
        staged = {
            row["state"]: row["n"]
            for row in store.conn.execute(
                "SELECT state, COUNT(*) AS n FROM staged_lyrics GROUP BY state"
            )
        }
        outcomes = {
            row["outcome"]: row["n"]
            for row in store.conn.execute(
                "SELECT outcome, COUNT(*) AS n FROM resolutions GROUP BY outcome ORDER BY n DESC"
            )
        }
        unfinished = store.unfinished_transactions()
        roots = [
            dict(r) for r in store.conn.execute("SELECT * FROM roots ORDER BY name")
        ]
        recent = [
            dict(r)
            for r in store.conn.execute(
                "SELECT rel_path, provider, score, duration_delta FROM staged_lyrics s "
                "JOIN locations l ON l.id = s.location_id "
                "WHERE s.state='needs_review' ORDER BY s.created_at DESC LIMIT 10"
            )
        ]
        active_renames = list(store.iter_renames())

        if args.json:
            print(json.dumps(
                {
                    "schema_version": store.current_schema_version(),
                    "integrity": store.integrity_check(),
                    "tables": counts,
                    "staged_by_state": staged,
                    "resolutions_by_outcome": outcomes,
                    "unfinished_transactions": len(unfinished),
                    "roots": roots,
                    "needs_review_recent": recent,
                    "active_renames": len(active_renames),
                },
                ensure_ascii=False, indent=2,
            ))
            return 0

        print("=" * 72)
        print("Lyra 状态")
        print("=" * 72)
        print(f"数据目录          {cfg.storage.resolved_data_dir}")
        print(f"schema 版本       {store.current_schema_version()}")
        print(f"完整性检查        {store.integrity_check()}")
        print(f"写入总开关        {'开' if cfg.policy.write_enabled else '关（只读）'}")
        print()
        print("-- 受管根 --")
        for r in roots:
            print(f"  {r['name']:12} {r['role']:8} health={r['health']:8} {r['path']}")
        print()
        print("-- 歌词状态（累计观察）--")
        print(f"  曲目位置        {counts.get('locations', 0)}")
        print(f"  歌词观察        {counts.get('lyric_observations', 0)}")
        print()
        print("-- 暂存区 --")
        if staged:
            for state, n in sorted(staged.items(), key=lambda kv: -kv[1]):
                print(f"  {state:20} {n}")
        else:
            print("  （空，尚未抓取或已全部落盘）")
        print()
        print("-- 来源解析结果（含负缓存）--")
        if outcomes:
            for outcome, n in outcomes.items():
                print(f"  {outcome:20} {n}")
        else:
            print("  （无）")
        print()
        print(f"-- 未完成事务 {len(unfinished)} 个 --")
        for row in unfinished[:5]:
            print(f"  {row['txn_id'][:12]} {row['state']:20} {row['error'] or ''}"[:110])
        print()
        print(f"-- 生效中的修正性改名 {len(active_renames)} 条 --")
        for row in active_renames[:5]:
            print(f"  [{row['id']}] {row['old_rel_path']} → {row['new_rel_path']}")
        if recent:
            print()
            print("-- 最近待审查（需你确认版本）--")
            for r in recent:
                print(
                    f"  score={r['score']:.3f} Δt={r['duration_delta']} "
                    f"[{r['provider']}] {r['rel_path']}"[:120]
                )
        print()
        return 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def cmd_stats(args: argparse.Namespace, cfg: Config) -> int:
    from .stats import collect_stats, render_stats_text

    roots = _select_roots(cfg, args.root)
    if not roots:
        print("错误: 没有匹配的受管根", file=sys.stderr)
        return 2

    payload = []
    for root in roots:
        base = root.resolved_path
        if not base.is_dir():
            print(f"错误: 受管根不存在或不是目录: {base}", file=sys.stderr)
            continue
        stats = collect_stats(root)
        payload.append(stats.to_dict())
        if not args.json:
            print(render_stats_text(stats))
            print()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


_WRITE_LOCK = "write"
"""写租约的名字。全库同一时刻只允许一个写入者。"""

_WATCH_OWNER = "watch"
"""监控进程的租约持有者标识，用于区分「锁是我自己持有的」。"""


def _beat(store: object, stage: str, detail: str, **counters: int) -> None:
    """记录监控进程的活动心跳。

    监控在后台自动抓取与写入，但界面上原本看不到任何痕迹——
    使用者无法区分「它正常运行」和「它三天前就挂了」。
    心跳让这件事变成可观察的。

    心跳失败**绝不能**影响监控本身，所以整体吞掉异常。
    """
    import json as _json
    import time as _time

    try:  # noqa: SIM105
        store.set_meta("watch.heartbeat_at", str(_time.time()))  # type: ignore[attr-defined]
        store.set_meta("watch.stage", stage)  # type: ignore[attr-defined]
        store.set_meta("watch.detail", detail)  # type: ignore[attr-defined]
        if counters:
            store.set_meta("watch.counters", _json.dumps(counters))  # type: ignore[attr-defined]
        # 同时记一条历史。心跳只保留「最后一次」，而使用者真正想知道的是
        # 「过去这一天它做了什么」——只留最后一次活动是看不出这个的。
        store.log_watch_event(stage, detail, counters)  # type: ignore[attr-defined]
        store.prune_watch_log()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _touch(store: object) -> None:
    """只刷新心跳时间，**不改**当前阶段与说明。

    为什么需要它：心跳原来只在「启动」和「写入」时更新。库里没有写入活动时，
    30 分钟过去界面就显示「心跳异常」——而进程好好的，只是没活干。
    对账循环每 30 秒跑一次，正好用它报一次「我还活着」，
    但不能覆盖 `stage`（那会把「已写入 12 首」冲成「监听中」）。
    """
    import time as _time

    try:  # noqa: SIM105
        store.set_meta("watch.heartbeat_at", str(_time.time()))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — 心跳失败不能影响监控
        pass  # noqa: SIM105


@contextlib.contextmanager
def _watch_lease(store: object, label: str = "watch"):
    """在监控的一轮批处理期间**持有**写租约。

    只查不取是不够的：监控原来只在开工前检查「别人是否持有租约」，
    自己从不持有——于是 Web 端的批量任务在同一时刻也能开工，两边短暂并
    发写库。实际一次 12 首的写入里 9 首被判「数据库持续被占用」。
    取租约之后，任务端的 `acquire_write_lock` 会直接失败并说明持有者。
    """
    if not store.acquire_write_lock(_WRITE_LOCK, _WATCH_OWNER, ttl_seconds=7200):  # type: ignore[attr-defined]
        holder = store.write_lock_holder(_WRITE_LOCK)  # type: ignore[attr-defined]
        print(f"[watch] {label}: 另一个写入任务正在进行（{holder}），本轮跳过",
              flush=True)
        yield False
        return
    try:
        yield True
    finally:
        store.release_write_lock(_WRITE_LOCK, _WATCH_OWNER)  # type: ignore[attr-defined]


def cmd_tags_scan(args: argparse.Namespace, cfg: Config) -> int:
    """CLI 入口：与网页「开始扫描」走同一套逻辑。"""
    from .fingerprint import AcoustIdClient, FpcalcFingerprinter
    from .tagscrape import scan_tags
    from .tagsources import MusicBrainzConfig, MusicBrainzProvider

    roots = _select_roots(cfg, args.root)
    if not roots:
        print("错误: 没有匹配的受管根", file=sys.stderr)
        return 2
    mb_cfg = cfg.providers.musicbrainz
    if not mb_cfg.enabled or not mb_cfg.base_url.strip():
        print("错误: 未配置 MusicBrainz", file=sys.stderr)
        return 2

    store = open_store(cfg.storage.resolved_data_dir)
    # 设置页保存的策略优先于配置文件，CLI 与网页/监控保持一致。
    raw_policy = store.get_meta("settings.tags_policy")
    if raw_policy:
        with contextlib.suppress(ValueError, TypeError, json.JSONDecodeError):
            apply_tag_policy_values(cfg, json.loads(raw_policy))
    provider = MusicBrainzProvider(
        MusicBrainzConfig(
            base_url=mb_cfg.base_url,
            request_timeout_seconds=mb_cfg.request_timeout_seconds,
            minimum_request_interval_seconds=mb_cfg.minimum_request_interval_seconds,
            search_limit=mb_cfg.search_limit,
            detail_limit=mb_cfg.detail_limit,
        )
    )
    fingerprinter = (
        FpcalcFingerprinter()
        if cfg.policy.tags.fingerprint and not args.no_fingerprint
        else None
    )
    acoustid = (
        AcoustIdClient(cfg.providers.acoustid)
        if cfg.policy.tags.fingerprint
        and not args.no_fingerprint
        and cfg.providers.acoustid.enabled
        else None
    )
    out: list[dict[str, Any]] = []
    try:
        for root in roots:
            print(f"[tags] 扫描 {root.name}…", flush=True)
            result = scan_tags(
                cfg,
                root,
                store,
                provider,
                auto_write=not args.dry_run and cfg.policy.tags.auto_write,
                limit=args.limit,
                progress=lambda message: print(f"[tags] {message}", flush=True),
                fingerprinter=fingerprinter,
                acoustid=acoustid,
            )
            out.append({"root": root.name, **result.to_dict()})
            if not args.json:
                data = result.to_dict()
                print(
                    f"[tags] {root.name}: 扫描 {data['scanned']}，"
                    f"可自动 {data['auto']}，待审查 {data['review']}，"
                    f"未匹配 {data['unresolved']}，写入 {data['written']}"
                    f"（{data['written_fields']} 个字段）"
                )
                for line in data["errors"][:10]:
                    print(f"[tags] ! {line}", file=sys.stderr)
        if args.limit is None and out and all(item.get("scanned") for item in out):
            # 只有全库扫描才更新「上次扫描」；限量试跑不该把自动调度推迟一天。
            store.set_meta("tags.last_scan_at", str(time.time()))
            store.set_meta(
                "tags.last_scan_summary",
                json.dumps({"roots": out}, ensure_ascii=False),
            )
        if args.json:
            print(json.dumps({"roots": out}, ensure_ascii=False, indent=2))
        return 0
    finally:
        provider.close()
        if acoustid is not None:
            acoustid.close()
        store.close()


def probe_providers(store, providers) -> None:
    """来源烟雾探测：对每个已启用来源发一次最小搜索，结果记进健康表。

    查询必须构造 **TrackQuery** 数据类——此前误传普通 dict，适配器访问
    `q.title` 时抛 AttributeError，被兜底捕获后把「探测代码自身的 Bug」
    记成了「来源结构变更」，健康面板显示了这条误导信息。
    """
    from .providers.base import ProviderBlockedError, ProviderError, TrackQuery

    query = TrackQuery(
        title="Lyra",
        artists=("probe",),
        album="",
        duration=None,
        musicbrainz_trackid="",
        isrc="",
    )
    for provider in providers:
        name = getattr(provider, "name", "")
        if not name:
            continue
        try:
            provider.search(query)
        except ProviderBlockedError as exc:
            store.bump_provider_health(name, blocked=True, error=str(exc)[:200])
            print(f"[watch] 来源 {name} 烟雾探测失败: {exc}", flush=True)
            continue
        except ProviderError as exc:
            # 需要登录 / 配额用尽 / 结构变化：不是「被风控」，别误导切网关
            store.bump_provider_health(name, schema_error=True, error=str(exc)[:200])
            print(f"[watch] 来源 {name} 烟雾探测失败: {exc}", flush=True)
            continue
        except Exception as exc:  # noqa: BLE001 — 探测绝不影响主循环
            store.bump_provider_health(
                name, schema_error=True, error=f"{type(exc).__name__}: {exc}"[:200]
            )
            continue
        # 探测词本身查无结果是正常的：接口可达即算健康；
        # 成功的探测同时清掉 last_error——「最近一次探测恢复了」比
        # 「留着昨天那条错误」更有信息量
        store.bump_provider_health(name, ok=True)


def cmd_watch(args: argparse.Namespace, cfg: Config) -> int:
    import signal
    import threading

    from .ingest import finish_new_files
    from .pipeline import run_apply, run_fetch
    from .providers import create_providers
    from .watcher import LibraryWatcher

    roots = _select_roots(cfg, args.root)
    if not roots:
        print("错误: 没有匹配的受管根", file=sys.stderr)
        return 2

    load_network_settings(cfg)
    store = open_store(cfg.storage.resolved_data_dir)
    providers = create_providers(cfg.providers)
    stop = threading.Event()
    lock = threading.Lock()

    write_enabled = cfg.policy.write_enabled and not args.fetch_only

    def _apply_saved_network() -> None:
        """设置页保存代理后，换掉已经建立的来源客户端。

        代理是 ``httpx.Client`` 构造时定下来的，之后改设置不会影响旧
        客户端；因此这里重建一份，并在写入锁里替换，避免关闭正在使用的
        连接。监控进程每 30 秒检查一次，Web 端保存后最多等半分钟。
        """
        saved, features = read_saved_network(cfg.storage.resolved_data_dir)
        if saved is None and features is None:
            return
        if saved is not None:
            cfg.network.proxy = saved
        if features is not None:
            cfg.network.use_proxy.update(features)
        before = active_network()
        set_active_network(
            cfg.network.proxy or environment_proxy(),
            cfg.network.use_proxy,
            proxy_bypass_hosts(cfg),
        )
        after = active_network()
        if (before.proxy, before.features) == (after.proxy, after.features):
            return
        cfg.network.proxy_source = (
            "settings" if after.proxy else ("env" if environment_proxy() else "none")
        )
        fresh = create_providers(cfg.providers)
        with lock:
            old = providers[:]
            providers[:] = fresh
        for provider in old:
            provider.close()
        enabled = "、".join(
            name for name in sorted(after.features) if after.features[name]
        )
        print(
            f"[watch] 代理设置已更新："
            f"{mask_proxy_url(after.proxy) if after.proxy else '未设置'}"
            f"（走代理的功能：{enabled or '无'}）",
            flush=True,
        )

    def _apply_saved_tag_policy() -> None:
        raw = store.get_meta("settings.tags_policy")
        if not raw:
            return
        try:
            apply_tag_policy_values(cfg, json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"[watch] 标签策略读取失败，沿用配置文件：{exc}", flush=True)

    def _apply_saved_ai_settings() -> None:
        """设置页保存的大模型设置优先于配置文件。

        不读的话，监控进程会一直用配置文件里的旧值——比如设置页里把
        「使用网络代理」关掉了，监控这边还在走代理。
        """
        raw = store.get_meta("settings.ai")
        if not raw:
            return
        try:
            apply_ai_settings_values(cfg, json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"[watch] 大模型设置读取失败，沿用配置文件：{exc}", flush=True)

    def _apply_saved_pipeline_settings() -> None:
        """封面 / 归置 / 歌词输出 / 指纹 key 的设置页保存值。

        与标签策略同一条规则：设置页保存的值写进了数据库却不回读，
        重启后就静默回到配置文件默认——用户开了「自动归位」，重启后
        归置模式悄悄变回 off，新文件不再归位还看不出原因。
        """
        loaders = (
            ("settings.cover_policy", apply_cover_policy_values, "封面"),
            ("settings.organize_policy", apply_organize_policy_values, "归置"),
            ("settings.lyrics_format", apply_lyrics_settings_values, "歌词输出"),
        )
        for key, apply_fn, label in loaders:
            raw = store.get_meta(key)
            if not raw:
                continue
            try:
                apply_fn(cfg, json.loads(raw))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                print(f"[watch] {label}设置读取失败，沿用配置文件：{exc}", flush=True)
        saved_key = store.get_meta("settings.acoustid_key")
        if saved_key and not os.environ.get("LYRA_ACOUSTID_API_KEY"):
            cfg.providers.acoustid.api_key = saved_key
        if store.get_meta("settings.acoustid_enabled") is not None:
            cfg.providers.acoustid.enabled = (
                store.get_meta("settings.acoustid_enabled") == "1"
            )

    tag_policy_revision = store.get_meta("settings.revision") or ""
    _apply_saved_tag_policy()
    _apply_saved_ai_settings()
    _apply_saved_pipeline_settings()

    def _process_new_files(root, paths, *, lock):  # noqa: ANN001, ANN202
        """对「本轮新出现的文件」走完整流程：先本地补 .lrc，再联网抓取剩下的。

        顺序很重要：库里绝大多数文件在整理时就带了内嵌歌词，**补 .lrc 是
        纯本地操作**（毫秒级、不联网、不改音频）；先做这一步，联网抓取
        自然只剩真正没有歌词的那几首。反过来先联网，就会对 160 多首
        「本来就有词」的文件做无意义的网络请求。
        """

        # **分批抓取、分批写入。** 原来是把整批（实际 1800 首）抓完再统一
        # 写入：跑了一个小时，磁盘上一份新歌词都没有——使用者看到的就是
        # 「卡住了」。每 50 首落一次盘，进度肉眼可见，且中途重启只丢一小段。
        ordered = list(paths)
        chunk_size = 50
        for start in range(0, len(ordered), chunk_size):
            chunk = ordered[start:start + chunk_size]
            with _watch_lease(store, root.name) as got:
                if not got:
                    return
                yielded = _run_batch(root, chunk, lock=lock, done=start)
            if yielded:
                # 人工在等写入：本轮到此为止，剩下的下一轮再说。
                # 必须**先释放租约**再返回——否则人工还要等到 TTL。
                print("[watch] 有人工写入在等，已让出写锁", flush=True)
                return
        return

    # 人工写入请求的检查：每个文件都查库没必要，1 秒一次足够快。
    _yield_state = {"at": 0.0}

    def _should_yield():
        now = time.monotonic()
        if now - _yield_state["at"] < 1.0:
            return False
        _yield_state["at"] = now
        return bool(store.waiting_writers())

    def _run_batch(root, paths, *, lock, done=0):  # noqa: ANN001, ANN202
        with lock:
            filled = fill_sidecars(store, cfg, root, only_rel_paths=set(paths))
            if filled.filled or filled.errors:
                print(
                    f"[watch] {root.name}: 新文件 {len(paths)} 个 → "
                    f"补 .lrc {filled.filled}（已有 {filled.already}／"
                    f"无内嵌词 {filled.no_source}／失败 {filled.failed}）",
                    flush=True,
                )
            fetched = run_fetch(
                store, cfg, root, providers, only_rel_paths=set(paths),
                progress=lambda m: print(f"[watch] {m}", flush=True),
                should_yield=_should_yield,
            )
            print(
                f"[watch] {root.name}: 新文件抓取 接受 {fetched.accepted}"
                f"／复核 {fetched.needs_review}／未找到 {fetched.not_found}"
                f"／无同步歌词 {fetched.no_synced}",
                flush=True,
            )
            applied = None
            if write_enabled and not root.read_only:
                applied = run_apply(
                    store, cfg, root, only_rel_paths=set(paths),
                    should_yield=_should_yield,
                )
                if applied.applied or applied.apply_failed:
                    print(
                        f"[watch] {root.name}: 新文件写入 {applied.applied}"
                        f"（失败 {applied.apply_failed}）",
                        flush=True,
                    )
                _beat(store, "已写入", f"累计 {done + applied.applied} 首",
                      applied=done + applied.applied, failed=applied.apply_failed)
            if fetched.ai_accepted:
                print(
                    f"[watch] {root.name}: AI 预审自动采纳 {fetched.ai_accepted}"
                    f"（判定已留档，写入可撤销）",
                    flush=True,
                )
            # 入库后半程：封面支线 + 归位（mode=new_files 时自动归位，全程可撤销）
            try:
                finished = finish_new_files(store, cfg, root, list(paths))
                if finished.get("covers") or finished.get("moved"):
                    print(
                        f"[watch] {root.name}: 封面嵌入 {finished.get('covers', 0)}"
                        f"／自动归位 {finished.get('moved', 0)}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001 — 后半程失败不阻塞下一轮
                print(f"[watch] {root.name}: 封面/归位失败 {type(exc).__name__}: {exc}",
                      flush=True)
            return bool(fetched.yielded or (applied is not None and applied.yielded))

    def make_callback(root):  # noqa: ANN001, ANN202
        def on_files(paths):  # noqa: ANN001, ANN202
            rels = set()
            for path in paths:
                try:
                    rels.add(str(path.relative_to(root.resolved_path)))
                except ValueError:
                    continue
            if not rels:
                return
            # 写入与抓取都必须串行：同一根只允许一个写入者。
            # 监听回调可能来自多个线程，所以这里加锁。
            # 跨进程写租约：Web 容器里的批量任务与这里的监控都会全库写入。
            # 两者同时跑会重复请求上游、互相争 SQLite 写锁，而且每次都把整库
            # 跑一遍（实际重复浪费约 10 分钟）。发现别人在写就跳过本轮，
            # 等它结束后下一轮自然接上。
            holder = store.write_lock_holder(_WRITE_LOCK)
            if holder is not None and holder != _WATCH_OWNER:
                print(
                    f"[watch] 检测到批量任务正在写入（{holder}），本轮跳过，稍后继续",
                    flush=True,
                )
                return

            # 先报「开始」再处理。一批可能有几百首、耗时几十分钟，
            # 若只在结束时输出，运维会以为监控卡死了。
            print(
                f"[watch] {root.name}: 开始处理 {len(rels)} 个稳定文件…",
                flush=True,
            )
            _process_new_files(root, rels, lock=lock)

        return on_files

    watchers = [
        LibraryWatcher(
            cfg, root, make_callback(root),
            log=lambda m: print(m, flush=True),
        )
        for root in roots
    ]

    def shutdown(signum, _frame):  # noqa: ANN001, ANN202
        print(f"\n[watch] 收到信号 {signum}，正在停止…", flush=True)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, shutdown)

    # 心跳：监控是个后台守护进程，界面上原本**完全看不到它在做什么**。
    # 使用者无法判断它是正常工作还是早就挂了——这对「好用」是硬伤。
    # 把状态写进 meta，界面就能显示「上次活动」与「最近做了什么」。
    store.set_meta("watch.started_at", str(time.time()))
    _beat(store, "已启动", "正在监听曲库变化")

    print(f"[watch] 启动 {len(watchers)} 个受管根的监听；写入={'开' if write_enabled else '关'}")
    print(
        f"[watch] 稳定判定: {cfg.watch.stable_seconds}s / {cfg.watch.stable_samples} 次采样；"
        f"对账间隔 {cfg.watch.reconcile_interval_seconds}s；"
        f"每日核对 {cfg.watch.full_scan_local_time} ({cfg.watch.timezone})"
    )

    threads = [
        threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
        for w in watchers
    ]
    for thread in threads:
        thread.start()

    # 周期性「状态对账」：整库审计 + 全量同步，**不联网、不抓歌词**。
    #
    # 为什么主循环里必须做这件事：监听只把「稳定下来的、仍存在的文件」交给
    # 处理流程，所以**删除和移走的文件永远进不了那个列表**——库里于是不知道
    # 它们没了。实际用户移动/删除了文件，界面要等到第二天全量核对才变。
    # 204 首的审计约 1 秒，30 秒一次可以接受。
    _STATE_SYNC_SECONDS = 30.0
    _OBSERVATION_PRUNE_SECONDS = 86400.0
    # 异名 .lrc 的自动改名：播放器只找 <音频同名>.lrc，Late 一天就等于
    # 这首歌「没有歌词」。扫描 3568 个文件约 1 秒，5 分钟一次足够。
    _SIDECAR_NAME_REPAIR_SECONDS = 300.0
    # 孤儿歌词清理：半小时一次足够。只在配置显式打开时才动文件，
    # 而且动作是移入回收站（可还原）。
    _ORPHAN_CLEAN_SECONDS = 1800.0
    # 封面文件名统一小写：几乎不用做第二次，但启动后的第一轮要做一次，
    # 之后每小时扫一遍足够（新拷进来的专辑可能带 Cover.JPG）。
    _COVER_NAME_REPAIR_SECONDS = 3600.0
    # 来源烟雾探测：每天一次最小搜索请求，探测非官方来源是否仍可用。
    # 结果进 provider_health，自动化页健康卡展示——「来源被风控了」不该
    # 等到用户发现歌词抓不到才知道。
    _PROBE_SECONDS = 86400.0
    # AI 预审批：抓取后的待审查队列交给模型判定，高置信直接转待写入。
    # 独立于抓取预算——抓取时逐首判定会被几千首的扫描量摊薄，队列消不动。
    _AI_PRE_REVIEW_SECONDS = 600.0
    last_ai_pre_review = 0.0
    last_orphan_clean = 0.0
    last_cover_repair = 0.0
    last_name_repair = 0.0
    last_state_sync = 0.0
    last_policy_check = 0.0
    last_network_check = 0.0
    last_probe = 0.0

    def _run_ai_pre_review() -> None:
        """对待审查队列做一次模型判定，高置信条目转为待写入。"""
        from .audit import audit_root
        from .match import build_context
        from .pipeline import sync_state
        from .pre_review import run_ai_pre_review

        if not (cfg.ai.enabled and cfg.ai.pre_review):
            return
        for root_cfg in cfg.enabled_roots:
            if root_cfg.read_only:
                continue
            audit = audit_root(root_cfg)
            sync_state(store, cfg, root_cfg, audit, full_scan=False)
            contexts = {}
            for track in audit.tracks:
                location_id = store.location_by_rel_path(
                    store.get_root_id(root_cfg.name) or -1, str(track.audio.rel_path)
                )
                if location_id is None:
                    continue
                ctx = build_context(track.tags, track.audio.duration)
                contexts[int(location_id["id"])] = (ctx, "")
            if not contexts:
                continue
            provider_map = {p.name: p for p in providers}
            root_label = root_cfg.name
            result = run_ai_pre_review(
                store, cfg, root_cfg, provider_map, contexts,
                progress=lambda message, label=root_label: print(
                    f"[watch] {label}: {message}", flush=True
                ),
            )
            if result.judged or result.adopted:
                print(
                    f"[watch] {root_cfg.name}: AI 预审 判定 {result.judged} 条，"
                    f"自动采纳 {result.adopted} 条",
                    flush=True,
                )

    def _provider_probe() -> None:
        probe_providers(store, providers)

    # 标签自动刮削：按配置间隔在后台线程跑。
    #
    # 为什么放在 watch 进程：它是常驻的、已经持有写协调的那一个；
    # 再起一个调度容器只会多一份状态。扫描本身只读，落盘才取租约，
    # 因此不会和「新文件处理」长期抢锁。
    tag_state = {"running": False, "next": 0.0}
    # 重复检测：只读扫描 + 写索引，不做任何删除。
    # 首次启动等 5 分钟，让状态对账先稳定；之后每 24 小时一轮。
    dedupe_state = {"running": False, "next": 0.0}
    last_dedupe = float(store.get_meta("dedupe.last_scan_at") or 0.0)
    dedupe_state["next"] = time.time() + (
        300.0 if not last_dedupe else max(60.0, 86400.0 - (time.time() - last_dedupe))
    )
    if cfg.policy.tags.auto_scan:
        last_tag_scan = float(store.get_meta("tags.last_scan_at") or 0.0)
        interval = max(1.0, cfg.policy.tags.scan_interval_hours) * 3600.0
        # 首次启动先等 2 分钟：让状态对账与歌词流程先稳定下来。
        due_in = 120.0 if not last_tag_scan else max(
            60.0, interval - (time.time() - last_tag_scan)
        )
        tag_state["next"] = time.time() + due_in

    def _tag_auto_worker() -> None:
        from .fingerprint import AcoustIdClient, FpcalcFingerprinter
        from .tagscrape import scan_tags
        from .tagsources import MusicBrainzConfig, MusicBrainzProvider

        provider = None
        acoustid = None
        try:
            mb_cfg = cfg.providers.musicbrainz
            provider = MusicBrainzProvider(
                MusicBrainzConfig(
                    base_url=mb_cfg.base_url,
                    request_timeout_seconds=mb_cfg.request_timeout_seconds,
                    minimum_request_interval_seconds=(
                        mb_cfg.minimum_request_interval_seconds
                    ),
                    search_limit=mb_cfg.search_limit,
                    detail_limit=mb_cfg.detail_limit,
                )
            )
            fingerprinter = (
                FpcalcFingerprinter() if cfg.policy.tags.fingerprint else None
            )
            acoustid = (
                AcoustIdClient(cfg.providers.acoustid)
                if cfg.policy.tags.fingerprint
                and cfg.providers.acoustid.enabled
                else None
            )
            for root in roots:
                if stop.is_set():
                    return
                print(f"[watch] 标签扫描 {root.name} 开始", flush=True)
                result = scan_tags(
                    cfg,
                    root,
                    store,
                    provider,
                    auto_write=cfg.policy.tags.auto_write and write_enabled,
                    progress=lambda message: print(
                        f"[watch] {message}", flush=True
                    ),
                    fingerprinter=fingerprinter,
                    acoustid=acoustid,
                )
                if not result.scanned:
                    # 来源不可用/根为空：一小时后再试，不要等一整天。
                    tag_state["next"] = time.time() + 3600.0
                else:
                    store.set_meta("tags.last_scan_at", str(time.time()))
                    store.set_meta(
                        "tags.last_scan_summary",
                        json.dumps(
                            {"roots": [{"root": root.name, **result.to_dict()}]},
                            ensure_ascii=False,
                        ),
                    )
                if result.errors:
                    store.log_watch_event(
                        "标签扫描",
                        f"{root.name}: {len(result.errors)} 个错误",
                        {"errors": len(result.errors)},
                    )
                print(
                    f"[watch] 标签扫描 {root.name} 完成：可自动 "
                    f"{result.auto}，待审查 {result.review}，未匹配 "
                    f"{result.unresolved}，写入 {result.written}",
                    flush=True,
                )
            _beat(store, "标签扫描完成", "自动补全已处理")
        except Exception as exc:  # noqa: BLE001 — 自动任务失败不能停监控
            tag_state["next"] = time.time() + 3600.0
            print(
                f"[watch] 标签扫描失败 {type(exc).__name__}: {exc}",
                flush=True,
            )
            store.log_watch_event("标签扫描失败", str(exc)[:300])
        finally:
            if provider is not None:
                provider.close()
            if acoustid is not None:
                acoustid.close()
            tag_state["running"] = False

    def _dedupe_auto_worker() -> None:
        from .dedupe import scan_duplicates

        try:
            print("[watch] 重复音频检测开始（只检查，不删除）", flush=True)
            result = scan_duplicates(store, cfg, roots)
            print(
                f"[watch] 重复音频检测完成：{result['duplicates']} 个可清理副本"
                f"（{result['groups']} 组，扫描 {result['scanned']} 个文件）",
                flush=True,
            )
            store.log_watch_event(
                "重复检测",
                f"{result['duplicates']} 个可清理副本 / {result['groups']} 组",
                {"duplicates": result["duplicates"], "groups": result["groups"]},
            )
        except Exception as exc:  # noqa: BLE001 — 自动任务失败不能停监控
            print(f"[watch] 重复音频检测失败 {type(exc).__name__}: {exc}", flush=True)
            store.log_watch_event("重复检测失败", str(exc)[:300])
        finally:
            dedupe_state["running"] = False

    try:
        while not stop.wait(1.0):
            now = time.monotonic()
            if now - last_policy_check >= 30.0:
                last_policy_check = now
                revision = store.get_meta("settings.revision") or ""
                if revision != tag_policy_revision:
                    tag_policy_revision = revision
                    _apply_saved_tag_policy()
                    _apply_saved_pipeline_settings()
                    # AI 设置也要热加载：否则在设置页打开「智能预审」后，
                    # 监控进程要重启才生效——而它恰恰是为了减少人工干预而开的
                    _apply_saved_ai_settings()
                    # 间隔变化后按「上次扫描时间」重新计算下一次；
                    # 缩短间隔时不必等完旧的 24 小时。
                    last_scan = float(store.get_meta("tags.last_scan_at") or 0.0)
                    interval = max(1.0, cfg.policy.tags.scan_interval_hours) * 3600.0
                    tag_state["next"] = (
                        time.time() + max(60.0, interval - (time.time() - last_scan))
                        if last_scan
                        else time.time() + 60.0
                    )
                    print(
                        "[watch] 已应用设置页的标签策略："
                        f"{'自动扫描' if cfg.policy.tags.auto_scan else '不自动扫描'}，"
                        f"间隔 {cfg.policy.tags.scan_interval_hours:g} 小时",
                        flush=True,
                    )
            if now - last_network_check >= 30.0:
                last_network_check = now
                try:
                    _apply_saved_network()
                except Exception as exc:  # noqa: BLE001 — 代理失败不能停掉监控
                    print(
                        f"[watch] 代理设置应用失败 {type(exc).__name__}: {exc}",
                        flush=True,
                    )
            if (
                cfg.policy.tags.auto_scan
                and not tag_state["running"]
                and time.time() >= tag_state["next"]
                and store.write_lock_holder(_WRITE_LOCK) in (None, _WATCH_OWNER)
            ):
                # 别的进程正在写（人工或批量）时先不抢；下一分钟再看。
                tag_state["running"] = True
                tag_state["next"] = time.time() + max(
                    1.0, cfg.policy.tags.scan_interval_hours
                ) * 3600.0
                import threading as _threading

                _threading.Thread(
                    target=_tag_auto_worker, daemon=True, name="lyra-tags"
                ).start()
            if (
                not dedupe_state["running"]
                and not tag_state["running"]
                and time.time() >= dedupe_state["next"]
            ):
                dedupe_state["running"] = True
                dedupe_state["next"] = time.time() + 86400.0
                import threading as _threading

                _threading.Thread(
                    target=_dedupe_auto_worker, daemon=True, name="lyra-dedupe"
                ).start()
            if now - last_state_sync < _STATE_SYNC_SECONDS:
                continue
            last_state_sync = now
            if store.write_lock_holder(_WRITE_LOCK) not in (None, _WATCH_OWNER):
                continue
            for root in roots:
                try:
                    from .pipeline import fill_sidecars, rescan

                    with lock:
                        result = rescan(store, cfg, root)
                    if result.get("missing"):
                        print(
                            f"[watch] {root.name}: 状态对账 新增 {result.get('added', 0)}"
                            f"／缺失 {result.get('missing', 0)}",
                            flush=True,
                        )
                    added = list(result.get("added_paths") or [])
                    if added:
                        _process_new_files(root, added, lock=lock)

                    # 异名 .lrc → 改成音频同名。**播放器只认这个名字**，
                    # 不改名等于这首歌在车机上没有歌词。
                    if now - last_name_repair >= _SIDECAR_NAME_REPAIR_SECONDS:
                        with _watch_lease(store, root.name) as got:
                            if got:
                                from .pipeline import repair_sidecar_names

                                with lock:      # 与批量线程串行，别同时写库
                                    fixed = repair_sidecar_names(store, cfg, root)
                                last_name_repair = now
                                if fixed["renamed"]:
                                    print(
                                        f"[watch] {root.name}: 修正 {fixed['renamed']} "
                                        f"个异名 .lrc（播放器只认同名）",
                                        flush=True,
                                    )
                                if fixed["errors"]:
                                    print(f"[watch] {root.name}: 修正异名 .lrc 有 "
                                          f"{len(fixed['errors'])} 个失败",
                                          flush=True)

                    # 孤儿 .lrc：内容重复的、曲库里没有对应音频的，
                    # 移入回收站（配置里显式打开才做）。
                    if (
                        cfg.write.auto_clean_orphan_sidecars
                        and write_enabled and not root.read_only
                        and now - last_orphan_clean >= _ORPHAN_CLEAN_SECONDS
                    ):
                        with _watch_lease(store, root.name) as got:
                            if got:
                                from .pipeline import clean_orphan_sidecars

                                with lock:
                                    cleaned = clean_orphan_sidecars(store, cfg, root)
                                last_orphan_clean = now
                                if cleaned["cleaned"]:
                                    print(
                                        f"[watch] {root.name}: 清理多余歌词 "
                                        f"{cleaned['cleaned']} 个（移入回收站）",
                                        flush=True,
                                    )
                    # 封面文件名统一小写。播放器（Emby / Jellyfin / Kodi /
                    # Navidrome）优先按 cover.jpg 这类小写名查找；而 Linux 上
                    # Cover.jpg 与 cover.jpg 能同时存在，两张图并存时不同播放器
                    # 会挑到不同那张。只改大小写，冲突留给人决定。
                    if (
                        write_enabled
                        and not root.read_only
                        and now - last_cover_repair >= _COVER_NAME_REPAIR_SECONDS
                    ):
                        with _watch_lease(store, root.name) as got:
                            if got:
                                from .pipeline import normalize_cover_names

                                with lock:
                                    covers = normalize_cover_names(store, cfg, root)
                                last_cover_repair = now
                                if covers["renamed"]:
                                    print(
                                        f"[watch] {root.name}: 规范 {covers['renamed']} "
                                        f"个封面文件名（改为小写）",
                                        flush=True,
                                    )
                                if covers["removed"]:
                                    print(
                                        f"[watch] {root.name}: 有 {covers['removed']} 个"
                                        f"封面与同名小写文件重复，已移入回收站",
                                        flush=True,
                                    )

                except Exception as exc:  # noqa: BLE001 — 对账失败不能停掉监控
                    print(f"[watch] {root.name}: 状态对账失败 {type(exc).__name__}: {exc}",
                          flush=True)
            # AI 预审批：每 30 分钟消化一次待审查队列
            if now - last_ai_pre_review >= _AI_PRE_REVIEW_SECONDS:
                last_ai_pre_review = now
                try:
                    _run_ai_pre_review()
                except Exception as exc:  # noqa: BLE001
                    print(f"[watch] AI 预审失败 {type(exc).__name__}: {exc}", flush=True)
            # 来源烟雾探测：每天一次，与对账解耦（探测失败不影响任何状态）
            if now - last_probe >= _PROBE_SECONDS:
                last_probe = now
                try:
                    _provider_probe()
                except Exception as exc:  # noqa: BLE001
                    print(f"[watch] 来源探测异常 {type(exc).__name__}: {exc}", flush=True)
            # 每轮对账都报一次存活：否则没有写入活动时，界面会把
            # 「进程空闲」显示成「心跳异常」（实际 30 分钟后就出现）。
            _touch(store)

            # 观察表只该在状态变化时增长（见 db.record_lyric_observation 的去重），
            # 但**旧版本留下的重复行还在**：实际 6 小时积了 15 万行，而位置只有
            # 204 个。每天清一次，每首保留最近 3 条——历史够用，库不再膨胀。
            try:
                pruned_at = float(store.get_meta("observations.pruned_at") or 0)
            except (TypeError, ValueError):
                pruned_at = 0.0
            if time.time() - pruned_at > _OBSERVATION_PRUNE_SECONDS:
                try:
                    removed = store.prune_observations(keep_per_location=3)
                    store.set_meta("observations.pruned_at", str(time.time()))
                    if removed:
                        print(f"[watch] 清理旧观察记录 {removed} 行", flush=True)
                except Exception as exc:  # noqa: BLE001 — 清理失败不影响监控
                    print(f"[watch] 清理旧观察记录失败 {type(exc).__name__}: {exc}",
                          flush=True)
    finally:
        for watcher in watchers:
            watcher.stop()
        for thread in threads:
            thread.join(timeout=5)
        for watcher in watchers:
            s = watcher.stats
            print(
                f"[watch] {watcher.root.name} 统计: 事件 {s.events_seen}／"
                f"稳定批次 {s.stable_batches}／处理 {s.files_processed}／"
                f"对账 {s.reconciles}／完整核对 {s.full_scans}"
                + (f"／已降级: {s.degrade_reason}" if s.degraded else "")
            )
        store.close()
    return 0


# ---------------------------------------------------------------------------
# serve / admin-password / token
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    from .api import serve

    if args.bind:
        cfg.web.bind = args.bind
    if args.port:
        cfg.web.port = args.port

    print(f"Lyra Web 启动于 http://{cfg.web.bind}:{cfg.web.port}")
    print(f"  数据目录: {cfg.storage.resolved_data_dir}")
    print(f"  写入开关: {'开' if cfg.policy.write_enabled else '关（只读）'}")
    print(f"  管理员账户: {cfg.web.admin_username}"
          + ("（密码来自配置）" if cfg.web.admin_password else "（未配置密码，将生成随机密码）"))
    if not cfg.web.admin_password:
        print(f"  初始密码文件: {cfg.storage.resolved_data_dir / 'initial-password.txt'}")
    print("  提示：只监听本机，请通过反向代理对外提供 HTTPS。")
    serve(cfg)
    return 0


def cmd_admin_password(args: argparse.Namespace, cfg: Config) -> int:
    from .auth import ensure_admin, generate_password, hash_password

    data_dir = cfg.storage.resolved_data_dir
    if args.show_file:
        path = data_dir / "initial-password.txt"
        if not path.is_file():
            print(f"不存在 {path}（说明凭据来自配置，或已被删除）", file=sys.stderr)
            return 2
        print(path.read_text(encoding="utf-8"))
        return 0

    username = args.username or cfg.web.admin_username
    effective = args.password or cfg.web.admin_password or None

    store = open_store(data_dir)
    try:
        if store.user_count() == 0:
            boot = ensure_admin(
                store, data_dir, username=username, password=effective
            )
            print(f"已创建管理员 {username}")
            if effective:
                print("密码采用配置文件 / 命令行指定的值")
            elif boot.password_file:
                print(f"未配置密码，已生成随机密码并写入: {boot.password_file}")
            return 0

        plain = effective or generate_password()
        store.upsert_user(username=username, password_hash=hash_password(plain))
        if effective:
            print(f"已把 {username} 的密码设置为配置/命令行指定的值")
        else:
            print(f"已重置 {username} 的密码为：")
            print(f"  {plain}")
        return 0
    finally:
        store.close()


def cmd_token_create(args: argparse.Namespace, cfg: Config) -> int:
    from .auth import create_token

    store = open_store(cfg.storage.resolved_data_dir)
    try:
        token = create_token(store, label=args.label, ttl_days=args.ttl_days)
        print(token)
        print(
            "\n以上 Token 只显示这一次，服务端只保存其哈希，无法找回。\n"
            "用法： curl -H 'Authorization: Bearer <token>' "
            f"http://127.0.0.1:{cfg.web.port}/api/v1/summary",
            file=sys.stderr,
        )
        return 0
    finally:
        store.close()


def cmd_token_list(args: argparse.Namespace, cfg: Config) -> int:
    from .auth import list_tokens

    store = open_store(cfg.storage.resolved_data_dir)
    try:
        rows = list_tokens(store)
        if not rows:
            print("（无）")
            return 0
        for r in rows:
            exp = "永不过期" if not r["expires_at"] else time.strftime(
                "%Y-%m-%d", time.localtime(float(r["expires_at"]))
            )
            last = (
                "从未使用"
                if not r["last_used_at"]
                else time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r["last_used_at"])))
            )
            print(f"  [{r['id']:>3}] {str(r['label'] or ''):24} 过期 {exp:12} 最近使用 {last}")
        return 0
    finally:
        store.close()


def cmd_token_revoke(args: argparse.Namespace, cfg: Config) -> int:
    from .auth import revoke_token

    store = open_store(cfg.storage.resolved_data_dir)
    try:
        revoke_token(store, args.token_id)
        print(f"已吊销 Token {args.token_id}")
        return 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# fix-ext / undo-rename
# ---------------------------------------------------------------------------


def _select_roots(cfg: Config, name: str | None) -> list:
    roots = cfg.enabled_roots
    if name:
        roots = [r for r in roots if r.name == name]
    return roots


def cmd_fix_ext(args: argparse.Namespace, cfg: Config) -> int:
    from .rename import apply_rename, scan_extension_mismatches

    roots = _select_roots(cfg, args.root)
    if not roots:
        print("错误: 没有匹配的受管根", file=sys.stderr)
        return 2

    store = open_store(cfg.storage.resolved_data_dir)
    payload: list[dict[str, Any]] = []
    exit_code = 0

    try:
        for root in roots:
            result = scan_extension_mismatches(root)
            root_id = store.upsert_root(
                name=root.name, role=root.role.value, path=str(root.resolved_path)
            )

            if not args.json:
                print("=" * 72)
                print(f"扩展名修正  root={root.name}  {root.resolved_path}")
                print("=" * 72)
                print(f"需要改名: {len(result.plans)}   冲突被跳过: {len(result.conflicts)}")
                print()

            entries: list[dict[str, Any]] = []
            for plan in result.plans:
                outcome = apply_rename(
                    store, root, plan, root_id=root_id, dry_run=not args.apply
                )
                entries.append(
                    {
                        "old": plan.rel_source,
                        "new": plan.rel_target,
                        "container": plan.media_format.value,
                        "reason": plan.reason,
                        "applied": bool(outcome.ok and args.apply),
                        "rename_id": outcome.rename_id,
                        "error": outcome.error,
                    }
                )
                if not outcome.ok:
                    exit_code = 1
                if not args.json:
                    mark = "✅ 已改名" if (outcome.ok and args.apply) else (
                        "🔍 待改名" if outcome.ok else "❌ 失败"
                    )
                    print(f"  {mark}  {plan.old_name}")
                    print(f"            → {plan.new_name}")
                    print(f"            {plan.reason}")
                    if outcome.error:
                        print(f"            错误: {outcome.error}")

            for plan, why in result.conflicts:
                entries.append({"old": plan.rel_source, "conflict": why})
                if not args.json:
                    print(f"  ⚠️ 跳过 {plan.old_name}: {why}")

            if result.playlist_refs and not args.json:
                print()
                print(f"  ⚠️ 播放列表引用了将被改名的文件 {len(result.playlist_refs)} 处：")
                for playlist, line in result.playlist_refs:
                    print(f"      [{playlist}] {line}")
                print("      改名前请确认这些列表是否需要同步更新。")

            payload.append(
                {
                    "root": root.name,
                    "plans": entries,
                    "playlist_refs": [
                        {"playlist": p, "line": line} for p, line in result.playlist_refs
                    ],
                }
            )

            if not args.json and not args.apply and result.plans:
                print()
                print("以上为预览。确认无误后加 --apply 执行；可用 undo-rename 撤销。")
    finally:
        store.close()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def cmd_fill_lrc(args: argparse.Namespace, cfg: Config) -> int:
    """把内嵌歌词补成同名 .lrc。默认预演。"""
    from .pipeline import fill_sidecars

    roots = _select_roots(cfg, args.root)
    if not roots:
        print("错误: 没有匹配的受管根", file=sys.stderr)
        return 2

    store = open_store(cfg.storage.resolved_data_dir)
    payload: list[dict[str, Any]] = []
    exit_code = 0
    try:
        for root in roots:
            summary = fill_sidecars(
                store, cfg, root,
                limit=args.limit,
                dry_run=not args.apply,
                progress=(lambda _m: None) if args.json else (
                    lambda m: print(f"  {m}")
                ),
            )
            payload.append({"root": root.name, **summary.to_dict()})
            if not args.json:
                verb = "已补" if args.apply else "可补"
                print(
                    f"[{root.name}] {verb} .lrc {summary.filled} 首"
                    f"（已有 {summary.already}／无内嵌歌词 {summary.no_source}／"
                    f"有内容无时间轴 {summary.untimed}／失败 {summary.failed}）"
                )
            for err in summary.errors:
                print(f"  ⚠️ {err}", file=sys.stderr)
            if summary.failed:
                exit_code = 1
    finally:
        store.close()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif not args.apply:
        print("以上为预演。加 --apply 才会真正写文件。")
    return exit_code


def cmd_undo_rename(args: argparse.Namespace, cfg: Config) -> int:
    from .rename import undo_rename

    store = open_store(cfg.storage.resolved_data_dir)
    try:
        row = store.conn.execute(
            "SELECT * FROM renames WHERE id=?", (args.rename_id,)
        ).fetchone()
        if row is None:
            print(f"错误: 找不到改名记录 {args.rename_id}", file=sys.stderr)
            return 2
        root = next(
            (r for r in cfg.roots
             if store.get_root_id(r.name) == row["root_id"]),
            None,
        )
        if root is None:
            print(
                f"错误: 改名记录 {args.rename_id} 属于一个当前配置里不存在的受管根",
                file=sys.stderr,
            )
            return 2
        ok, message = undo_rename(store, root, args.rename_id)
        print(("✅ " if ok else "❌ ") + message)
        return 0 if ok else 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # config init 不需要已有配置
    if args.command == "config" and getattr(args, "config_command", None) == "init":
        return int(args.func(args, None))

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        load_network_settings(cfg)
        return int(args.func(args, cfg))
    finally:
        # 同进程内连续调用 main() 时（例如测试），别让上一轮的代理设置
        # 留在进程状态里。
        set_active_network("", {}, [])


if __name__ == "__main__":
    raise SystemExit(main())
