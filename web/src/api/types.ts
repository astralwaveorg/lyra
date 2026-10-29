/**
 * 接口返回结构。
 *
 * 只声明界面真正读取的字段——把后端每个键都写一遍既没人维护，
 * 也会在字段改名时给出「类型对了、值是 undefined」的假安全感。
 */

export type Root = {
  name: string
  role: string
  path: string
  health: string | null
}

export type Summary = {
  version: string
  schema_version: number
  integrity: string | null
  write_enabled: boolean
  lyrics_by_provider: Record<string, Record<string, number>>
  coverage_history: { date: string; written: number; reviewed: number; written_today: number }[]
  roots: Root[]
  counts: {
    locations: number
    observations: number
    staged_pending: number
    needs_review: number
    review_tracks: number
    applied: number
    unfinished_transactions: number
    active_renames: number
  }
  staged_by_state: Record<string, number>
  resolutions_by_outcome: Record<string, number>
  issues: Record<string, number>
  busy: boolean
}

export type TagCoverage = { name: string; count: number; pct: number }
export type BarItem = { name: string; count: number; is_other?: boolean }

export type RootStats = {
  root_name: string
  root_path: string
  elapsed_seconds: number
  audio_total: number
  by_format: Record<string, number>
  unsupported_total: number
  parse_failures: number
  lrc_total: number
  lrc_empty: number
  lrc_synced: number
  lrc_plain: number
  lrc_suspicious: number
  lrc_unparseable: number
  lrc_bom: number
  lrc_orphan: number
  lrc_duplicate_groups: number
  embedded_synced: number
  embedded_plain: number
  embedded_absent: number
  embedded_suspicious: number
  without_embedded_cover: number
  cover_total: number
  cover_pct: number
  tracks_with_lyrics: number
  tracks_without_lyrics: number
  coverage_pct: number
  sidecar_timed: number
  sidecar_untimed: number
  sidecar_missing_embedded: number
  sidecar_missing_none: number
  sidecar_coverage_pct: number
  embedded_coverage_pct: number
  issues: Record<string, number>
  issues_skipped_unfixable: number
  total_seconds: number
  album_total: number
  album_other: number
  artist_total: number
  genre_total: number
  coverage_both: number
  coverage_embedded_only: number
  coverage_sidecar_only: number
  coverage_none: number
  genres: BarItem[]
  albums: BarItem[]
  artists: BarItem[]
  labels: BarItem[]
  decades: BarItem[]
  languages: BarItem[]
  duration_histogram: BarItem[]
  tag_coverage: TagCoverage[]
}

export type StatsResponse = {
  roots: RootStats[]
  write_enabled: boolean
  providers: { name: string }[]
}

export type Track = {
  id: number
  rel_path: string
  root: string
  size: number
  embedded_state: string | null
  sidecar_state: string | null
  sidecar_size: number | null
  issues: string[]
}

export type TrackPage = { total: number; page: number; size: number; items: Track[] }

export type StagedCandidate = {
  provider_rank?: number
  id: number
  provider: string
  provider_id: string
  score: number | null
  duration_delta: number | null
  reason: string | null
  has_evidence: boolean
  /** AI 预审结论（未开启或未判定时为 null）。 */
  ai?: {
    best: string | null
    confidence: number
    relation: string
    reason: string
  } | null
  failed_checks: { key: string; label: string; detail: string }[]
  safe: boolean
}

export type StagedGroup = {
  rel_path: string
  root: string
  best_id: number
  best_score: number | null
  candidates: StagedCandidate[]
  safe_count: number
}

export type ReviewPage = {
  total: number
  page: number
  size: number
  groups: StagedGroup[]
}

export type Gate = { key: string; label: string; passed: boolean; detail: string }

/** 「推荐采纳」列表项：硬闸门全过、仅分数未达自动采用线的候选。 */
export type SafeItem = {
  id: number
  rel_path: string
  root: string
  provider: string
  provider_id: string
  score: number | null
  reason: string | null
}

/** 排除规则。歌词审查的「忽略」就是 path 级 skip_fetch 规则。 */
export type Rule = {
  id: number
  scope: string
  target: string
  action: string
  reason: string | null
  created_at: number
  expires_at: number | null
}

