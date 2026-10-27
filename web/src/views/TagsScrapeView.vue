<script setup lang="ts">
/**
 * 标签刮削：扫描 → 逐字段对照 → 自动补空 / 人工复核。
 *
 * 页面的信息层级按「先看什么」排：
 *   扫描卡 —— 现在跑不跑、上次跑出了什么；
 *   指标   —— 有多少可以自动写、多少要人看、多少没找到；
 *   三个标签 —— 推荐采纳 / 待审查 / 未匹配。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type {
  TagHistoryRow,
  TagProposal,
  TagSummary,
  TaskRecord,
} from '@/api/types'
import { useMeta } from '@/stores/meta'
import { useNarrow } from '@/composables/useMedia'
import { fmtAgo, fmtInterval, fmtNumber } from '@/utils/format'
import { tagFieldLabel } from '@/utils/labels'
import PageHead from '@/components/PageHead.vue'
import EmptyState from '@/components/EmptyState.vue'
import AppIcon from '@/components/AppIcon.vue'
import TagProposalRow from '@/components/TagProposalRow.vue'
import TagLinkDialog from '@/components/TagLinkDialog.vue'
import TrackFixDrawer from '@/components/TrackFixDrawer.vue'

type Tab = 'auto' | 'review' | 'unmatched' | 'ignored'

const meta = useMeta()
const narrow = useNarrow()
const route = useRoute()
const router = useRouter()

const requested = typeof route.query.tab === 'string' ? route.query.tab : ''
const tab = ref<Tab>(
  requested === 'review' ||
    requested === 'unmatched' ||
    requested === 'auto' ||
    requested === 'ignored'
    ? requested
    : 'auto',
)

const summary = ref<TagSummary | null>(null)
const items = ref<TagProposal[]>([])
const total = ref(0)
const page = ref(1)
const size = 20
const q = ref('')
const appliedQuery = ref('')
const loading = ref(false)
const error = ref('')

const autoWrite = ref(true)
const fingerprint = ref(true)
const scanning = ref(false)
const scanProgress = ref('')
const scanTask = ref('')
const bulkBusy = ref(false)

const linkOpen = ref(false)
const linkTarget = ref<TagProposal | null>(null)
const linkSeed = ref('')

const drawerOpen = ref(false)
const drawerRoot = ref('')
const drawerPath = ref('')

const historyOpen = ref(false)
const historyLoading = ref(false)
const history = ref<TagHistoryRow[]>([])

let poll: ReturnType<typeof setInterval> | undefined
let summaryPoll: ReturnType<typeof setInterval> | undefined

const counts = computed(
  () => summary.value?.counts ?? { auto: 0, review: 0, unmatched: 0, ignored: 0 },
)
const source = computed(() => summary.value?.source)
const activeCount = computed(() => counts.value[tab.value] ?? 0)

const tabLabels: Record<Tab, string> = {
  auto: '推荐采纳',
  review: '待审查',
  unmatched: '未匹配',
  ignored: '已忽略',
}

const grouped = computed(() => {
  const out: { key: string; album: string; artist: string; items: TagProposal[] }[] = []
  const index = new Map<string, number>()
  for (const item of items.value) {
    const key = item.track.dir || item.album || '未分类'
    let at = index.get(key)
    if (at === undefined) {
      at = out.length
      index.set(key, at)
      const folder = key.split('/').filter(Boolean)
      out.push({
        key,
        album: item.album || folder[folder.length - 1] || '未分类',
        artist: item.track.albumartist || item.track.artists?.[0] || '',
        items: [],
      })
    }
    out[at].items.push(item)
  }
  return out
})

const lastScanLine = computed(() => {
  if (!summary.value?.last_scan_at) return '还没有扫描过'
  const at = fmtAgo(summary.value.last_scan_at)
  const roots = summary.value.last_scan?.roots ?? []
  // 状态数量看上面的 KPI；这里只记录上次扫描本身，避免历史数字
  // 和当前队列（人工处理/自动写入之后）对不上。
  const parts = roots.map((root) => {
    const bits = [`扫描 ${fmtNumber(root.scanned)} 首`]
    if (root.written) bits.push(`写入 ${fmtNumber(root.written)}`)
    if (root.errors.length) bits.push(`${fmtNumber(root.errors.length)} 处异常`)
    return bits.join('，')
  })
  return `${at}${parts.length ? ` · ${parts.join('；')}` : ''}`
})

const sourceLine = computed(() => {
  if (!source.value) return ''
  const bits: string[] = []
  bits.push(source.value.musicbrainz ? 'MusicBrainz 已连接' : 'MusicBrainz 未配置')
  if (!source.value.acoustid) bits.push('指纹未启用')
  else if (!source.value.fingerprint_tool) bits.push('指纹工具缺失')
  else bits.push('指纹可用')
  if (summary.value?.policy.auto_scan) {
    bits.push(`每 ${fmtInterval(summary.value.policy.scan_interval_hours)}自动扫描`)
  } else {
    bits.push('自动扫描已关闭')
  }
  return bits.join(' · ')
})

const reviewNotice = computed(() => {
  if (tab.value === 'auto') {
    return {
      title: '推荐采纳',
      text: `共 ${fmtNumber(counts.value.auto)} 首，来源与曲目一致，没有需要人工判断的冲突。`,
    }
  }
  if (tab.value === 'review') {
    return {
      title: '待审查',
      text: `共 ${fmtNumber(counts.value.review)} 首，存在与本地不同的字段。冲突默认勾选，可一键取消；写入前会预览。`,
    }
  }
  if (tab.value === 'unmatched') {
    return {
      title: '未匹配',
      text: `共 ${fmtNumber(counts.value.unmatched)} 首，没有找到可信的 MusicBrainz 条目。可以粘贴链接指定，或手动编辑。`,
    }
  }
  return {
    title: '已忽略',
    text: `共 ${fmtNumber(counts.value.ignored)} 首，是你自己选择不再看的条目。扫描不会把它们翻回来，随时可以恢复。`,
  }
})

const pageFillCount = computed(() =>
  items.value.reduce(
    (sum, item) => sum + (item.summary.fill ?? 0) + (item.summary.correct ?? 0),
    0,
  ),
)

async function loadSummary() {
  try {
    const first = summary.value === null
    const data = await api.tagsSummary()
    summary.value = data
    // 只在首次加载时同步两个开关；之后的轮询只更新状态文字和计数，
    // 不覆盖使用者为下一次扫描临时改的选择。
    if (first) {
      autoWrite.value = data.policy.auto_write
      fingerprint.value = data.policy.fingerprint
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '状态加载失败'
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api.tagProposals({
      state: tab.value,
      q: appliedQuery.value,
      limit: size,
      offset: (page.value - 1) * size,
    })
    items.value = data.items
    total.value = data.total
  } catch (err) {
    error.value = err instanceof Error ? err.message : '提案加载失败'
  } finally {
    loading.value = false
  }
}

async function refreshAll() {
  await Promise.all([loadSummary(), load()])
}

function applySearch() {
  appliedQuery.value = q.value.trim()
  page.value = 1
  void load()
}

function resetSearch() {
  if (!q.value) return
  q.value = ''
  applySearch()
}

function changeTab(value: string | number) {
  tab.value = String(value) as Tab
}

watch(tab, (value) => {
  page.value = 1
  void router.replace({ query: { ...route.query, tab: value } })
  void load()
})

watch(page, () => void load())

function stopPolling() {
  if (poll) {
    clearInterval(poll)
    poll = undefined
  }
}

function pollTask(id: string) {
  stopPolling()
  scanTask.value = id
  scanning.value = true
  poll = setInterval(async () => {
    try {
      const task = await api.task(id)
      scanProgress.value = task.progress || '正在扫描…'
      if (task.state === 'running') return
      stopPolling()
      scanning.value = false
      if (task.state === 'done') {
        const roots = (task.result as { roots?: { auto?: number; review?: number; unmatched?: number; written?: number }[] } | null)?.roots ?? []
        const written = roots.reduce((sum, root) => sum + (root.written ?? 0), 0)
        ElMessage.success(written ? `扫描完成，已自动写入 ${written} 首` : '扫描完成')
      } else {
        error.value = task.error || '扫描失败'
      }
      page.value = 1
      await refreshAll()
      await meta.refresh().catch(() => undefined)
    } catch (err) {
      stopPolling()
      scanning.value = false
      error.value = err instanceof Error ? err.message : '任务状态读取失败'
    }
  }, 2000)
}

async function resumeRunningTask() {
  try {
    const tasks = await api.tasks(8)
    const running = tasks.items.find(
      (task: TaskRecord) => task.kind === 'tags-scan' && task.state === 'running',
    )
    if (running) pollTask(running.id)
  } catch {
    /* 没有任务记录不影响页面 */
  }
}

