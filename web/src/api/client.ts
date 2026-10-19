import type {
  Alternatives,
  SafeItem,
  CleanupResult,
  DedupeResponse,
  DirNode,
  FileMeta,
  FilePage,
  Health,
  LibraryDirs,
  LyricContent,
  OrganizeDir,
  OrganizeOp,
  OrganizePlan,
  OrganizeResult,
  LyricsPreview,
  OrphanItem,
  RenameRow,
  ReviewPage,
  RootStats,
  StagedDetail,
  StatsResponse,
  Summary,
  TagHistoryRow,
  TagLinkResult,
  TagProposal,
  TagProposalPage,
  TagPolicy,
  TagScanRoot,
  TagSettings,
  TagSummary,
  TagSuggest,
  TaskRecord,
  TokenRow,
  TrackCandidates,
  TrackPage,
  TrashList,
  TxnRow,
  WatchStatus,
  WriteRow,
} from './types'

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** 会话失效时由 App 层接管的回调，避免 client 反向依赖 store。 */
let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: (() => void) | null) {
  onUnauthorized = fn
}

function query(params: Record<string, unknown> = {}): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, { credentials: 'same-origin', ...init, headers })

  if (response.status === 401) {
    onUnauthorized?.()
    throw new ApiError('需要登录', 401)
  }

  const text = await response.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { detail: text }
    }
  }

  if (!response.ok) {
    // 422 有两种：pydantic 的参数校验（detail 是英文数组），以及业务层
    // 明确拒绝（detail 是中文说明，例如「字段在扫描后已被改动」）。
    // 后者必须原样展示——把它统一换成「请求参数不正确」会让用户完全
    // 不知道发生了什么，也不知道该重新扫描。
    if (response.status === 422) {
      const detail = (data as { detail?: unknown } | null)?.detail
      if (typeof detail === 'string' && detail.trim()) {
        throw new ApiError(detail, 422)
      }
      throw new ApiError('请求参数不正确，请刷新页面后重试', 422)
    }
    const detail =
      (data as { detail?: unknown; message?: unknown } | null)?.detail ??
      (data as { message?: unknown } | null)?.message
    const message =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => (d as { msg?: string }).msg ?? String(d)).join('；')
          : `请求失败（HTTP ${response.status}）`
    throw new ApiError(message, response.status)
  }
  return data as T
}

const json = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) })

/** 路径里的 id 必须是整数：`undefined` 会被序列化成字面量，服务端只能回英文校验错误。 */
function requireId(value: number): number {
  const id = Number(value)
  if (!Number.isInteger(id) || id <= 0) {
    throw new ApiError('这条记录已失效，请刷新页面后重试', 400)
  }
  return id
}
const del = (): RequestInit => ({ method: 'DELETE' })

/* ------------------------------------------------------------------ 鉴权 */