export type StagedDetail = {
  id: number
  rel_path: string
  content: string
  state: string
  provider: string
  provider_id: string
  score: number | null
  duration_delta: number | null
  providers: { provider: string; found?: number; error?: string | null }[]
  checks: Gate[]
  recommendation: { text?: string; action?: string } | null
  provider_order: string[]
}

export type CompareOption = {
  provider: string
  provider_id: string
  title: string
  artists: string[]
  album: string
  duration: number | null
  duration_delta: number | null
  score: number | null
  title_score: number | null
  artist_score: number | null
  version_ok: boolean
  rejected: boolean
  reject_reason: string | null
  is_best: boolean
  is_chosen: boolean
  gates: Gate[]
  lines: { ms: number | null; stamp: string; text: string }[]
  content: string
  synced: boolean
  error: string | null
}

/** `/staged/{id}/alternatives` 的选项。**注意**：它没有 `gates`，
 *  也没有 title_score / is_best —— 与 `/tracks/candidates` 的形状不同。
 *  之前按候选列表的类型去读 `option.gates`，一打开抽屉就抛 TypeError。 */
export type AlternativeOption = {
  provider: string
  provider_id: string
  title: string
  artists: string[]
  album: string
  duration: number | null
  duration_delta: number | null
  score: number | null
  content: string
  line_count: number
  timestamp_count: number
  synced: boolean
  word_level: boolean
  has_translation: boolean
  rejected: boolean
  reject_reason: string | null
  is_current: boolean
  error: string | null
  lines: { ms: number | null; stamp: string; text: string }[]
}

export type Alternatives = {
  staged_id: number
  rel_path: string
  track: { title: string; artists: string[]; album: string; duration: number | null }
  current: { provider: string; provider_id: string }
  options: AlternativeOption[]
  sources: { provider: string; found?: number; error?: string | null }[]
}

export type TrackCandidate = {
  provider: string
  provider_id: string
  title: string
  artists: string[]
  album: string
  duration: number | null
  duration_delta: number | null
  score: number | null
  title_score: number | null
  artist_score: number | null
  version_ok: boolean
  rejected: boolean
  reject_reason: string | null
  is_best: boolean
  is_chosen: boolean
  /** 这份歌词有没有时间轴：true/false 已知，null 表示来源没给结论。 */
  has_timeline?: boolean | null
  gates: Gate[]
}

export type TrackCandidates = {
  rel_path: string
  root: string
  size: number | null
  track: {
    title: string
    artists: string[]
    album: string
    duration: number | null
    version_tokens: string[]
  }
  provider_order: string[]
  sources: { provider: string; found?: number; error?: string | null }[]
  verdict: {
    decision: string
    reason: string | null
    chosen_provider: string | null
    chosen_provider_id: string | null
  }
  options: TrackCandidate[]
  has_existing_lyrics: boolean
}

export type LyricsPreview = {
  provider: string
  provider_id: string
  synced: boolean
  word_level: boolean
  has_translation: boolean
  line_count: number
  timestamp_count: number
  /** 音频时长（秒）；取不到时为 null。 */
  duration_seconds?: number | null
  /** 末行时间戳（毫秒）；纯文本歌词没有时间轴时为 null。 */
  last_timestamp_ms?: number | null
  /** 时间轴超出音频的秒数；0 表示没超出，null 表示算不出来。 */
  overrun_seconds?: number | null
  text: string
  lines: { ms: number | null; stamp: string; text: string }[]
}

export type FileItem = {
  root: string
  rel_path: string
  name: string
  kind: 'audio' | 'lyrics' | 'image' | 'text'
  ext: string
  size: number
  mtime: number
}

export type FilePage = {
  total: number
  page: number
  size: number
  by_kind: Record<string, number>
  items: FileItem[]
}

export type FileMeta = {
  rel_path: string
  all_tags: Record<string, string[]>
  all_tags_error: string | null
  media_format: string
  extension: string
  extension_matches_container: boolean
  duration: number | null
  size: number
  parse_error: string | null
  embedded_state: string
  embedded_fields: string[]
  has_cover: boolean
  problems: string[]
  embedded_text?: string
  embedded_text_error?: string | null
}

