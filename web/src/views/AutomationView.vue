<script setup lang="ts">
/**
 * 自动化控制台：批量任务、任务历史、来源健康三个页签。
 *
 *   批量任务 —— 手动发起歌词抓取与写入，查看监控心跳；
 *   任务历史 —— 最近 30 次批量任务；入库与歌词抓取可展开，逐文件看每一步结果；
 *   来源健康 —— 非官方来源最近 30 天的探测计数（成功 / 未找到 / 风控 / 结构变更）。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watchEffect } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type {
  RunStep, DirNode, LibraryArtist, LibraryDirs, TaskRecord, WatchStatus } from '@/api/types'
import { useMeta } from '@/stores/meta'
import { fmtAgo, fmtNumber } from '@/utils/format'
import {
  TASK_KIND_LABEL,
  STEP_KIND_LABEL,
  STEP_STATE_LABEL,
  PROVIDER_LABEL, TASK_STATE_LABEL } from '@/utils/labels'
import EmptyState from '@/components/EmptyState.vue'
import AppIcon from '@/components/AppIcon.vue'

/* ---------------------------------------------------- 新接口的最小请求封装 */

/**
 * 一次批量任务的逐文件明细：每行是「一个文件 × 一个环节」的结果。
 * 接口走 client.ts 的 run / providerHealth（同一套错误语义）。
 */
type ProviderHealthRow = {
  provider: string
  ok: number
  not_found: number
  blocked: number
  schema_error: number
  last_error: string | null
}

/* ------------------------------------------------------------ 文案映射 */
// 枚举中文名统一放 utils/labels.ts；这里只保留取值函数。

function taskKindLabel(kind: string): string {
  return TASK_KIND_LABEL[kind] ?? kind
}

function stepTagType(state: string): 'success' | 'warning' | 'danger' | 'info' {
  if (state === 'done') return 'success'
  if (state === 'review') return 'warning'
  if (state === 'failed') return 'danger'
  return 'info'
}