export const api = {
  health: () => request<Health>('/api/v1/health'),
  me: () => request<{ ok: boolean; auth_required: boolean; username: string }>('/api/v1/auth/me'),
  login: (username: string, password: string) =>
    request<{ ok: boolean; username: string }>('/api/v1/auth/login', json({ username, password })),
  logout: () => request<{ ok: boolean }>('/api/v1/auth/logout', json({})),
  changePassword: (payload: { username: string; current_password: string; new_password: string }) =>
    request<{ ok: boolean }>('/api/v1/auth/password', json(payload)),

  tokens: () => request<{ items: TokenRow[] }>('/api/v1/tokens'),
  mintToken: (label: string, ttl_days: number | null) =>
    request<{ token: string; label: string }>('/api/v1/auth/token', json({ label, ttl_days })),
  revokeToken: (id: number) => request<{ ok: boolean }>(`/api/v1/tokens/${id}`, del()),

  /* ---------------------------------------------------------------- 概览 */

  summary: () => request<Summary>('/api/v1/summary'),
  stats: (refresh = false) => request<StatsResponse>(`/api/v1/stats${query({ refresh })}`),
  watch: () => request<WatchStatus>('/api/v1/watch'),
  config: () => request<Record<string, unknown>>('/api/v1/config'),

  /* ---------------------------------------------------------------- 曲目 */

  tracks: (params: { state?: string; issue?: string; q?: string; page?: number; size?: number }) =>
    request<TrackPage>(`/api/v1/tracks${query(params)}`),
  issueKinds: () => request<{ kinds: { code: string; count: number }[]; total: number }>(
    '/api/v1/issue-kinds',
  ),
  trackCandidates: (params: { root?: string; path: string; per_provider?: number }) =>
    request<TrackCandidates>(`/api/v1/tracks/candidates${query(params)}`),
  resolveLink: (payload: { root?: string; path: string; url: string }) =>
    request<LyricsPreview & { detail: unknown; url: string }>(
      '/api/v1/tracks/resolve-link',
      json(payload),
    ),
  lyricsPreview: (params: {
    root?: string
    path: string
    provider: string
    provider_id: string
    by_id?: boolean
  }) => request<LyricsPreview>(`/api/v1/tracks/lyrics-preview${query(params)}`),
  applyLyrics: (payload: {
    root?: string
    path: string
    provider: string
    provider_id: string
    apply?: boolean
    replace_existing?: boolean
    by_id?: boolean
  }) =>
    request<{
      ok: boolean
      staged_id: number
      bytes: number
      applied: {
        ok: boolean
        written: number
        failed: number
        errors: string[]
        error?: string
      } | null
    }>('/api/v1/tracks/apply-lyrics', json(payload)),

  /* ---------------------------------------------------------------- 复核 */

  staged: (params: {
    state?: string
    page?: number
    size?: number
    by_track?: boolean
    exclude_safe?: boolean
  }) =>
    request<ReviewPage>(`/api/v1/staged${query(params)}`),
  safeBatch: () =>
    request<{ total: number; items: SafeItem[] }>('/api/v1/staged/safe-batch'),
  acceptSafeBatch: (ids?: number[]) =>
    request<{ accepted: number; total: number }>(
      '/api/v1/staged/safe-batch/accept',
      json(ids ? { ids } : {}),
    ),
  stagedDetail: (id: number) => request<StagedDetail>(`/api/v1/staged/${id}`),
  stagedAlternatives: (id: number) => request<Alternatives>(`/api/v1/staged/${id}/alternatives`),
  chooseAlternative: (id: number, provider: string, provider_id: string) =>
    request<{ ok: boolean; staged_id: number }>(
      `/api/v1/staged/${id}/choose`,
      json({ provider, provider_id }),
    ),
  acceptStaged: (id: number) =>
    request<{ ok: boolean; id: number; state: string }>(
      `/api/v1/staged/${requireId(id)}/accept`,
      json({}),
    ),
  rejectStaged: (id: number) =>
    request<{ ok: boolean; id: number; state: string }>(
      `/api/v1/staged/${requireId(id)}/reject`,
      json({}),
    ),

  /* ---------------------------------------------------------------- 文件 */

  files: (params: { root?: string; kind?: string; q?: string; page?: number; size?: number }) =>
    request<FilePage>(`/api/v1/files${query(params)}`),
  fileMeta: (root: string, path: string) =>
    request<FileMeta>(`/api/v1/files/meta${query({ root, path })}`),
  fileContent: (root: string, path: string) =>
    request<LyricContent>(`/api/v1/files/content${query({ root, path })}`),
  fileSidecar: (root: string, path: string) =>
    request<{ path: string | null; exists: boolean; state: string; name_differs: boolean; wanted: string }>(
      `/api/v1/files/sidecar${query({ root, path })}`,
    ),
  deleteFile: (root: string, path: string) =>
    request<{ ok: boolean; moved_to: string; sidecar_moved_to: string | null }>(
      '/api/v1/files/delete',
      json({ root, path }),
    ),
  batchDelete: (root: string, paths: string[]) =>
    request<{ ok: boolean; moved: string[]; skipped: string[]; errors: string[]; batch: string }>(
      '/api/v1/files/batch-delete',
      json({ root, paths }),
    ),
  renameFile: (payload: { root: string; path: string; new_name: string }) =>
    request<{
      ok: boolean
      old_rel_path: string
      new_rel_path: string
      sidecar_renamed: boolean
      warnings: string[]
    }>('/api/v1/files/rename', json(payload)),
  replaceFile: async (root: string, path: string, file: File, moveOld = true) => {
    const url = `/api/v1/files/replace${query({ root, path, move_old: moveOld })}`
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    })
    return parseResponse<{
      ok: boolean
      final_name: string
      final_rel_path: string
      renamed: boolean
      detected_format: string
      extension_used: string
    }>(response)
  },

  /* ---------------------------------------------------------------- 封面 */

  coverSibling: (root: string, path: string) =>
    request<{ found: boolean; name?: string; width?: number; height?: number; error?: string }>(
      `/api/v1/files/cover/sibling${query({ root, path })}`,
    ),
  coverFromDir: (root: string, path: string) =>
    request<{ ok: boolean; source_file: string; cover: unknown }>(
      '/api/v1/files/cover/from-dir',
      json({ root, path }),
    ),
  coverFromUrl: (root: string, path: string, url: string) =>
    request<{ ok: boolean; source_url: string; cover: unknown }>(
      '/api/v1/files/cover/from-url',
      json({ root, path, url }),
    ),
  coverRemove: (root: string, path: string) =>
    request<{ ok: boolean; removed: boolean }>(
      `/api/v1/files/cover${query({ root, path, remove: true })}`,
      { method: 'POST', body: '' },
    ),
  coverUpload: async (root: string, path: string, file: File) => {
    const response = await fetch(`/api/v1/files/cover${query({ root, path })}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    })
    return parseResponse<{ ok: boolean; cover: unknown }>(response)
  },
  coverUrl: (root: string, path: string, bust = 0) =>
    `/api/v1/files/cover${query({ root, path, v: bust || undefined })}`,
  mediaUrl: (root: string, path: string) => `/api/v1/files/media${query({ root, path })}`,

  /* ---------------------------------------------------------------- 标签 */

  writeTags: (payload: { root: string; path: string; tags: Record<string, string[]>; remove: string[] }) =>
    request<{ ok: boolean; applied: string[]; removed: string[]; audio_hash: string; all_tags: Record<string, string[]> }>(
      '/api/v1/files/tags',
      json(payload),
    ),
  tagSuggest: (root: string, path: string, release = '') =>
    request<TagSuggest>(`/api/v1/files/tags/suggest${query({ root, path, release })}`),
  tagLink: (root: string, path: string, url: string) =>
    request<TagSuggest & { link: { entity_label: string } }>(
      `/api/v1/files/tags/link${query({ root, path, url })}`,
    ),

  /* ---------------------------------------------------------------- 标签刮削 */

  tagsSummary: () => request<TagSummary>('/api/v1/tags/summary'),
  tagSettings: () => request<TagSettings>('/api/v1/settings/tags'),
  updateTagSettings: (policy: TagPolicy) =>
    request<TagSettings>('/api/v1/settings/tags', {
      method: 'PUT',
      body: JSON.stringify(policy),
    }),
  tagProposals: (params: { state?: string; q?: string; limit?: number; offset?: number }) =>
    request<TagProposalPage>(`/api/v1/tags/proposals${query(params)}`),
  applyTagProposal: (id: number, fields?: string[]) =>
    request<{ ok: boolean; applied: string[]; values: Record<string, string[]>; remaining: number }>(
      `/api/v1/tags/proposals/${id}/apply`,
      json(fields ? { fields } : {}),
    ),
  skipTagProposal: (id: number) =>
    request<{ ok: boolean }>(`/api/v1/tags/proposals/${id}/skip`, json({})),
  bulkApplyTags: (payload: { ids?: number[]; scope?: 'auto' | 'fills' }) =>
    request<{ ok: boolean; files: number; fields: number; errors: string[] }>(
      '/api/v1/tags/proposals/bulk-apply',
      json(payload),
    ),
  linkTagProposal: (id: number, url: string, index?: number) =>
    request<TagLinkResult>(
      `/api/v1/tags/proposals/${id}/link`,
      json(index === undefined ? { url } : { url, index }),
    ),
  tagHistory: (limit = 100) =>
    request<{ items: TagHistoryRow[] }>(`/api/v1/tags/history${query({ limit })}`),
  undoTagsWrite: (id: number) =>
    request<{ ok: boolean; rel_path: string; restored: string[]; removed: string[] }>(
      `/api/v1/tags/writes/${id}/undo`,
      json({}),
    ),

  /* ---------------------------------------------------------------- 任务 */

  startFetch: (payload: {
    root?: string
    dir?: string
    paths?: string[]
    limit?: number
    force?: boolean
    apply?: boolean
    filters?: {
      lyric_state?: 'none' | 'unsynced'
      missing_sidecar?: boolean
      missing_embedded?: boolean
    }
  }) => request<TaskRecord>('/api/v1/tasks/fetch', json(payload)),
  startApply: (payload: { root?: string; limit?: number }) =>
    request<TaskRecord>('/api/v1/tasks/apply', json(payload)),
  startTagsAudit: (payload: { root?: string; limit?: number }) =>
    request<TaskRecord>('/api/v1/tasks/tags-audit', json(payload)),
  startTagsScan: (payload: { root?: string; auto_write?: boolean; fingerprint?: boolean; limit?: number }) =>
    request<TaskRecord>('/api/v1/tasks/tags-scan', json(payload)),
  tasks: (limit = 12) => request<{ items: TaskRecord[]; total: number }>(`/api/v1/tasks${query({ limit })}`),
  task: (id: string) => request<TaskRecord>(`/api/v1/tasks/${id}`),
  libraryDirs: (root?: string) =>
    request<LibraryDirs>(`/api/v1/library/dirs${query({ root })}`),

  /* ---------------------------------------------------------------- 日志 */

  writes: () => request<{ items: WriteRow[]; total: number; keep: number }>('/api/v1/writes'),
  undoWrite: (id: number) => request<{ ok: boolean; message: string }>(`/api/v1/writes/${id}/undo`, json({})),
  deleteWrite: (id: number) => request<{ ok: boolean }>(`/api/v1/writes/${id}`, del()),
  clearWrites: () => request<{ deleted: number }>('/api/v1/writes/clear', json({})),
  transactions: () =>
    request<{ items: TxnRow[]; unfinished: number; total: number }>('/api/v1/transactions'),
  deleteTransaction: (id: string) => request<{ ok: boolean }>(`/api/v1/transactions/${id}`, del()),
  clearTransactions: () => request<{ deleted: number }>('/api/v1/transactions/clear', json({})),
  renames: () => request<{ items: RenameRow[] }>('/api/v1/renames'),
  undoRename: (id: number) => request<{ ok: boolean; message: string }>(`/api/v1/renames/${id}/undo`, json({})),
  batchUndoRenames: (ids: number[]) =>
    request<{ ok: boolean; undone: number; failed: number; messages: string[] }>(
      '/api/v1/renames/batch-undo',
      json({ ids }),
    ),

  /* ------------------------------------------------------------ 曲库整理 */

  organizePreview: (payload: { root: string; subpaths?: string[]; exclude?: string[] }) =>
    request<OrganizePlan>('/api/v1/organize/preview', json(payload)),
  organizeExecute: (payload: { plan_id: string; dirs?: string[] }) =>
    request<OrganizeResult>('/api/v1/organize/execute', json(payload)),
  organizeCleanupPreview: (payload: { root: string; subpaths?: string[]; exclude?: string[] }) =>
    request<{ root: string; subpaths: string[]; exclude: string[]; items: OrganizeDir[]; empty: number; no_audio: number }>(
      '/api/v1/organize/cleanup-preview',
      json(payload),
    ),
  organizeCleanup: (payload: { root: string; subpaths?: string[]; exclude?: string[]; paths: string[] }) =>
    request<CleanupResult>('/api/v1/organize/cleanup', json(payload)),
  organizeOps: (includeUndone = false) =>
    request<{ items: OrganizeOp[]; total: number }>(
      `/api/v1/organize/ops${query({ include_undone: includeUndone })}`,
    ),
  organizeUndo: (ids: number[]) =>
    request<{ ok: boolean; undone: number; failed: number; messages: string[] }>(
      '/api/v1/organize/undo',
      json({ ids }),
    ),
  dedupeGroups: () => request<DedupeResponse>('/api/v1/dedupe/groups'),
  startDedupeScan: (payload: { root?: string } = {}) =>
    request<TaskRecord>('/api/v1/tasks/dedupe-scan', json(payload)),
  dedupeDelete: (groupId: number, paths: string[]) =>
    request<{ ok: boolean; deleted: number; errors: string[]; batch_id: string }>(
      `/api/v1/dedupe/groups/${groupId}/delete`,
      json({ paths }),
    ),
  dedupeDismiss: (groupId: number) =>
    request<{ ok: boolean }>(`/api/v1/dedupe/groups/${groupId}/dismiss`, json({})),
  dedupeDeleteSelected: (items: { group_id: number; path: string }[]) =>
    request<{ ok: boolean; deleted: number; errors: string[]; batch_ids: string[] }>(
      '/api/v1/dedupe/delete',
      json({ items }),
    ),
  dedupeDeleteKind: (kind: 'exact' | 'recording' | 'similar') =>
    request<{ ok: boolean; deleted: number; errors: string[]; batch_ids: string[] }>(
      '/api/v1/dedupe/delete',
      json({ kind }),
    ),

  /* ---------------------------------------------------------------- 回收站 */

  trash: () => request<TrashList>('/api/v1/trash'),
  restoreTrash: (batch: string, path: string, root: string) =>
    request<{ ok: boolean; also_restored: string[] }>(
      '/api/v1/trash/restore',
      json({ batch, path, root }),
    ),
  emptyTrash: (batch?: string) =>
    request<{ removed_files: number; removed_bytes: number }>(
      '/api/v1/trash/empty',
      json({ confirm: true, batch: batch ?? '' }),
    ),

  /* ---------------------------------------------------------------- 孤儿 */

  orphans: (root?: string) =>
    request<{ total: number; items: OrphanItem[]; duplicates: number; same_content: number }>(
      `/api/v1/orphans${query({ root })}`,
    ),
  repairOrphans: (payload: { root?: string; paths?: string[]; dry_run?: boolean }) =>
    request<{ roots: { root: string; fixed: number; planned: number; errors: string[] }[] }>(
      '/api/v1/orphans/repair',
      json(payload),
    ),
  cleanOrphans: (payload: { root?: string; dry_run?: boolean }) =>
    request<Record<string, unknown>>('/api/v1/orphans/clean', json(payload)),
  aiReviewOrphans: (payload: { root?: string; paths?: string[]; apply?: boolean; min_confidence?: number }) =>
    request<{ items: Record<string, unknown>[]; applied: boolean }>(
      '/api/v1/orphans/ai-review',
      json(payload),
    ),
  replaceOrphan: (payload: { root: string; path: string; target?: string; confirm: true }) =>
    request<{ ok: boolean; new_sidecar: string }>('/api/v1/orphans/replace', json(payload)),
}

/** 上传类请求共用：把响应解析成同一套错误语义。 */
export async function parseResponse<T>(response: Response): Promise<T> {
  const text = await response.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { detail: text }
    }
  }
  if (!response.ok) {
    if (response.status === 422) {
      const detail = (data as { detail?: unknown } | null)?.detail
      if (typeof detail === 'string' && detail.trim()) throw new ApiError(detail, 422)
      throw new ApiError('请求参数不正确，请刷新页面后重试', 422)
    }
    const detail = (data as { detail?: string } | null)?.detail
    throw new ApiError(typeof detail === 'string' ? detail : `请求失败（HTTP ${response.status}）`, response.status)
  }
  return data as T
}

export type { RootStats, DirNode }