export type LyricContent = {
  rel_path: string
  kind: string
  size: number
  truncated: boolean
  has_bom: boolean
  state: string
  line_count: number
  timestamp_count: number
  offset_ms: number
  has_word_level: boolean
  tags: Record<string, string>
  problems: string[]
  lines: { ms: number | null; stamp: string; text: string }[]
  text?: string
}

export type TaskRecord = {
  id: string
  kind: string
  state: 'running' | 'done' | 'failed'
  started_at: number
  finished_at: number | null
  progress: string
  result: unknown
  error: string | null
}

export type WatchStatus = {
  running: boolean
  started_at: number | null
  heartbeat_at: number | null
  heartbeat_age_seconds: number | null
  healthy: boolean
  stage: string | null
  detail: string | null
  counters: Record<string, unknown>
  recent: { at?: number; stage?: string; detail?: string }[]
}

export type TrashBatch = {
  batch: string
  items: { rel_path: string; name: string; size: number; kind: string }[]
}

export type TrashList = { total: number; bytes: number; batches: TrashBatch[] }

export type OrphanItem = {
  root: string
  rel_path: string
  size: number
  state: string
  line_count: number
  timestamp_count: number
  preview: string
  action: 'duplicate' | 'choose' | 'unmatched'
  guess_audio: string | null
  paired_sidecar: string | null
  /** 目标音频那份同名 lrc 的状态（没有则 null）。「placeholder」＝只有纯音乐占位。 */
  paired_state: string | null
  /** 目标音频是不是已经有歌词（同名 lrc 或内嵌都算）。 */
  target_has_lyrics: boolean
  same_content: boolean
  candidates: {
    rel_path: string
    how: string
    scope: string
    paired_sidecar: string | null
    /** 那份同名 lrc 里是否真的有歌词（占位文案不算）。 */
    paired_has_lyrics: boolean
    paired_state: string | null
    /** 那首音频整体是否已经有歌词（同名 lrc 或内嵌）。 */
    has_lyrics: boolean
    same_content: boolean
  }[]
}

export type WriteRow = {
  id: number
  path: string
  rel_path: string | null
  created_new: boolean
  at: number
}

export type TxnRow = { txn_id: string; state: string; error: string | null; updated_at: number }
export type RenameRow = { id: number; old: string; new: string; reason: string; at: number }

export type DirNode = {
  path: string
  name: string
  depth: number
  audio: number
  missing_lrc: number
}

export type LibraryDirs = { roots: { root: string; path: string; dirs: DirNode[] }[] }

export type TagSuggest = {
  rel_path: string
  track: { title: string; artists: string[]; album: string; duration: number | null }
  candidates: {
    provider_id: string
    fields: Record<string, unknown>
    exact: boolean
    source_kind: string
    duration_delta: number | null
    summary: Record<string, number>
    rows: { key: string; current: string[]; suggested: string[]; action: string }[]
  }[]
}

export type TokenRow = {
  id: number
  label: string
  created_at: number
  last_used_at: number | null
  expires_at: number | null
}

export type Health = {
  ok: boolean
  version: string
  schema_version: number
  write_enabled: boolean
  busy: boolean
}

/* ---------------------------------------------------------------- 标签刮削 */

export type TagRowAction = 'same' | 'fill' | 'conflict' | 'correct'

export type TagFieldRow = {
  key: string
  current: string[]
  suggested: string[]
  action: TagRowAction
}

export type TagProposalMatch = {
  source: string
  source_label: string
  confidence: number
  release_id: string
  recording_id: string
  album: string
  duration_delta: number | null
  verified?: boolean
  verification?: string
}

export type TagProposal = {
  id: number
  rel_path: string
  root: string
  state: 'auto' | 'review' | 'unmatched' | 'ignored'
  /** 人工忽略的时间；只有 state='ignored' 时有值。 */
  dismissed_at?: number | null
  source: string
  source_label: string
  confidence: number
  album: string
  title: string
  artists: string
  duration_delta: number | null
  track: {
    title?: string
    artists?: string[]
    album?: string
    albumartist?: string
    duration?: number | null
    format?: string
    writable?: boolean
    dir?: string
    name?: string
  }
  rows: TagFieldRow[]
  summary: { conflict?: number; correct?: number; fill?: number; same?: number; total?: number }
  extra: Record<string, string>
  notes: string[]
  candidates: {
    provider_id: string
    title: string
    artists: string[]
    album: string
    duration: number | null
    score: number
  }[]
  writable: boolean
  match: TagProposalMatch
  updated_at: number
}