/** detail 是零散的键值对，拼成一句纯文本说明。 */
function stepDetailText(detail: Record<string, unknown>): string {
  const parts: string[] = []
  for (const value of Object.values(detail)) {
    if (typeof value === 'string' || typeof value === 'number') parts.push(String(value))
  }
  return parts.join('；')
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/* ------------------------------------------------------------ 页面状态 */

const meta = useMeta()

type ConsoleTab = 'batch' | 'history' | 'health'
const tab = ref<ConsoleTab>('batch')

const tasks = ref<TaskRecord[]>([])
const tasksLoading = ref(false)
const tasksError = ref('')
const watch = ref<WatchStatus | null>(null)

const health = ref<ProviderHealthRow[]>([])
const healthLoading = ref(false)
const healthError = ref('')

const scopeMode = ref<'all' | 'dir'>('all')
const dirs = ref<LibraryDirs | null>(null)
const dirsLoading = ref(false)
const scopePath = ref('')
const limit = ref<number | undefined>(undefined)
/** 按艺人筛选（单选）。包含即命中：一个文件有多个艺人时，选中任意一个都算。 */
const artist = ref('')
const artists = ref<LibraryArtist[]>([])
const artistsLoading = ref(false)
/** 筛选：只处理符合「歌词现状」的曲目，条件之间是与关系。 */
const lyricState = ref<'all' | 'none' | 'unsynced'>('all')
const missingSidecar = ref(false)
const missingEmbedded = ref(false)
const force = ref(false)
const applyNow = ref(true)
// 只读实例默认不勾选「立即写入」，否则开始按钮会被自己锁住。
watchEffect(() => {
  if (!meta.writeEnabled) applyNow.value = false
})
const starting = ref('')
const error = ref('')

let poll: ReturnType<typeof setInterval> | undefined

const pending = computed(() => meta.summary?.counts.staged_pending ?? 0)
const watched = computed(() => watch.value ?? meta.watch)

async function loadTasks() {
  tasksLoading.value = true
  try {
    tasks.value = (await api.tasks(30)).items
    tasksError.value = ''
  } catch (err) {
    tasksError.value = err instanceof Error ? err.message : '任务记录加载失败'
  } finally {
    tasksLoading.value = false
  }
}

async function loadWatch() {
  try {
    watch.value = await api.watch()
  } catch {
    /* 监控状态取不到不影响操作 */
  }
}

async function loadHealth() {
  healthLoading.value = true
  try {
    health.value = (await api.providerHealth()).providers
    healthError.value = ''
  } catch (err) {
    healthError.value = err instanceof Error ? err.message : '来源健康加载失败'
  } finally {
    healthLoading.value = false
  }
}

/** 批量任务页签的刷新：任务列表与监控心跳一起看。 */
async function refreshBatch() {
  await Promise.all([loadTasks(), loadWatch()])
}

/* ------------------------------------------------------------ 任务明细 */

/** 每个任务一份展开明细；展开时才请求，只请求一次。 */
type StepsState = { loading: boolean; error: string; items: RunStep[]; total: number }
const stepsState = reactive<Record<string, StepsState>>({})

function stepsOf(taskId: string): StepsState {
  return stepsState[taskId] ?? { loading: false, error: '', items: [], total: 0 }
}

async function loadSteps(task: TaskRecord) {
  const known = stepsState[task.id]
  if (known?.loading || (known && known.total > 0)) return
  stepsState[task.id] = { loading: true, error: '', items: [], total: 0 }
  try {
    const data = await api.run(task.id)
    stepsState[task.id] = {
      loading: false,
      error: '',
      items: data.steps,
      total: data.total_steps,
    }
  } catch (err) {
    stepsState[task.id] = {
      loading: false,
      error: err instanceof Error ? err.message : '任务明细加载失败',
      items: [],
      total: 0,
    }
  }
}

/** 只有入库与歌词抓取记录逐文件明细；其它类型展开只给一句说明。 */
function onTaskExpand(row: TaskRecord, expanded: TaskRecord[]) {
  if (!expanded.some((item) => item.id === row.id)) return
  if (row.kind !== 'ingest' && row.kind !== 'fetch') return
  void loadSteps(row)
}

/** 筛选说明：缺失项之间是「或」，与歌词状态之间是「与」。 */
const filterHint = computed(() => {
  const sides: string[] = []
  if (missingSidecar.value) sides.push('缺 .lrc 文件')
  if (missingEmbedded.value) sides.push('缺内嵌歌词')
  const stateActive = lyricState.value !== 'all'
  const parts: string[] = []
  if (stateActive) parts.push('状态符合')
  if (sides.length) parts.push(sides.join('或'))
  if (artist.value) parts.push(`艺人包含「${artist.value}」`)
  if (!parts.length) return ''
  return `只处理${parts.join('，且')}的曲目`
})

async function loadArtists() {
  artistsLoading.value = true
  try {
    artists.value = (await api.libraryArtists()).items
  } catch {
    /* 艺人列表取不到不影响刮削 */
  } finally {
    artistsLoading.value = false
  }
}

async function loadDirs() {
  dirsLoading.value = true
  try {
    dirs.value = await api.libraryDirs()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '目录列表加载失败'
  } finally {
    dirsLoading.value = false
  }
}

const flatDirs = computed<{ root: string; node: DirNode }[]>(() => {
  const out: { root: string; node: DirNode }[] = []
  for (const root of dirs.value?.roots ?? []) {
    for (const node of root.dirs) out.push({ root: root.root, node })
  }
  return out
})

const selectedDir = computed(() => flatDirs.value.find((item) => item.node.path === scopePath.value) ?? null)

function chooseDirMode() {
  scopeMode.value = 'dir'
  if (!dirs.value) void loadDirs()
}

async function start(kind: 'fetch' | 'apply') {
  starting.value = kind
  error.value = ''
  try {
    if (kind === 'fetch') {
      const filters =
        lyricState.value !== 'all' || missingSidecar.value || missingEmbedded.value || artist.value
          ? {
              lyric_state: lyricState.value === 'all' ? undefined : lyricState.value,
              missing_sidecar: missingSidecar.value,
              missing_embedded: missingEmbedded.value,
              artist: artist.value || undefined,
            }
          : undefined
      await api.startFetch({
        root: selectedDir.value?.root,
        dir: scopeMode.value === 'dir' ? scopePath.value : undefined,
        limit: limit.value || undefined,
        force: force.value,
        apply: applyNow.value,
        filters,
      })
    } else {
      const task = await api.startApply({ root: selectedDir.value?.root })
      ElMessage.success('写入任务已开始，完成后会显示结果')
      void watchTask(task.id)
      await loadTasks()
      await meta.refresh().catch(() => undefined)
      return
    }
    ElMessage.success('任务已开始')
    await loadTasks()
    await meta.refresh().catch(() => undefined)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '任务启动失败'
  } finally {
    starting.value = ''
  }
}

/**
 * 跟踪一个刚提交的任务直到终态：成功给结果，失败给出原因。
 *
 * 没有它，用户点「开始写入」后只能看到「任务已开始」——写入被租约挡住、
 * 或者中途失败时，界面不会有任何提示，看起来就像「一直没写入」。
 */
async function watchTask(taskId: string) {
  for (let i = 0; i < 60; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 2000))
    let task: TaskRecord | undefined
    try {
      const detail = await api.run(taskId)
      task = detail.task as unknown as TaskRecord
      // 任务的 result 字段是结构化的，用它还原摘要
      task = { ...task, result: detail.task.result }
    } catch {
      // 任务记录可能还没落库：退回任务列表
      const list = await api.tasks(5).catch(() => ({ items: [] as TaskRecord[] }))
      task = list.items.find((item) => item.id === taskId)
    }
    if (!task) continue
    if (task.state === 'failed') {
      ElMessage.error(task.error ? `任务失败：${task.error}` : '任务失败')
      await loadTasks()
      return
    }
    if (task.state === 'done') {
      const summary = summaryOf(task)
      ElMessage.success(summary ? `任务完成：${summary}` : '任务完成')
      await loadTasks()
      await meta.refresh().catch(() => undefined)
      return
    }
  }
}