async function startScan() {
  if (!source.value?.musicbrainz) {
    ElMessage.warning('还没有配置 MusicBrainz')
    return
  }
  if (autoWrite.value) {
    try {
      await ElMessageBox.confirm(
        '扫描后会直接写入「本地为空、来源有值」的字段；两边都有值且不同的会留给人确认，不会被自动覆盖。',
        '扫描并自动写入',
        { confirmButtonText: '开始扫描', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  try {
    const task = await api.startTagsScan({
      auto_write: autoWrite.value,
      fingerprint: fingerprint.value,
    })
    scanProgress.value = '任务已开始'
    pollTask(task.id)
    ElMessage.success('扫描已开始')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '扫描启动失败'
  }
}

async function applyAllAuto() {
  if (!counts.value.auto) return
  try {
    await ElMessageBox.confirm(
      `将写入 ${fmtNumber(counts.value.auto)} 首曲目的可补字段，冲突不会覆盖。`,
      '全部采用',
      { confirmButtonText: '全部采用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  bulkBusy.value = true
  try {
    const result = await api.bulkApplyTags({ scope: 'auto' })
    ElMessage.success(`已写入 ${result.files} 首、${result.fields} 个字段`)
    if (result.errors.length) ElMessage.warning(result.errors[0])
    await refreshAll()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '写入失败')
  } finally {
    bulkBusy.value = false
  }
}

async function applyPageFills() {
  const ids = items.value
    .filter(
      (item) =>
        item.state !== 'unmatched' &&
        item.source !== 'search' &&
        (item.summary.fill ?? 0) + (item.summary.correct ?? 0) > 0,
    )
    .map((item) => item.id)
  if (!ids.length) {
    ElMessage.info('本页没有可批量写入的安全字段；标题搜索项请逐首确认')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将写入本页 ${ids.length} 首的可补字段，冲突不会覆盖。`,
      '写入可补字段',
      { confirmButtonText: '写入', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  bulkBusy.value = true
  try {
    const result = await api.bulkApplyTags({ scope: 'fills', ids })
    ElMessage.success(`已写入 ${result.files} 首、${result.fields} 个字段`)
    if (result.errors.length) ElMessage.warning(result.errors[0])
    await refreshAll()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '写入失败')
  } finally {
    bulkBusy.value = false
  }
}

function openLink(item: TagProposal, seed = '') {
  linkTarget.value = item
  linkSeed.value = seed
  linkOpen.value = true
}

async function afterLink() {
  await refreshAll()
}

function openEditor(item: TagProposal) {
  drawerRoot.value = item.root
  drawerPath.value = item.rel_path
  drawerOpen.value = true
}

async function openHistory() {
  historyOpen.value = true
  historyLoading.value = true
  try {
    history.value = (await api.tagHistory(60)).items
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '写入记录加载失败')
  } finally {
    historyLoading.value = false
  }
}

async function undo(row: TagHistoryRow) {
  try {
    await ElMessageBox.confirm(
      `将把「${row.rel_path.split('/').pop()}」的标签还原到写入前，只还原这次写入涉及的字段。`,
      '撤销写入',
      { confirmButtonText: '撤销', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.undoTagsWrite(row.id)
    ElMessage.success('已撤销')
    await openHistory()
    await refreshAll()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '撤销失败')
  }
}

onMounted(async () => {
  await refreshAll()
  await resumeRunningTask()
  // 设置页改了扫描间隔后，已打开的标签页也能在一分钟内看到新提示。
  summaryPoll = setInterval(() => void loadSummary(), 60000)
})

onUnmounted(() => {
  stopPolling()
  if (summaryPoll) clearInterval(summaryPoll)
})
</script>

<template>
  <PageHead :count="counts.auto + counts.review + counts.unmatched">
    <el-button size="small" class="head-btn" @click="openHistory">
      <AppIcon name="Clock" :size="13" /><span class="head-label">写入记录</span>
    </el-button>
    <el-button size="small" class="head-btn" :loading="loading" @click="refreshAll">
      <AppIcon name="Refresh" :size="13" /><span class="head-label">刷新</span>
    </el-button>
  </PageHead>

  <section class="card scan-card">
    <div class="card-head">
      <span class="card-title">扫描</span>
      <span class="card-hint">按 MusicBrainz 补齐缺失标签；自动写入只补缺失，冲突不覆盖。</span>
      <div class="card-actions scan-actions">
        <el-switch v-model="autoWrite" size="small" active-text="自动写入" />
        <el-switch v-model="fingerprint" size="small" active-text="指纹校验" />
        <el-button
          type="primary"
          :loading="scanning"
          :disabled="!source?.musicbrainz"
          @click="startScan"
        >
          开始扫描
        </el-button>
      </div>
    </div>
    <div class="card-body scan-body">
      <div class="scan-line">
        <span class="dim">{{ lastScanLine }}</span>
        <span v-if="sourceLine" class="dim">· {{ sourceLine }}</span>
      </div>
      <div v-if="scanning" class="scan-progress">
        <span class="pulse" />
        <span>{{ scanProgress }}</span>
      </div>
      <el-alert
        v-if="!source?.musicbrainz"
        type="warning"
        :closable="false"
        show-icon
        title="没有配置 MusicBrainz，标签刮削不可用。"
        class="gap"
      />
    </div>
  </section>

  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-label"><AppIcon name="MagicStick" :size="13" />可自动写入</div>
      <div class="kpi-value num">{{ fmtNumber(counts.auto) }}<small>首</small></div>
      <div class="kpi-foot">无冲突，扫描后可直接补齐</div>
    </div>
    <div class="kpi">
      <div class="kpi-label"><AppIcon name="Warning" :size="13" />待审查</div>
      <div class="kpi-value num">{{ fmtNumber(counts.review) }}<small>首</small></div>
      <div class="kpi-foot">有冲突或需要确认来源</div>
    </div>
    <div class="kpi">
      <div class="kpi-label"><AppIcon name="Search" :size="13" />未匹配</div>
      <div class="kpi-value num">{{ fmtNumber(counts.unmatched) }}<small>首</small></div>
      <div class="kpi-foot">没有找到可信条目</div>
    </div>
    <div class="kpi">
      <div class="kpi-label"><AppIcon name="Clock" :size="13" />上次扫描</div>
      <div class="kpi-value last-scan">{{ summary?.last_scan_at ? fmtAgo(summary.last_scan_at) : '—' }}</div>
      <div class="kpi-foot">自动扫描按设置间隔运行</div>
    </div>
  </div>

  <el-tabs :model-value="tab" class="tags-tabs" @tab-change="changeTab">
    <el-tab-pane :label="`${tabLabels.auto} ${counts.auto}`" name="auto" />
    <el-tab-pane :label="`${tabLabels.review} ${counts.review}`" name="review" />
    <el-tab-pane :label="`${tabLabels.unmatched} ${counts.unmatched}`" name="unmatched" />
    <el-tab-pane :label="`${tabLabels.ignored} ${counts.ignored}`" name="ignored" />
  </el-tabs>

  <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="gap" />

  <section class="card">
    <div class="notice">
      <div class="notice-text">
        <div class="notice-title">{{ reviewNotice.title }}</div>
        <div class="notice-sub">{{ reviewNotice.text }}</div>
      </div>
      <div class="notice-actions">
        <el-button
          v-if="tab === 'auto'"
          type="success"
          :loading="bulkBusy"
          :disabled="!counts.auto || !summary?.write_enabled"
          @click="applyAllAuto"
        >
          全部采用
        </el-button>
        <el-button
          v-else-if="tab === 'review'"
          :loading="bulkBusy"
          :disabled="!pageFillCount || !summary?.write_enabled"
          title="只写补空与目录纠正；冲突请逐条确认"
          @click="applyPageFills"
        >
          写入本页可补
        </el-button>
      </div>
    </div>

    <div class="filter-row">
      <div class="filter-search">
        <el-input
          v-model="q"
          placeholder="搜索曲目、艺人或专辑"
          clearable
          @keyup.enter="applySearch"
          @clear="resetSearch"
        />
        <el-button @click="applySearch">搜索</el-button>
      </div>
      <span v-if="appliedQuery" class="dim">筛选：{{ appliedQuery }}</span>
    </div>

    <el-skeleton v-if="loading" animated :rows="6" class="skeleton" />

    <template v-else-if="items.length">
      <div v-for="group in grouped" :key="group.key" class="group">
        <div class="group-head">
          <span class="group-title ellipsis">{{ group.album }}</span>
          <span v-if="group.artist" class="group-artist ellipsis">{{ group.artist }}</span>
          <span class="group-count num">{{ group.items.length }} 首</span>
        </div>
        <TagProposalRow
          v-for="item in group.items"
          :key="item.id"
          :item="item"
          @applied="refreshAll"
          @ignored="refreshAll"
          @restored="refreshAll"
          @link="openLink"
          @candidate="openLink"
          @edit="openEditor"
        />
      </div>

      <div v-if="total > size" class="pager">
        <el-pagination
          v-model:current-page="page"
          :page-size="size"
          :total="total"
          layout="total, prev, pager, next"
          background
        />
      </div>
    </template>

    <EmptyState
      v-else
      :title="
        tab === 'auto'
          ? '没有可自动写入的标签'
          : tab === 'review'
            ? '没有待审查的标签'
            : tab === 'unmatched'
              ? '没有未匹配的曲目'
              : '还没有忽略任何条目'
      "
      :hint="
        tab === 'auto'
          ? '扫描后，来源与曲目一致且没有冲突的会出现在这里。'
          : tab === 'review'
            ? '冲突字段需要人工确认；没有冲突时会自动写入。'
            : tab === 'unmatched'
              ? '全部曲目都找到了可信的 MusicBrainz 条目。'
              : '在待审查或未匹配里点「忽略」，条目会移到这里，扫描不会再翻它回来。'
      "
      :icon="
        tab === 'auto'
          ? 'PriceTag'
          : tab === 'review'
            ? 'CircleCheck'
            : tab === 'unmatched'
              ? 'Search'
              : 'Hide'
      "
    />
  </section>

  <TagLinkDialog
    v-model="linkOpen"
    :item="linkTarget"
    :initial-url="linkSeed"
    @linked="afterLink"
  />

  <TrackFixDrawer
    v-model="drawerOpen"
    :root="drawerRoot"
    :path="drawerPath"
    tab="tags"
    @refresh="refreshAll"
  />

  <el-dialog
    v-model="historyOpen"
    title="标签写入记录"
    :width="narrow ? '94%' : '720px'"
    :top="narrow ? '4vh' : '10vh'"
  >
    <el-skeleton v-if="historyLoading" animated :rows="4" />
    <div v-else-if="history.length" class="history">
      <div v-for="row in history" :key="row.id" class="history-row">
        <div class="history-main">
          <div class="history-title ellipsis">{{ row.rel_path.split('/').pop() }}</div>
          <div class="history-sub dim ellipsis">
            {{ row.root }} · {{ fmtAgo(row.written_at) }} ·
            {{ Object.keys(row.applied).map((key) => tagFieldLabel(key)).join('、') }}
          </div>
        </div>
        <el-tag v-if="row.undone" size="small" type="info" effect="plain">已撤销</el-tag>
        <el-button v-else size="small" text @click="undo(row)">撤销</el-button>
      </div>
    </div>
    <EmptyState v-else title="还没有写入记录" hint="自动或手动写入标签后，记录会出现在这里。" />
    <template #footer>
      <el-button @click="historyOpen = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.scan-card {
  margin-bottom: 12px;
}
.scan-actions {
  gap: 14px;
}
.scan-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.scan-line {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
}
.scan-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--accent-strong);
}
.pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
  animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.45;
  }
}
.kpi-grid {
  margin-bottom: 4px;
}
.last-scan {
  font-size: 20px;
  font-weight: 600;
}
.tags-tabs {
  margin-top: 4px;
}
.tags-tabs :deep(.el-tabs__header) {
  margin-bottom: 10px;
}
.notice {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 13px 16px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--border);
  border-radius: var(--r-lg) var(--r-lg) 0 0;
}
.notice-text {
  min-width: 0;
}
.notice-title {
  font-size: 13px;
  font-weight: 600;
}
.notice-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-3);
}
.notice-actions {
  margin-left: auto;
  flex: none;
}
.filter-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.filter-search {
  display: flex;
  gap: 8px;
  flex: 1 1 320px;
  min-width: 220px;
  max-width: 520px;
}
.filter-search :deep(.el-input) {
  flex: 1;
}
.group {
  border-bottom: 1px solid var(--border);
}
.group:last-of-type {
  border-bottom: 0;
}
.group-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 9px 14px 7px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.group-title {
  font-size: 12.5px;
  font-weight: 600;
}
.group-artist {
  font-size: 12px;
  color: var(--text-3);
  min-width: 0;
}
.group-count {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--text-3);
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding: 12px 14px;
}
.skeleton {
  padding: 16px;
}
.history {
  display: flex;
  flex-direction: column;
}
.history-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 0;
  border-bottom: 1px solid var(--border);
}
.history-row:last-child {
  border-bottom: 0;
}
.history-main {
  min-width: 0;
  flex: 1;
}
.history-title {
  font-size: 13px;
}
.history-sub {
  margin-top: 2px;
  font-size: 11.5px;
}
@media (max-width: 760px) {
  .head-label {
    display: none;
  }
  .head-btn {
    padding: 0 9px;
  }
  .scan-card :deep(.card-head) {
    flex-wrap: wrap;
    align-items: flex-start;
  }
  .scan-card .card-hint {
    flex: 1 1 100%;
    order: 3;
  }
  .scan-card .card-title {
    white-space: nowrap;
  }
  .scan-actions {
    margin-left: 0;
    width: 100%;
    flex-wrap: wrap;
    gap: 10px;
  }
  .notice {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }
  .notice-actions {
    margin-left: 0;
    width: 100%;
  }
  .notice-actions :deep(.el-button) {
    width: 100%;
  }
  .filter-search {
    max-width: none;
    flex-basis: 100%;
  }
  .group-head {
    flex-wrap: wrap;
  }
}
</style>