export type TagProposalPage = {
  total: number
  items: TagProposal[]
  limit: number
  offset: number
}

export type TagScanRoot = {
  root: string
  scanned: number
  matched_mbid: number
  matched_album: number
  matched_search: number
  matched_fingerprint: number
  unresolved: number
  satisfied: number
  auto: number
  review: number
  written: number
  written_fields: number
  fingerprints: number
  errors: string[]
  elapsed_seconds: number
}

export type TagSummary = {
  counts: { auto: number; review: number; unmatched: number; ignored: number }
  total: number
  last_scan_at: number | null
  last_scan: { roots?: TagScanRoot[] } | null
  write_enabled: boolean
  source: { musicbrainz: boolean; acoustid: boolean; fingerprint_tool: boolean }
  policy: {
    auto_scan: boolean
    scan_interval_hours: number
    auto_write: boolean
    fingerprint: boolean
    max_auto_write_per_run: number
    max_duration_delta_seconds: number
  }
}

export type TagPolicy = TagSummary['policy']

export type TagSettings = {
  policy: TagPolicy
  revision: string
}

export type ProxyFeature = 'lrclib' | 'musicbrainz' | 'acoustid' | 'cover'

export type NetworkSettings = {
  proxy: string
  source: 'settings' | 'config' | 'env' | 'none'
  effective_proxy: string
  environment_proxy: string
  schemes: string[]
  features: ProxyFeature[]
  use_proxy: Record<ProxyFeature, boolean>
  proxy_reachable: boolean | null
  proxy_status: 'none' | 'ok' | 'unreachable'
}

export type NetworkTestResult = {
  ok: boolean
  proxy: string
  reachable: boolean | null
  message: string
}

export type AIProtocol = 'openai_chat' | 'openai_responses' | 'anthropic'

export type AISettings = {
  enabled: boolean
  protocol: AIProtocol
  base_url: string
  model: string
  timeout_seconds: number
  max_requests_per_run: number
  auto_clean_duplicates: boolean
  pre_review: boolean
  pre_review_confidence: number
  auto_accept: boolean
  verify_tags: boolean
  use_proxy: boolean
  has_api_key: boolean
  api_key_source: 'env' | 'settings' | 'config' | 'none'
  protocols: AIProtocol[]
  revision: string
}

export type AISettingsInput = Partial<Pick<
  AISettings,
  | 'enabled'
  | 'protocol'
  | 'base_url'
  | 'model'
  | 'timeout_seconds'
  | 'max_requests_per_run'
  | 'auto_clean_duplicates'
  | 'use_proxy'
>> & {
  api_key?: string
  clear_api_key?: boolean
}

export type AITestResult = {
  ok: boolean
  protocol: AIProtocol
  model: string
  endpoint: string
  sample: string
  latency_ms: number
  use_proxy: boolean
}

export type TagLinkCandidate = {
  index: number
  title: string
  artists: string[]
  album: string
  duration: number | null
  duration_delta: number | null
  rows: TagFieldRow[]
  summary: { conflict?: number; correct?: number; fill?: number; same?: number; total?: number }
  extra: Record<string, string>
}

export type TagLinkResult = {
  link: { entity: string; entity_label: string; mbid: string; url: string }
  candidates: TagLinkCandidate[]
  applied: boolean
  proposal?: TagProposal
}

export type TagHistoryRow = {
  id: number
  txn_id: string
  rel_path: string
  root: string
  applied: Record<string, string[]>
  before: Record<string, string[]>
  written_at: number
  undone: boolean
}

/* ------------------------------------------------------------ 曲库整理 */

export type OrganizeMove = {
  old_rel_path: string
  new_rel_path: string
  old_name: string
  new_name: string
  size: number
  format: string
  has_sidecar: boolean
  sidecar_old: string
  sidecar_new: string
  artist: string
  artists: string[]
  album: string
  title: string
  track: number | null
  disc: number | null
  multi_disc: boolean
  status: 'move' | 'chain' | 'conflict'
  reason: string
}