function summaryOf(task: TaskRecord): string {
  // 失败任务优先显示失败原因——此前一律显示「—」，用户看到的就是
  // 「点了开始写入，提示任务已开始，但一直没写入」，无从知道被什么卡住。
  if (task.state === 'failed') {
    return task.error ? `失败：${task.error}` : '失败'
  }
  const result = task.result as
    | { roots?: { root?: string; scanned?: number; fetched?: number; accepted?: number; needs_review?: number; applied?: number | { written?: number }; errors?: string[]; total?: number; updatable?: number; conflicts?: number }[] }
    | null
  if (!result?.roots?.length) return ''
  return result.roots
    .map((root) => {
      const parts: string[] = []
      if (typeof root.scanned === 'number') parts.push(`扫描 ${root.scanned}`)
      if (typeof root.fetched === 'number') parts.push(`抓取 ${root.fetched}`)
      if (typeof root.accepted === 'number') parts.push(`命中 ${root.accepted}`)
      if (typeof root.needs_review === 'number') parts.push(`待审查 ${root.needs_review}`)
      if (typeof root.applied === 'number') parts.push(`写入 ${root.applied}`)
      // 网络失败与「未找到」是两回事：混在零里看，用户会以为「查过没有」
      const transientCount = (root.errors ?? []).filter((e) =>
        String(e).includes('取词失败') || String(e).includes('Transient') || String(e).includes('RateLimited'),
      ).length
      if (transientCount > 0) parts.push(`网络失败 ${transientCount}`)
      if (root.applied && typeof root.applied === 'object' && root.applied.written) {
        parts.push(`写入 ${root.applied.written}`)
      }
      if (typeof root.total === 'number') parts.push(`检查 ${root.total}`)
      if (typeof root.updatable === 'number') parts.push(`可补 ${root.updatable}`)
      if (root.errors?.length) parts.push(`${root.errors.length} 条错误`)
      return parts.join(' · ')
    })
    .filter(Boolean)
    .join('；')
}

