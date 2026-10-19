<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watchEffect } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { DirNode, LibraryDirs, TaskRecord, WatchStatus } from '@/api/types'
import { useMeta } from '@/stores/meta'
import { fmtAgo, fmtNumber } from '@/utils/format'
import { TASK_KIND_LABEL, TASK_STATE_LABEL } from '@/utils/labels'
import PageHead from '@/components/PageHead.vue'
import EmptyState from '@/components/EmptyState.vue'
import AppIcon from '@/components/AppIcon.vue'

const meta = useMeta()

const tasks = ref<TaskRecord[]>([])
const tasksLoading = ref(false)
const watch = ref<WatchStatus | null>(null)

const scopeMode = ref<'all' | 'dir'>('all')
const dirs = ref<LibraryDirs | null>(null)
const dirsLoading = ref(false)
const scopePath = ref('')
const limit = ref<number | undefined>(undefined)
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
    tasks.value = (await api.tasks(12)).items
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

/** 筛选说明：缺失项之间是「或」，与歌词状态之间是「与」。 */
const filterHint = computed(() => {
  const sides: string[] = []
  if (missingSidecar.value) sides.push('缺 .lrc 文件')
  if (missingEmbedded.value) sides.push('缺内嵌歌词')
  const stateActive = lyricState.value !== 'all'
  if (!sides.length) return stateActive ? '只处理该状态的曲目' : ''
  const sideText = sides.join('或')
  return stateActive ? `状态符合，且${sideText}` : `只处理${sideText}的曲目`
})

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
        lyricState.value !== 'all' || missingSidecar.value || missingEmbedded.value
          ? {
              lyric_state: lyricState.value === 'all' ? undefined : lyricState.value,
              missing_sidecar: missingSidecar.value,
              missing_embedded: missingEmbedded.value,
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
      await api.startApply({ root: selectedDir.value?.root })
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

function summaryOf(task: TaskRecord): string {
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

onMounted(async () => {
  await loadTasks()
  await loadWatch()
  poll = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    if (runningCount.value > 0) {
      void loadTasks()
      void loadWatch()
      void meta.refresh().catch(() => undefined)
    }
  }, 3000)
})

onUnmounted(() => clearInterval(poll))
</script>

<template>
  <PageHead>
    <el-button :loading="tasksLoading" @click="loadTasks">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
    </el-button>
  </PageHead>

  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

  <section class="card">
    <div class="card-head"><span class="card-title">刮削</span></div>
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
          <EmptyState v-if="!flatDirs.length" title="没有读到目录" icon="FolderOpened" />
        </div>
      </div>

      <div class="field">
        <span class="field-label">筛选</span>
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
        <el-checkbox v-model="applyNow">抓完立即写入</el-checkbox>
        <el-tooltip
          content="连同已有歌词的曲目一起重新查询，可能替换现有歌词"
          placement="top"
        >
          <el-checkbox v-model="force">全量刮削</el-checkbox>
        </el-tooltip>
        <div class="submit">
          <span v-if="!meta.writeEnabled && applyNow" class="cell-meta">
            写入开关关闭，取消勾选「立即写入」才能抓取
          </span>
          <el-button
            type="primary"
            :loading="starting === 'fetch'"
            :disabled="!meta.writeEnabled && applyNow"
            @click="start('fetch')"
          >
            开始刮削
          </el-button>
        </div>
      </div>

      <div class="form-note cell-meta">
        {{
          force
            ? '全量刮削会重新查询全部曲目，包括已有歌词的，可能替换现有歌词。'
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
        <RouterLink to="/tags">
          <el-button>打开标签刮削</el-button>
        </RouterLink>
      </div>
    </section>
  </div>

  <div class="grid-2">
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

    <section class="card">
      <div class="card-head">
        <span class="card-title">任务记录</span>
        <span v-if="runningCount" class="card-hint">{{ runningCount }} 个进行中</span>
      </div>
      <div class="card-body flush">
        <div v-for="task in tasks" :key="task.id" class="task">
          <div class="task-top">
            <span class="task-kind">{{ TASK_KIND_LABEL[task.kind] ?? task.kind }}</span>
            <el-tag
              size="small"
              :type="task.state === 'done' ? 'success' : task.state === 'failed' ? 'danger' : 'warning'"
            >
              {{ TASK_STATE_LABEL[task.state] ?? task.state }}
            </el-tag>
            <span class="cell-meta num" style="margin-left: auto">{{ fmtAgo(task.started_at) }}</span>
          </div>
          <div v-if="task.state === 'running'" class="progress">
            <div class="progress-bar"><i /></div>
            <span class="cell-meta ellipsis">{{ task.progress || '准备中' }}</span>
          </div>
          <div v-else-if="task.error" class="task-error">{{ task.error }}</div>
          <div v-else-if="summaryOf(task)" class="cell-meta">{{ summaryOf(task) }}</div>
        </div>
        <EmptyState v-if="!tasks.length" title="还没有任务" hint="从上面开始一次抓取或写入。" icon="MagicStick" />
      </div>
    </section>
  </div>
</template>

<style scoped>
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
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 14px;
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

.task {
  padding: 11px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.task:last-child {
  border-bottom: 0;
}

.task-top {
  display: flex;
  align-items: center;
  gap: 8px;
}

.task-kind {
  font-size: 13px;
  font-weight: 550;
}

.task-error {
  font-size: 12px;
  color: var(--danger);
}

.progress {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.progress-bar {
  height: 4px;
  border-radius: 999px;
  background: var(--surface-3);
  overflow: hidden;
  position: relative;
}

.progress-bar i {
  position: absolute;
  inset: 0;
  width: 38%;
  border-radius: 999px;
  background: var(--accent);
  animation: slide 1.4s ease-in-out infinite;
}

@keyframes slide {
  0% {
    transform: translateX(-100%);
  }
  100% {
    transform: translateX(300%);
  }
}
</style>