export type OrganizeDir = {
  rel_path: string
  depth: number
  files: number
  kind: 'empty' | 'no_audio' | 'after_move'
}

export type OrganizePlan = {
  plan_id: string
  root: string
  subpaths: string[]
  exclude: string[]
  subpath?: string
  moves: OrganizeMove[]
  executable: OrganizeMove[]
  conflicts: OrganizeMove[]
  skipped: { old_rel_path: string; new_rel_path?: string; reason: string }[]
  dirs: OrganizeDir[]
  summary: {
    total: number
    move: number
    conflict: number
    already_ok: number
    empty_dirs: number
    no_audio_dirs: number
    after_move_dirs?: number
  }
}

export type OrganizeResult = {
  ok: boolean
  batch_id: string
  moved: number
  failed: number
  errors: string[]
  dirs_removed: number
  dirs_trashed: number
  ops: number[]
}

export type OrganizeOp = {
  id: number
  root: string
  batch_id: string
  kind: 'move' | 'delete' | 'rmdir'
  old_rel_path: string
  new_rel_path: string
  trash_rel_path: string
  detail: string
  created_at: number
  undone_at: number | null
}

export type DedupeItem = {
  rel_path: string
  name: string
  size: number
  mtime: number
  format: string
  audio_hash: string
  duration: number | null
  title: string
  artist: string
  album: string
  recording_id: string
  has_lrc: boolean
  tag_count: number
  suggested_delete: boolean
  keep_reason: string
  reference?: boolean
}

export type DedupeGroup = {
  id: number
  root: string
  kind: 'exact' | 'recording' | 'similar'
  keep_rel_path: string
  keep_reason: string
  items: DedupeItem[]
  found_at: number
  dismissed_at?: number | null
}

export type DedupeResponse = {
  groups: DedupeGroup[]
  dismissed?: DedupeGroup[]
  duplicates: number
  last_scan_at: number | null
  last_scan: {
    scanned?: number
    groups?: number
    duplicates?: number
    errors?: string[]
    elapsed_seconds?: number
  }
}

export type CleanupResult = {
  ok: boolean
  batch_id: string
  removed: number
  trashed: number
  errors: string[]
  ops: number[]
}

/** 曲库里的艺人（按标签统计），供「按艺人刮削」筛选。 */
export type LibraryArtist = {
  name: string
  count: number
}


/* ------------------------------------------------------------ 重构新增 */

export type TodoKind = 'lyrics' | 'tags' | 'covers' | 'organize' | 'orphans' | 'failed'

export interface TodoCounts {
  lyrics: number
  tags: number
  covers: number
  organize: number
  orphans: number
  failed: number
}

export interface TodoItem {
  kind: TodoKind
  ref: string
  root: string
  rel_path: string
  /** 各类别附带的字段不同，宽松索引签名。 */
  [key: string]: unknown
}

export interface TodoPage {
  counts: TodoCounts
  total: number
  page: number
  size: number
  items: TodoItem[]
}

export interface RunStep {
  seq: number
  kind: string
  rel_path: string | null
  state: string
  detail: Record<string, unknown>
}

export interface RunDetail {
  task: {
    id: string
    kind: string
    state: string
    progress: string
    error: string | null
    result: unknown
    started_at: number
    finished_at: number | null
  }
  total_steps: number
  steps: RunStep[]
}

export interface ProviderHealthDay {
  day: string
  ok: number
  not_found: number
  blocked: number
  schema_error: number
}

export interface ProviderHealth {
  provider: string
  ok: number
  not_found: number
  blocked: number
  schema_error: number
  last_error: string | null
  days: ProviderHealthDay[]
}

export interface CoverPolicySettings {
  auto_embed_sibling: boolean
  fetch_sources: string[]
  min_min_pixels: number
  auto_write: boolean
}

export interface OrganizePolicySettings {
  mode: string
  template: string
}

export interface LyricsSettings {
  translation: string
  keep_word_level: boolean
  romanization: boolean
  encoding: string
  bom: boolean
  line_ending: string
  format: string
}

export interface AIPolicySettings {
  pre_review: boolean
  pre_review_confidence: number
  auto_accept: boolean
  verify_tags: boolean
  max_requests_per_run: number
  timeout_seconds: number
}