const runningCount = computed(() => tasks.value.filter((task) => task.state === 'running').length)

/** 按当前范围批量补封面：沿用范围选择器（全库或指定目录）。 */
async function startCover() {
  starting.value = 'cover'
  error.value = ''
  try {
    await api.startCoverFetch({
      root: selectedDir.value?.root,
      dir: scopeMode.value === 'dir' ? scopePath.value : undefined,
      apply: meta.writeEnabled,
    })
    ElMessage.success('封面抓取任务已开始，可在「任务历史」查看进度')
    await loadTasks()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '封面抓取任务发起失败'
  } finally {
    starting.value = ''
  }
}

onMounted(async () => {
  void loadArtists()
  void loadHealth()
  await loadTasks()
  await loadWatch()
  poll = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    if (runningCount.value > 0) {
      // 只刷任务与心跳：meta.refresh 里的待办计数含全库孤儿扫描，
      // 每 3 秒一次会让 NAS 磁盘永不休眠；徽标由 45 秒全局轮询负责
      void loadTasks()
      void loadWatch()
    }
  }, 3000)
})

onUnmounted(() => clearInterval(poll))
</script>

<template>
  <el-tabs v-model="tab" class="auto-tabs">
    <!-- 批量任务：手动发起抓取与写入，顺带看监控心跳 -->
    <el-tab-pane label="批量任务" name="batch">
      <div class="queue-bar">
        <span class="queue-hint">歌词抓取与写入都在这里发起，下方是监控心跳。</span>
        <el-button :loading="tasksLoading" @click="refreshBatch">
          <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
        </el-button>
      </div>

      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

      <section class="card">
        <div class="card-head"><span class="card-title">歌词抓取</span></div>
        <div class="card-body form">
          <div class="field">
            <span class="field-label">范围</span>
            <div class="seg">
              <button :class="{ on: scopeMode === 'all' }" @click="scopeMode = 'all'">全库</button>
              <button :class="{ on: scopeMode === 'dir' }" @click="chooseDirMode">
                指定目录
              </button>
            </div>
            <span v-if="scopeMode === 'dir' && selectedDir" class="cell-meta">
              {{ selectedDir.node.path || selectedDir.root }} · {{ selectedDir.node.audio }} 首 ·
              缺 .lrc {{ selectedDir.node.missing_lrc }}
            </span>
          </div>

          <div v-if="scopeMode === 'dir'" class="dirs">
            <el-skeleton v-if="dirsLoading" animated :rows="4" />
            <div v-else class="dir-list">
              <button
                v-for="item in flatDirs"
                :key="`${item.root}:${item.node.path}`"
                class="dir"
                :class="{ on: scopePath === item.node.path }"
                :style="{ paddingLeft: 10 + item.node.depth * 14 + 'px' }"
                @click="scopePath = item.node.path"
              >
                <AppIcon name="FolderOpened" :size="13" />
                <span class="ellipsis">{{ item.node.name }}</span>
                <span class="cell-meta num">{{ item.node.audio }} 首</span>
                <span v-if="item.node.missing_lrc" class="cell-meta num miss">缺 {{ item.node.missing_lrc }}</span>
              </button>
              <EmptyState v-if="!flatDirs.length" title="未能读取目录" icon="FolderOpened" />
            </div>
          </div>

          <div class="field">
            <span class="field-label">筛选</span>
            <el-select
              v-model="artist"
              class="artist-select"
              filterable
              clearable
              placeholder="全部艺术家"
              :loading="artistsLoading"
            >
              <el-option
                v-for="item in artists"
                :key="item.name"
                :label="`${item.name}（${item.count}）`"
                :value="item.name"
              />
            </el-select>
            <el-select v-model="lyricState" class="state-select">
              <el-option label="全部曲目" value="all" />
              <el-option label="没有歌词" value="none" />
              <el-option label="没有时间轴" value="unsynced" />
            </el-select>
            <el-checkbox v-model="missingSidecar">缺 .lrc 文件</el-checkbox>
            <el-checkbox v-model="missingEmbedded">缺内嵌歌词</el-checkbox>
            <span v-if="filterHint" class="cell-meta">{{ filterHint }}</span>
          </div>

          <div class="field options">
            <span class="field-label">执行</span>
            <el-input-number v-model="limit" :min="1" :max="10000" :step="50" placeholder="不限数量" controls-position="right" class="limit" />
            <el-checkbox v-model="applyNow">抓取完成后立即写入</el-checkbox>
            <el-tooltip
              content="连同已有歌词的曲目一起重新查询，可能替换现有歌词"
              placement="top"
            >
              <el-checkbox v-model="force">全量抓取</el-checkbox>
            </el-tooltip>
            <div class="submit">
              <span v-if="!meta.writeEnabled && applyNow" class="cell-meta">
                写入开关关闭，取消勾选「抓取完成后立即写入」才能抓取
              </span>
              <el-button
                type="primary"
                :loading="starting === 'fetch'"
                :disabled="!meta.writeEnabled && applyNow"
                @click="start('fetch')"
              >
                开始抓取
              </el-button>
            </div>
          </div>

          <div class="form-note cell-meta">
            {{
              force
                ? '全量刮削会重新查询全部曲目，包括已有歌词的曲目，可能替换现有歌词。'
                : '默认只处理缺歌词或没有时间轴的曲目，已合格的会跳过。'
            }}
          </div>
        </div>
      </section>

      <div class="grid-2">
        <section class="card">
          <div class="card-head">
            <span class="card-title">写入操作</span>
            <span class="card-hint">{{ fmtNumber(pending) }} 首已确认</span>
          </div>
          <div class="card-body">
            <p class="desc">将已确认的歌词写进文件。</p>
            <el-button :loading="starting === 'apply'" :disabled="!meta.writeEnabled || !pending" @click="start('apply')">
              开始写入
            </el-button>
          </div>
        </section>

        <section class="card">
          <div class="card-head"><span class="card-title">标签</span></div>
          <div class="card-body">
            <p class="desc">按 MusicBrainz 补齐缺失标签，冲突交人工确认。</p>
            <RouterLink to="/todo?kind=tags">
              <el-button>打开标签待办</el-button>
            </RouterLink>
          </div>
        </section>

        <section class="card">
          <div class="card-head"><span class="card-title">封面</span></div>
          <div class="card-body">
            <p class="desc">
              按目录批量补封面：同目录规范命名图片自动嵌入；有发行 MBID 时从
              Cover Art Archive 按专辑一次下载。质量不足或无来源的进入待办确认。
            </p>
            <div class="cover-actions">
              <el-button
                :loading="starting === 'cover'"
                :disabled="scopeMode === 'dir' && !scopePath"
                @click="startCover()"
              >
                按当前范围补封面
              </el-button>
              <RouterLink to="/todo?kind=covers">
                <el-button text>查看缺封面清单</el-button>
              </RouterLink>
            </div>
          </div>
        </section>
      </div>

      <section class="card">
        <div class="card-head"><span class="card-title">监控</span></div>
        <div class="card-body">
          <div v-if="watched" class="watch">
            <div class="watch-line">
              <span class="status-dot" :class="watched.healthy ? '' : 'warn'" />
              <span>{{ watched.healthy ? '运行正常' : watched.running ? '心跳异常' : '未运行' }}</span>
              <span class="cell-meta">· 最后心跳 {{ fmtAgo(watched.heartbeat_at) }}</span>
            </div>
            <div v-if="watched.stage" class="cell-meta">当前：{{ watched.detail || watched.stage }}</div>
            <div v-if="watched.recent.length" class="recent">
              <div v-for="(event, index) in watched.recent.slice(0, 6)" :key="index" class="recent-row">
                <span class="cell-meta num">{{ fmtAgo(event.at) }}</span>
                <span class="ellipsis">{{ event.detail || event.stage }}</span>
              </div>
            </div>
          </div>
          <div v-else class="dim">监控没有上报状态。</div>
        </div>
      </section>
    </el-tab-pane>

    <!-- 任务历史：最近 30 次批量任务，入库与抓取可展开逐文件明细 -->
    <el-tab-pane label="任务历史" name="history">
      <div class="queue-bar">
        <span class="queue-hint">
          最近 30 次批量任务{{ runningCount ? `，${runningCount} 个进行中` : '' }}；入库与歌词抓取可展开看每个文件的结果。
        </span>
        <el-button size="small" :loading="tasksLoading" @click="loadTasks">
          <AppIcon name="Refresh" :size="13" />刷新
        </el-button>
      </div>

      <el-alert v-if="tasksError" :title="tasksError" type="error" show-icon :closable="false" />

      <el-table
        v-loading="tasksLoading"
        :data="tasks"
        row-key="id"
        class="task-table"
        @expand-change="onTaskExpand"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <div v-if="row.kind === 'ingest' || row.kind === 'fetch'" class="steps">
              <el-skeleton v-if="stepsOf(row.id).loading" animated :rows="3" />
              <div v-else-if="stepsOf(row.id).error" class="task-error">
                {{ stepsOf(row.id).error }}
              </div>
              <template v-else>
                <el-table :data="stepsOf(row.id).items" size="small" class="steps-table">
                  <el-table-column label="序号" prop="seq" width="64" />
                  <el-table-column label="环节" width="90">
                    <template #default="{ row: step }">
                      {{ STEP_KIND_LABEL[step.kind] ?? step.kind }}
                    </template>
                  </el-table-column>
                  <el-table-column label="文件" min-width="220" show-overflow-tooltip>
                    <template #default="{ row: step }">{{ step.rel_path || '—' }}</template>
                  </el-table-column>
                  <el-table-column label="状态" width="88">
                    <template #default="{ row: step }">
                      <el-tag size="small" effect="plain" :type="stepTagType(step.state)">
                        {{ STEP_STATE_LABEL[step.state] ?? step.state }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="说明" min-width="200" show-overflow-tooltip>
                    <template #default="{ row: step }">
                      {{ stepDetailText(step.detail) || '—' }}
                    </template>
                  </el-table-column>
                </el-table>
                <div
                  v-if="stepsOf(row.id).total > stepsOf(row.id).items.length"
                  class="cell-meta steps-more"
                >
                  共 {{ stepsOf(row.id).total }} 条明细，这里只显示前 {{ stepsOf(row.id).items.length }} 条。
                </div>
                <div v-if="!stepsOf(row.id).items.length" class="cell-meta steps-more">
                  这次任务没有记录逐文件明细。
                </div>
              </template>
            </div>
            <div v-else class="cell-meta steps-more">这个任务类型没有逐文件明细。</div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">{{ taskKindLabel(row.kind) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="92">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.state === 'done' ? 'success' : row.state === 'failed' ? 'danger' : 'warning'"
            >
              {{ TASK_STATE_LABEL[row.state] ?? row.state }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="开始时间" width="120">
          <template #default="{ row }">
            <span class="cell-meta num">{{ fmtAgo(row.started_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.state === 'running'" class="cell-meta">{{ row.progress || '准备中' }}</span>
            <span v-else-if="row.error" class="task-error">{{ row.error }}</span>
            <span v-else class="cell-meta">{{ summaryOf(row) || '—' }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState
            title="还没有任务"
            hint="从「批量任务」发起一次抓取或写入，记录会出现在这里。"
            icon="MagicStick"
          />
        </template>
      </el-table>
    </el-tab-pane>

    <!-- 来源健康：监控每天探测一次，看最近 30 天的计数 -->
    <el-tab-pane label="来源健康" name="health">
      <div class="queue-bar">
        <span class="queue-hint">监控进程每天探测一次来源可用性，这里是最近 30 天的计数。</span>
        <el-button size="small" :loading="healthLoading" @click="loadHealth">
          <AppIcon name="Refresh" :size="13" />刷新
        </el-button>
      </div>

      <el-alert v-if="healthError" :title="healthError" type="error" show-icon :closable="false" />

      <div v-if="health.length" class="health-grid">
        <section v-for="item in health" :key="item.provider" class="card health-card">
          <div class="card-head">
            <span class="card-title">{{ PROVIDER_LABEL[item.provider] ?? item.provider }}</span>
            <span class="card-hint">最近 30 天</span>
          </div>
          <div class="card-body">
            <div class="health-counts">
              <div class="health-count">
                <span class="health-num num">{{ item.ok }}</span>
                <span class="health-label">成功</span>
              </div>
              <div class="health-count">
                <span class="health-num num">{{ item.not_found }}</span>
                <span class="health-label">未找到</span>
              </div>
              <div class="health-count">
                <span class="health-num num" :class="{ warn: item.blocked > 0 }">{{ item.blocked }}</span>
                <span class="health-label">风控</span>
              </div>
              <div class="health-count">
                <span class="health-num num" :class="{ warn: item.schema_error > 0 }">
                  {{ item.schema_error }}
                </span>
                <span class="health-label">结构变更</span>
              </div>
            </div>
            <div v-if="item.last_error" class="health-error ellipsis" :title="item.last_error">
              {{ truncate(item.last_error, 60) }}
            </div>
          </div>
        </section>
      </div>

      <EmptyState
        v-else
        v-loading="healthLoading"
        title="还没有探测数据"
        hint="监控进程每天探测一次来源可用性。"
        icon="CircleCheck"
      />
    </el-tab-pane>
  </el-tabs>
</template>

<style scoped>
.auto-tabs :deep(.el-tabs__content) {
  overflow: visible;
}

.queue-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.queue-hint {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  color: var(--text-3);
}

.task-table {
  margin-bottom: 14px;
}

.steps {
  padding: 4px 16px 10px 48px;
}

.steps-more {
  padding: 8px 0 2px;
}

.health-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr));
  gap: 14px;
}

.health-counts {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 28px;
}

.health-count {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.health-num {
  font-size: 20px;
  font-weight: 600;
}

.health-num.warn {
  color: var(--warn);
}

.health-label {
  font-size: 12px;
  color: var(--text-3);
}

.health-error {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--danger);
}

.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.field {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.field-label {
  width: 40px;
  font-size: 12px;
  color: var(--text-3);
  flex: none;
}

.limit {
  width: 130px;
}

.artist-select {
  width: 260px;
}

.state-select {
  width: 150px;
}

.form-note {
  margin-left: 52px;
  font-size: 12px;
}

.dirs {
  margin-left: 52px;
}

.dir-list {
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  max-height: 240px;
  overflow: auto;
  background: var(--surface-2);
}

.dir {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 10px;
  border: 0;
  background: transparent;
  color: var(--text-2);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  text-align: left;
}

.dir:hover {
  background: var(--surface-3);
}

.dir.on {
  background: var(--accent-soft);
  color: var(--accent-strong);
}

.dir .ellipsis {
  flex: 1;
  min-width: 0;
}

.miss {
  color: var(--warn);
}

.submit {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}

@media (max-width: 720px) {
  .queue-bar {
    flex-wrap: wrap;
  }

  .submit {
    margin-left: 0;
    width: 100%;
    justify-content: flex-end;
  }
}

.desc {
  font-size: 12.5px;
  color: var(--text-2);
  margin-bottom: 12px;
}

.grid-2 {
  display: grid;
  /* min(360px, 100%)：窄屏上列宽收缩到容器内，避免 minmax 固定下限把网格撑出视口 */
  grid-template-columns: repeat(auto-fit, minmax(min(360px, 100%), 1fr));
  gap: 14px;
  margin-bottom: 14px;
}

.cover-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.watch {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.watch-line {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
}

.recent {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}

.recent-row {
  display: flex;
  gap: 10px;
  font-size: 12px;
}

.recent-row .ellipsis {
  flex: 1;
  min-width: 0;
}

.task-error {
  font-size: 12px;
  color: var(--danger);
}
</style>
