<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError, api } from '@/api/client'
import type {
  CleanupResult,
  DedupeGroup,
  DedupeResponse,
  DirNode,
  OrganizeDir,
  OrganizeMove,
  OrganizePlan,
  OrganizeResult,
} from '@/api/types'
import { fmtAgo, fmtBytes, fmtDuration, splitPath } from '@/utils/format'
import { useMeta } from '@/stores/meta'
import PageHead from '@/components/PageHead.vue'
import EmptyState from '@/components/EmptyState.vue'
import AppIcon from '@/components/AppIcon.vue'
import PreviewPlayButton from '@/components/PreviewPlayButton.vue'

const meta = useMeta()
const tab = ref<'organize' | 'duplicates' | 'cleanup'>('organize')
const root = ref('')
const subpaths = ref<string[]>([])
const excludedPaths = ref<string[]>([])
const dirs = ref<DirNode[]>([])
const loadingDirs = ref(false)

const roots = computed(() =>
  (meta.summary?.roots ?? []).map((item) => ({
    name: item.name,
    label: item.label || item.name,
    path: item.display_path || item.host_path || item.path,
  })),
)
const rootDirs = computed(() => dirs.value.filter((item) => item.path !== ''))

const plan = ref<OrganizePlan | null>(null)
const previewing = ref(false)
const executing = ref(false)
const result = ref<OrganizeResult | null>(null)
const selectedDirs = ref<string[]>([])

const dedupe = ref<DedupeResponse | null>(null)
const loadingDupes = ref(false)
const scanning = ref(false)
const scanProgress = ref('')
const selections = ref<Record<number, string[]>>({})
const deletingGroup = ref(0)
const kindFilter = ref<'' | 'exact' | 'recording' | 'similar'>('')
const bulkDeleting = ref(false)

const cleanupItems = ref<OrganizeDir[]>([])
const selectedCleanup = ref<string[]>([])
const cleanupPreviewing = ref(false)
const cleaning = ref(false)
const cleanupResult = ref<CleanupResult | null>(null)
const cleanupLoaded = ref(false)

const kindLabel: Record<string, string> = {
  exact: '音频完全一致',
  recording: '同一录音',
  similar: '标题/艺术家/时长相近',
}
const kindTip: Record<string, string> = {
  exact: '去掉标签后的音频字节完全相同，几乎可以确定是同一份。',
  recording: 'MusicBrainz 录音 ID 或 AcoustID 指纹指向同一录音，可能是不同压制或容器。',
  similar: '没有共同 ID，只按标题、艺人与时长推断；请务必试听确认。',
}
const statusLabel: Record<string, string> = {
  move: '待移动',
  chain: '按依赖移动',
  conflict: '冲突',
}
const kindOrder: ('exact' | 'recording' | 'similar')[] = ['exact', 'recording', 'similar']

const visibleGroups = computed(() =>
  (dedupe.value?.groups ?? []).filter(
    (group) => !kindFilter.value || group.kind === kindFilter.value,
  ),
)
const dismissedGroups = computed(() => dedupe.value?.dismissed ?? [])

const kindStats = computed(() => {
  const stats: Record<
    '' | 'exact' | 'recording' | 'similar',
    { groups: number; deletable: number; total: number }
  > = {
    '': { groups: 0, deletable: 0, total: 0 },
    exact: { groups: 0, deletable: 0, total: 0 },
    recording: { groups: 0, deletable: 0, total: 0 },
    similar: { groups: 0, deletable: 0, total: 0 },
  }
  for (const group of dedupe.value?.groups ?? []) {
    const deletable = group.items.filter(
      (item) => item.suggested_delete && !item.reference,
    ).length
    for (const key of ['', group.kind] as const) {
      stats[key].groups += 1
      stats[key].deletable += deletable
      stats[key].total += group.items.length
    }
  }
  return stats
})

const selectedEntries = computed(() => {
  const out: { group: DedupeGroup; item: DedupeGroup['items'][number] }[] = []
  // 统计与批量删除都按「全部组」算，不受上面的分类筛选影响：
  // 筛选只是看哪几组，勾选才是要处理什么。
  for (const group of dedupe.value?.groups ?? []) {
    const chosen = new Set(selections.value[group.id] ?? [])
    for (const item of group.items) {
      if (!chosen.has(item.rel_path)) continue
      // 人工可以改选应用原本建议保留的那一份；参照项永远不参与删除。
      if (item.reference) continue
      out.push({ group, item })
    }
  }
  return out
})
const selectedCount = computed(() => selectedEntries.value.length)
/** 组内被勾选、但应用原本建议保留的份数；用于在确认框里提示用户。 */
const overridingKeeps = computed(() =>
  selectedEntries.value.filter(
    (entry) => entry.item.rel_path === entry.group.keep_rel_path,
  ).length,
)
/** 会把整组删光的组：每组至少要留一份。 */
const groupsFullyPicked = computed(() =>
  (dedupe.value?.groups ?? []).filter((group) => {
    const chosen = itemsToDelete(group)
    return chosen.length > 0 && chosen.length >= group.items.length
  }),
)

const needsReclassify = computed(() =>
  (dedupe.value?.groups ?? []).some((group) =>
    group.items.some((item) => !('reference' in item)),
  ),
)

async function loadDirs() {
  if (!root.value) return
  loadingDirs.value = true
  try {
    const data = await api.libraryDirs(root.value)
    dirs.value = data.roots[0]?.dirs ?? []
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '目录加载失败')
  } finally {
    loadingDirs.value = false
  }
}

async function loadDupes() {
  loadingDupes.value = true
  try {
    dedupe.value = await api.dedupeGroups()
    // 「建议删除」的默认勾上：判断对了直接批量处理，判断错了把勾去掉。
    // 「建议保留」的默认不勾，但可以手动勾——判断错了要能改选删哪一份。
    selections.value = {}
    for (const group of dedupe.value.groups) {
      const paths = group.items
        .filter((item) => item.suggested_delete && !item.reference)
        .map((item) => item.rel_path)
      if (paths.length) selections.value[group.id] = paths
    }
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '重复检测加载失败')
  } finally {
    loadingDupes.value = false
  }
}

async function preview() {
  if (!root.value) {
    ElMessage.warning('先选择要整理的曲库目录')
    return
  }
  previewing.value = true
  result.value = null
  try {
    plan.value = await api.organizePreview({
      root: root.value,
      subpaths: subpaths.value,
      exclude: excludedPaths.value,
    })
    selectedDirs.value = plan.value.dirs.map((item) => item.rel_path)
    conflictDecisions.value = {}
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '预览失败')
  } finally {
    previewing.value = false
  }
}

/** 冲突的逐条处理：原相对路径 → 'replace'。缺省一律保留现有、跳过。 */
const conflictDecisions = ref<Record<string, 'skip' | 'replace'>>({})

const replaceCount = computed(
  () => Object.values(conflictDecisions.value).filter((v) => v === 'replace').length,
)

function decideConflict(rel: string, decision: 'skip' | 'replace') {
  conflictDecisions.value = { ...conflictDecisions.value, [rel]: decision }
}

function decideAllConflicts(decision: 'skip' | 'replace') {
  if (!plan.value) return
  const next: Record<string, 'skip' | 'replace'> = {}
  for (const item of plan.value.conflicts) {
    next[item.old_rel_path] = decision
  }
  conflictDecisions.value = next
}

/** 只有「目标已存在」的冲突才能替换：两个源映射到同一目标时没有可替换的对象。 */
function canReplace(item: OrganizeMove): boolean {
  return item.conflict_type === 'target_exists'
}

async function execute() {
  if (!plan.value) return
  const summary = plan.value.summary
  const replaceText = replaceCount.value
    ? ` ${replaceCount.value} 个冲突会用整理后的文件替换已有文件（被替换的文件进回收站，可还原）。`
    : ''
  try {
    await ElMessageBox.confirm(
      `将移动 ${summary.move} 个文件，并清理 ${selectedDirs.value.length} 个空目录或移动后变空的目录。` +
        `${summary.conflict ? ` ${summary.conflict} 个冲突会跳过` : ''}。` +
        replaceText +
        '移动不复制音频，移动后变空的旧目录会一并移除，所有操作都可撤销。',
      '确认整理',
      {
        confirmButtonText: '开始整理',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  } catch {
    return
  }
  executing.value = true
  try {
    result.value = await api.organizeExecute({
      plan_id: plan.value.plan_id,
      dirs: selectedDirs.value,
      resolutions: conflictDecisions.value,
    })
    if (result.value.ok) {
      const extra = result.value.skipped_conflicts
        ? `，${result.value.skipped_conflicts} 个冲突保留原样`
        : ''
      ElMessage.success(`已移动 ${result.value.moved} 个文件${extra}`)
    } else {
      ElMessage.warning(
        `已移动 ${result.value.moved} 个文件，${result.value.failed} 个失败：${
          result.value.errors[0] ?? ''
        }`,
      )
    }
    plan.value = null
    await loadDirs()
  } catch (err) {
    // 监控正在写文件时，人工请求会等一会儿再拿写租约；拿不到就返回 409。
    // 这不是失败——等监控让出后重试即可。
    if (err instanceof ApiError && err.status === 409) {
      ElMessage.warning('监控正在处理文件，稍后自动重试…')
      try {
        await new Promise((resolve) => setTimeout(resolve, 8000))
        const planId = plan.value?.plan_id
        if (!planId) throw new Error('预览已失效，请重新生成')
        result.value = await api.organizeExecute({
          plan_id: planId,
          dirs: selectedDirs.value,
          resolutions: conflictDecisions.value,
        })
      } catch (retryErr) {
        ElMessage.error(
          retryErr instanceof Error ? retryErr.message : '整理失败，请稍后重试',
        )
      }
    } else {
      ElMessage.error(err instanceof Error ? err.message : '整理失败')
    }
  } finally {
    executing.value = false
  }
}

function toggleAllDirs(check: boolean) {
  selectedDirs.value = check && plan.value ? plan.value.dirs.map((item) => item.rel_path) : []
}

async function scanDupes() {
  scanning.value = true
  scanProgress.value = '正在启动扫描…'
  try {
    const task = await api.startDedupeScan({})
    let record = task
    while (record.state === 'running') {
      await new Promise((resolve) => setTimeout(resolve, 1500))
      record = await api.task(task.id)
      scanProgress.value = record.progress || '正在计算音频哈希…'
    }
    if (record.state !== 'done') {
      throw new Error(record.error || '扫描被中断，请重新检查')
    }
    await loadDupes()
    ElMessage.success('重复检测完成，未删除任何文件')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '扫描失败')
  } finally {
    scanning.value = false
    scanProgress.value = ''
  }
}

function toggleItem(group: DedupeGroup, relPath: string, checked: boolean) {
  const item = group.items.find((entry) => entry.rel_path === relPath)
  // 建议保留的那一份也允许勾选：判断错了要能手动改。参照项不参与删除。
  if (!item || item.reference) return
  const current = new Set(selections.value[group.id] ?? [])
  if (checked) {
    // 勾上「建议保留」的那一份，等于说「判断错了，我要删这份」。
    // 同组其余的取消勾选，这组才留得住一份，不会变成整组删光。
    if (relPath === group.keep_rel_path) {
      current.clear()
    }
    current.add(relPath)
  } else {
    current.delete(relPath)
  }
  selections.value = { ...selections.value, [group.id]: [...current] }
}

function itemsToDelete(group: DedupeGroup) {
  return group.items.filter(
    (item) =>
      (selections.value[group.id] ?? []).includes(item.rel_path) &&
      !item.reference,
  )
}

function selectVisibleSuggested() {
  // 只补当前分类里没勾的，不动其它分类已经勾好的。
  const next: Record<number, string[]> = { ...selections.value }
  for (const group of visibleGroups.value) {
    const current = new Set(next[group.id] ?? [])
    for (const item of group.items) {
      if (item.suggested_delete && !item.reference) current.add(item.rel_path)
    }
    next[group.id] = [...current]
  }
  selections.value = next
}

function clearSelection() {
  selections.value = {}
}

function selectedKindSummary() {
  const counts: Record<string, number> = {}
  for (const entry of selectedEntries.value) {
    counts[entry.group.kind] = (counts[entry.group.kind] ?? 0) + 1
  }
  return Object.entries(counts)
    .map(([kind, count]) => `${kindLabel[kind] ?? kind} ${count} 个`)
    .join('，')
}

async function deleteSelected() {
  const entries = selectedEntries.value
  if (!entries.length) {
    ElMessage.warning('先勾选要移入回收站的副本')
    return
  }
  if (groupsFullyPicked.value.length) {
    ElMessage.warning(
      `有 ${groupsFullyPicked.value.length} 组会被整组删除，每组至少要保留一份`,
    )
    return
  }
  const summary = selectedKindSummary()
  const keepNote =
    overridingKeeps.value > 0
      ? `其中 ${overridingKeeps.value} 份原为系统建议保留，已被改选删除。\n`
      : ''
  try {
    await ElMessageBox.confirm(
      `将删除 ${entries.length} 个已勾选副本（${summary}）。\n` +
        keepNote +
        '其他分类的参照项不会被删除；删除后可在整理日志批量撤销。',
      '确认删除所选副本',
      {
        confirmButtonText: '移入回收站',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  bulkDeleting.value = true
  try {
    const outcome = await api.dedupeDeleteSelected(
      entries.map((entry) => ({
        group_id: entry.group.id,
        path: entry.item.rel_path,
      })),
    )
    ElMessage.success(`已移入回收站 ${outcome.deleted} 个副本`)
    await loadDupes()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  } finally {
    bulkDeleting.value = false
  }
}

async function deleteKind() {
  const kind = kindFilter.value
  if (!kind) return
  const stats = kindStats.value[kind]
  if (!stats.deletable) {
    ElMessage.info('这个分类没有建议删除的副本')
    return
  }
  try {
    await ElMessageBox.confirm(
      `只处理「${kindLabel[kind]}」分类的 ${stats.deletable} 个副本（${stats.groups} 组）。\n` +
        '属于其他分类的参照项不会被删除，也不会被选中；删除后可在整理日志批量撤销。',
      `一键删除「${kindLabel[kind]}」`,
      {
        confirmButtonText: '移入回收站',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  bulkDeleting.value = true
  try {
    const outcome = await api.dedupeDeleteKind(kind)
    ElMessage.success(`已移入回收站 ${outcome.deleted} 个副本`)
    await loadDupes()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  } finally {
    bulkDeleting.value = false
  }
}

async function removeSelected(group: DedupeGroup) {
  const chosen = itemsToDelete(group)
  if (!chosen.length) {
    ElMessage.warning('先勾选要移入回收站的副本')
    return
  }
  if (chosen.length >= group.items.length) {
    ElMessage.warning('至少要保留一份，不能整组删除')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将把 ${chosen.length} 个副本移入回收站，保留 ${group.items.length - chosen.length} 个。\n` +
        chosen.map((item) => item.rel_path).join('\n'),
      '确认删除重复副本',
      {
        confirmButtonText: '移入回收站',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  deletingGroup.value = group.id
  try {
    const outcome = await api.dedupeDelete(group.id, chosen.map((item) => item.rel_path))
    ElMessage.success(`已移入回收站 ${outcome.deleted} 个副本`)
    await loadDupes()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  } finally {
    deletingGroup.value = 0
  }
}

async function dismiss(group: DedupeGroup) {
  try {
    await api.dedupeDismiss(group.id)
    ElMessage.success('已忽略这组，之后的扫描不会再列出来')
    await loadDupes()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '操作失败')
  }
}

async function restore(group: DedupeGroup) {
  try {
    await api.dedupeRestore(group.id)
    ElMessage.success('已恢复，这组重新回到待处理列表')
    await loadDupes()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '操作失败')
  }
}

async function loadCleanup() {
  if (!root.value) {
    ElMessage.warning('先选择曲库目录')
    return
  }
  cleanupPreviewing.value = true
  cleanupLoaded.value = false
  try {
    const data = await api.organizeCleanupPreview({
      root: root.value,
      subpaths: subpaths.value,
      exclude: excludedPaths.value,
    })
    cleanupItems.value = data.items
    // 空目录默认清理；含非音频文件的目录也默认勾选，但界面上写明会进回收站。
    selectedCleanup.value = data.items.map((item) => item.rel_path)
    cleanupLoaded.value = true
    if (!data.items.length) ElMessage.success('没有发现无音频目录')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '检查失败')
  } finally {
    cleanupPreviewing.value = false
  }
}

async function runCleanup() {
  if (!selectedCleanup.value.length) {
    ElMessage.warning('没有勾选要清理的目录')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将清理 ${selectedCleanup.value.length} 个目录。空目录直接移除；含封面/歌词的目录会整体移入回收站。`,
      '确认清理目录',
      { confirmButtonText: '开始清理', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  cleaning.value = true
  try {
    cleanupResult.value = await api.organizeCleanup({
      root: root.value,
      subpaths: subpaths.value,
      exclude: excludedPaths.value,
      paths: selectedCleanup.value,
    })
    ElMessage.success(
      `已移除 ${cleanupResult.value.removed} 个空目录，${cleanupResult.value.trashed} 个目录移入回收站`,
    )
    cleanupItems.value = []
    cleanupLoaded.value = false
    selectedCleanup.value = []
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '清理失败')
  } finally {
    cleaning.value = false
  }
}

function toggleAllCleanup(check: boolean) {
  selectedCleanup.value = check ? cleanupItems.value.map((item) => item.rel_path) : []
}

watch(root, async () => {
  subpaths.value = []
  excludedPaths.value = []
  plan.value = null
  cleanupItems.value = []
  cleanupLoaded.value = false
  await loadDirs()
})

watch(kindFilter, () => {
  clearSelection()
})

watch([subpaths, excludedPaths], () => {
  plan.value = null
  result.value = null
  cleanupItems.value = []
  cleanupLoaded.value = false
})

onMounted(async () => {
  if (!meta.summary) await meta.refresh().catch(() => undefined)
  root.value = roots.value[0]?.name ?? ''
  await loadDirs()
  await loadDupes()
})
</script>

<template>
  <PageHead :count="tab === 'duplicates' ? dedupe?.duplicates : undefined">
    <el-button :loading="loadingDupes && tab === 'duplicates'" @click="loadDupes">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
    </el-button>
  </PageHead>

  <el-tabs v-model="tab" class="tabs">
    <el-tab-pane label="一键整理" name="organize" />
    <el-tab-pane label="重复音频" name="duplicates" />
    <el-tab-pane label="空目录" name="cleanup" />
  </el-tabs>

  <div class="scope-bar">
    <el-select v-model="root" class="root-sel" placeholder="曲库">
      <el-option
        v-for="item in roots"
        :key="item.name"
        :label="item.label"
        :value="item.name"
      >
        <div class="root-option">
          <span>{{ item.label }}</span>
          <span class="dim mono">{{ item.path }}</span>
        </div>
      </el-option>
    </el-select>
    <el-select
      v-model="subpaths"
      class="dir-sel"
      filterable
      multiple
      collapse-tags
      collapse-tags-tooltip
      clearable
      :max-collapse-tags="2"
      :loading="loadingDirs"
      placeholder="整个曲库（可多选目录）"
    >
      <el-option
        v-for="item in rootDirs"
        :key="item.path"
        :label="`${'　'.repeat(Math.max(0, item.depth - 1))}${item.name}（${item.audio} 首）`"
        :value="item.path"
      />
    </el-select>
    <span class="scope-label">排除</span>
    <el-select
      v-model="excludedPaths"
      class="exclude-sel"
      filterable
      multiple
      collapse-tags
      collapse-tags-tooltip
      clearable
      :max-collapse-tags="2"
      :loading="loadingDirs"
      placeholder="不整理这些目录"
    >
      <el-option
        v-for="item in rootDirs"
        :key="item.path"
        :label="`${'　'.repeat(Math.max(0, item.depth - 1))}${item.name}（${item.audio} 首）`"
        :value="item.path"
      />
    </el-select>
    <span class="cell-meta hint">
      {{ subpaths.length ? `已选 ${subpaths.length} 个目录` : '整个曲库' }}
      {{ excludedPaths.length ? ` · 排除 ${excludedPaths.length} 个` : '' }} · 不会在后台自动移动
    </span>
  </div>

  <template v-if="tab === 'organize'">
    <section class="card intro">
      <div class="intro-main">
        <div class="card-title">按标签生成 艺术家 / 专辑 / 音轨号 - 曲名</div>
        <p class="cell-meta">
          多艺人使用 <code>; </code> 连接，专辑目录优先用专辑艺人，合辑不会被拆散；
          缺专辑放进 <code>Unknown Album</code>，多碟自动加碟号前缀，缺音轨号直接用曲名。
        </p>
      </div>
      <el-button type="primary" :loading="previewing" @click="preview">
        <AppIcon name="View" :size="14" style="margin-right: 5px" />生成预览
      </el-button>
    </section>

    <el-alert v-if="result" :closable="false" show-icon :type="result.ok ? 'success' : 'warning'">
      <template #title>
        已移动 {{ result.moved }} 个文件，清理 {{ result.dirs_removed }} 个空目录 /
        {{ result.dirs_trashed }} 个无音频目录
        <template v-if="result.replaced">
          ，替换 {{ result.replaced }} 个冲突目标
        </template>
        <template v-if="result.skipped_conflicts">
          ，{{ result.skipped_conflicts }} 个冲突保留原样
        </template>
      </template>
      <div v-if="result.errors.length" class="cell-meta">
        {{ result.errors.slice(0, 3).join('；') }}
      </div>
    </el-alert>

    <template v-if="plan">
      <div class="summary-row">
        <span class="pill accent">待移动 {{ plan.summary.move }}</span>
        <span v-if="plan.summary.conflict" class="pill danger">冲突 {{ plan.summary.conflict }}</span>
        <span v-if="plan.summary.empty_dirs" class="pill">空目录 {{ plan.summary.empty_dirs }}</span>
        <span v-if="plan.summary.no_audio_dirs" class="pill">
          无音频目录 {{ plan.summary.no_audio_dirs }}
        </span>
        <span v-if="plan.summary.after_move_dirs" class="pill">
          移动后变空 {{ plan.summary.after_move_dirs }}
        </span>
        <span class="cell-meta">预览不会改动任何文件</span>
      </div>

      <section v-if="plan.conflicts.length" class="card">
        <div class="card-head">
          <span class="card-title">目标冲突</span>
          <span class="card-hint">逐条选择怎么处理，默认保留已有文件、跳过移动</span>
          <div class="card-actions">
            <el-button size="small" text @click="decideAllConflicts('skip')">
              全部保留
            </el-button>
            <el-button size="small" text @click="decideAllConflicts('replace')">
              全部替换
            </el-button>
          </div>
        </div>
        <div class="conflict-list">
          <div
            v-for="item in plan.conflicts"
            :key="item.old_rel_path"
            class="conflict-item"
            :class="{ replacing: conflictDecisions[item.old_rel_path] === 'replace' }"
          >
            <div class="conflict-main">
              <div class="conflict-paths">
                <span class="path ellipsis">{{ item.old_rel_path }}</span>
                <AppIcon name="Right" :size="12" />
                <span class="path ellipsis muted">{{ item.new_rel_path }}</span>
              </div>
              <div class="conflict-meta">
                <span class="cell-meta">{{ item.reason }}</span>
                <span v-if="canReplace(item)" class="cell-meta">
                  目标已有 {{ fmtBytes(item.target_info?.size) }}
                </span>
                <span v-else class="cell-meta">两条路径会撞在一起，只能跳过其中一条</span>
              </div>
            </div>
            <div class="conflict-actions">
              <el-radio-group
                :model-value="conflictDecisions[item.old_rel_path] ?? 'skip'"
                size="small"
                @update:model-value="
                  (v: string | number | boolean) =>
                    decideConflict(item.old_rel_path, v === 'replace' ? 'replace' : 'skip')
                "
              >
                <el-radio-button value="skip">保留已有</el-radio-button>
                <el-radio-button value="replace" :disabled="!canReplace(item)">
                  用它替换
                </el-radio-button>
              </el-radio-group>
            </div>
          </div>
        </div>
        <div class="cell-meta conflict-foot">
          「用它替换」会把目标位置的文件移进回收站（可还原），再把整理后的文件放过去。
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">新旧路径对照</span>
          <span class="card-hint">共 {{ plan.moves.length }} 条</span>
        </div>
        <div class="move-table">
          <div class="move-row head">
            <span>原路径</span>
            <span />
            <span>新路径</span>
            <span>状态</span>
          </div>
          <div v-for="item in plan.moves" :key="item.old_rel_path" class="move-row">
            <span class="path ellipsis" :title="item.old_rel_path">
              {{ item.old_rel_path }}
              <em v-if="item.has_sidecar" class="lrc">.lrc</em>
            </span>
            <AppIcon name="Right" :size="12" class="arrow" />
            <span class="path ellipsis" :title="item.new_rel_path">{{ item.new_rel_path }}</span>
            <el-tag
              size="small"
              :type="item.status === 'conflict' ? 'danger' : item.status === 'chain' ? 'warning' : 'success'"
            >
              {{ statusLabel[item.status] }}
            </el-tag>
          </div>
          <EmptyState
            v-if="!plan.moves.length"
            title="曲库已经符合结构"
            hint="没有需要移动的文件。"
            icon="FolderChecked"
          />
        </div>
      </section>

      <section v-if="plan.dirs.length" class="card">
        <div class="card-head">
          <span class="card-title">顺带清理无音频目录</span>
          <span class="card-hint">
            空目录与「移动后变空」直接移除；含封面/歌词的目录整目录移入回收站
          </span>
          <div class="card-actions">
            <el-button size="small" text @click="toggleAllDirs(true)">全选</el-button>
            <el-button size="small" text @click="toggleAllDirs(false)">清除选择</el-button>
          </div>
        </div>
        <el-checkbox-group v-model="selectedDirs" class="dir-grid">
          <el-checkbox v-for="dir in plan.dirs" :key="dir.rel_path" :value="dir.rel_path" class="dir-check">
            <span class="path">{{ dir.rel_path }}</span>
            <el-tag size="small" :type="dir.kind === 'empty' || dir.kind === 'after_move' ? 'info' : 'warning'">
              {{
                dir.kind === 'empty'
                  ? '空目录'
                  : dir.kind === 'after_move'
                    ? '移动后变空'
                    : `含 ${dir.files} 个文件`
              }}
            </el-tag>
          </el-checkbox>
        </el-checkbox-group>
      </section>

      <div class="execute-bar">
        <el-checkbox
          v-if="plan.dirs.length"
          :model-value="selectedDirs.length === plan.dirs.length"
          :indeterminate="selectedDirs.length > 0 && selectedDirs.length < plan.dirs.length"
          @change="toggleAllDirs(Boolean($event))"
        >
          同时清理无音频目录
        </el-checkbox>
        <el-button
          type="primary"
          size="large"
          :loading="executing"
          :disabled="!plan.summary.move && !selectedDirs.length"
          @click="execute"
        >
          执行 {{ plan.summary.move }} 项移动
        </el-button>
      </div>
    </template>

    <EmptyState
      v-else-if="!previewing"
      title="先生成预览"
      hint="所有移动都会先列出新旧路径，确认后才执行。"
      icon="Sort"
    />
  </template>

  <template v-else-if="tab === 'duplicates'">
    <section class="card intro">
      <div class="intro-main">
        <div class="card-title">后台每 24 小时检查一次，只列出来，不自动删除</div>
        <p class="cell-meta">
          默认不勾选任何项。可以逐项选择、全选当前分类，或按分类一键处理。
          删除会把副本移入回收站并记入整理日志，可批量撤销。
        </p>
        <p v-if="dedupe?.last_scan_at" class="cell-meta">
          上次检查 {{ fmtAgo(dedupe.last_scan_at) }} · 扫描
          {{ dedupe.last_scan?.scanned ?? 0 }} 个文件
        </p>
      </div>
      <el-button type="primary" :loading="scanning" @click="scanDupes">
        <AppIcon name="Search" :size="14" style="margin-right: 5px" />立即检查
      </el-button>
    </section>
    <el-alert v-if="scanning" :title="scanProgress" type="info" show-icon :closable="false" />
    <el-alert
      v-if="needsReclassify"
      type="warning"
      show-icon
      :closable="false"
      title="重复分类规则已更新，当前结果是旧版混合分类"
    >
      <template #default>
        旧版可能把精确副本、同一录音和相似录音混在一组。请先重新检测，
        否则无法按分类一键删除。
        <el-button size="small" type="warning" plain :loading="scanning" @click="scanDupes">
          立即检查
        </el-button>
      </template>
    </el-alert>

    <div class="dedupe-filter">
      <button
        v-for="option in [
          { value: '', label: '全部', stats: kindStats[''] },
          { value: 'exact', label: kindLabel.exact, stats: kindStats.exact },
          { value: 'recording', label: kindLabel.recording, stats: kindStats.recording },
          { value: 'similar', label: kindLabel.similar, stats: kindStats.similar },
        ]"
        :key="option.value"
        class="filter-chip"
        :class="{ on: kindFilter === option.value }"
        type="button"
        @click="kindFilter = option.value as '' | 'exact' | 'recording' | 'similar'"
      >
        {{ option.label }}
        <span class="chip-count">{{ option.stats.deletable }}</span>
      </button>
      <span class="cell-meta filter-note">
        分类规则：精确副本 → 同一录音 → 相似；参照项属于其他分类，只参与对比、不会被删除
      </span>
    </div>

    <div class="dedupe-bar">
      <span class="cell-meta">
        共 {{ dedupe?.groups.length ?? 0 }} 组<template v-if="kindFilter">
          · 当前显示 {{ visibleGroups.length }} 组</template> · 已选 {{ selectedCount }} 个
        <template v-if="kindFilter">
          · 本分类建议删除 {{ kindStats[kindFilter].deletable }} 个
        </template>
      </span>
      <div class="dedupe-actions">
        <el-button size="small" @click="selectVisibleSuggested">全选当前分类</el-button>
        <el-button size="small" text :disabled="!selectedCount" @click="clearSelection">清除选择</el-button>
        <el-button
          v-if="kindFilter"
          size="small"
          type="danger"
          plain
          :loading="bulkDeleting"
          :disabled="!kindStats[kindFilter].deletable || needsReclassify"
          @click="deleteKind"
        >
          一键删除此分类（{{ kindStats[kindFilter].deletable }}）
        </el-button>
        <el-button
          size="small"
          type="danger"
          :loading="bulkDeleting"
          :disabled="!selectedCount || needsReclassify"
          @click="deleteSelected"
        >
          批量删除所选（{{ selectedCount }}）
        </el-button>
      </div>
    </div>

    <el-skeleton v-if="loadingDupes && !dedupe" animated :rows="8" />
    <template v-else>
      <section v-for="group in visibleGroups" :key="group.id" class="card group">
        <div class="card-head">
          <el-tag size="small" :type="group.kind === 'exact' ? 'danger' : group.kind === 'recording' ? 'warning' : 'info'">
            {{ kindLabel[group.kind] }}
          </el-tag>
          <span class="card-hint">{{ kindTip[group.kind] }}</span>
          <div class="card-actions">
            <el-button size="small" text @click="dismiss(group)">忽略这组</el-button>
            <el-button
              size="small"
              type="danger"
              plain
              :loading="deletingGroup === group.id"
              :disabled="needsReclassify"
              @click="removeSelected(group)"
            >
              移入回收站（{{ itemsToDelete(group).length }}）
            </el-button>
          </div>
        </div>
        <div class="dup-grid">
          <div
            v-for="item in group.items"
            :key="item.rel_path"
            class="dup-item"
            :class="{ keep: item.rel_path === group.keep_rel_path }"
          >
            <div class="dup-head">
              <el-checkbox
                v-if="!item.reference"
                :model-value="(selections[group.id] ?? []).includes(item.rel_path)"
                @change="toggleItem(group, item.rel_path, Boolean($event))"
              >
                '删除此份'
              </el-checkbox>
              <el-tag v-if="item.reference" size="small" type="info">其他分类保留</el-tag>
              <el-tag
                v-else-if="
                  item.rel_path === group.keep_rel_path &&
                  !(selections[group.id] ?? []).includes(item.rel_path)
                "
                size="small"
                type="success"
              >
                建议保留
              </el-tag>
              <el-tag
                v-else-if="
                  item.rel_path === group.keep_rel_path &&
                  (selections[group.id] ?? []).includes(item.rel_path)
                "
                size="small"
                type="warning"
              >
                已改选删除
              </el-tag>
              <el-tag
                v-else-if="
                  item.suggested_delete &&
                  (selections[group.id] ?? []).includes(item.rel_path)
                "
                size="small"
                type="danger"
                effect="plain"
              >
                建议删除
              </el-tag>
              <el-tag v-else-if="item.suggested_delete" size="small" type="info" effect="plain">
                已改选保留
              </el-tag>
              <span class="cell-meta">{{ item.format.toUpperCase() }}</span>
              <span class="cell-meta num">{{ fmtDuration(item.duration) }}</span>
              <span class="cell-meta num">{{ fmtBytes(item.size) }}</span>
              <PreviewPlayButton :root="group.root" :path="item.rel_path" />
            </div>
            <div class="dup-title">{{ item.title || splitPath(item.rel_path).name }}</div>
            <div class="cell-meta">{{ item.artist || '—' }} · {{ item.album || '—' }}</div>
            <div class="path dup-path" :title="item.rel_path">{{ item.rel_path }}</div>
            <div class="dup-meta">
              <span v-if="item.has_lrc" class="cell-meta">有歌词</span>
              <span v-if="item.recording_id" class="cell-meta mono">
                {{ item.recording_id.slice(0, 8) }}
              </span>
              <span
                v-if="item.rel_path === group.keep_rel_path && group.keep_reason"
                class="cell-meta keep-reason"
              >
                保留理由：{{ group.keep_reason }}
              </span>
            </div>
          </div>
        </div>
      </section>

      <section v-if="dismissedGroups.length" class="card group">
        <div class="card-head">
          <el-tag size="small" type="info">已忽略</el-tag>
          <span class="card-hint">
            这些组被忽略过，之后的扫描不会再列出来。组里的文件有变化时会重新判断。
          </span>
        </div>
        <div class="dup-grid">
          <div v-for="group in dismissedGroups" :key="group.id" class="dup-item">
            <div class="dup-head">
              <el-tag
                size="small"
                :type="group.kind === 'exact' ? 'danger' : group.kind === 'recording' ? 'warning' : 'info'"
              >
                {{ kindLabel[group.kind] }}
              </el-tag>
              <span class="cell-meta">{{ group.items.length }} 份</span>
              <el-button size="small" text type="primary" @click="restore(group)">
                恢复
              </el-button>
            </div>
            <div class="dup-title">
              {{ group.items[0]?.title || group.items[0]?.rel_path || '—' }}
            </div>
            <div class="path dup-path">
              {{ group.items.map((item) => item.rel_path).join('、') }}
            </div>
          </div>
        </div>
      </section>

      <EmptyState
        v-if="!visibleGroups.length"
        :title="kindFilter ? `没有「${kindLabel[kindFilter]}」分类` : '没有发现重复音频'"
        :hint="kindFilter ? '切换其他分类，或点「立即检查」重新分析。' : '后台会定期检查；也可以点「立即检查」立即执行一次。'"
        icon="CopyDocument"
      />
    </template>
  </template>

  <template v-else>
    <section class="card intro">
      <div class="intro-main">
        <div class="card-title">递归确认没有音频的目录</div>
        <p class="cell-meta">
          真正的空目录直接移除；只剩封面、歌词或说明文件的目录会整体移入回收站，
          不会静默丢掉内容。所有记录都能在整理日志里撤销。
        </p>
      </div>
      <el-button type="primary" :loading="cleanupPreviewing" @click="loadCleanup">
        <AppIcon name="Search" :size="14" style="margin-right: 5px" />检查目录
      </el-button>
    </section>

    <el-alert
      v-if="cleanupResult"
      :closable="false"
      show-icon
      :type="cleanupResult.ok ? 'success' : 'warning'"
      :title="`已移除 ${cleanupResult.removed} 个空目录，${cleanupResult.trashed} 个目录移入回收站`"
    />

    <template v-if="cleanupLoaded">
      <div class="summary-row">
        <span class="pill">共 {{ cleanupItems.length }} 个无音频目录</span>
        <el-button size="small" text @click="toggleAllCleanup(true)">全选</el-button>
        <el-button size="small" text @click="toggleAllCleanup(false)">清除选择</el-button>
      </div>
      <section v-if="cleanupItems.length" class="card">
        <el-checkbox-group v-model="selectedCleanup" class="dir-grid">
          <el-checkbox v-for="item in cleanupItems" :key="item.rel_path" :value="item.rel_path" class="dir-check">
            <span class="path">{{ item.rel_path }}</span>
            <el-tag size="small" :type="item.kind === 'empty' ? 'info' : 'warning'">
              {{ item.kind === 'empty' ? '空目录' : `含 ${item.files} 个文件` }}
            </el-tag>
          </el-checkbox>
        </el-checkbox-group>
      </section>
      <EmptyState v-else title="没有需要清理的目录" hint="所有目录里都还有音频。" icon="FolderChecked" />
      <div v-if="cleanupItems.length" class="execute-bar">
        <span class="cell-meta">已选 {{ selectedCleanup.length }} 项，执行前还会再确认一次</span>
        <el-button type="primary" :loading="cleaning" @click="runCleanup">
          清理所选目录
        </el-button>
      </div>
    </template>
  </template>
</template>

<style scoped>
.tabs {
  margin-bottom: 2px;
}

.scope-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.root-sel {
  width: 140px;
}

.dir-sel {
  width: min(340px, 100%);
}

.exclude-sel {
  width: min(280px, 100%);
}

.scope-label {
  font-size: 12px;
  color: var(--text-3);
}

.hint {
  margin-left: auto;
}

.intro {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 16px 18px;
  margin-bottom: 14px;
}

.intro-main {
  flex: 1;
  min-width: 0;
}

.intro p {
  margin: 6px 0 0;
  line-height: 1.6;
}

.intro code {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
}

.summary-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 0 12px;
}

.pill {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text-2);
}

.pill.accent {
  color: var(--accent);
  border-color: var(--accent-line);
  background: var(--accent-soft);
}

.pill.danger {
  color: var(--danger);
  border-color: var(--danger);
  background: var(--danger-soft);
}

.conflict-list {
  max-height: 240px;
  overflow: auto;
}

.conflict-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}

.conflict-item.replacing {
  background: var(--surface-2, rgba(120, 120, 120, 0.08));
}

.conflict-main {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}

.conflict-paths {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.conflict-paths .path {
  min-width: 0;
}

.conflict-meta {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.conflict-actions {
  flex: none;
}

.conflict-foot {
  padding: 8px 16px 12px;
}

.muted {
  color: var(--text-3);
}

.move-table {
  max-height: 560px;
  overflow: auto;
}

.move-row {
  display: grid;
  grid-template-columns: 1fr 18px 1fr 92px;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}

.move-row.head {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--surface-2);
  color: var(--text-3);
  font-weight: 600;
}

.arrow {
  color: var(--text-3);
}

.move-row .path {
  min-width: 0;
}

.lrc {
  font-style: normal;
  color: var(--ok);
  margin-left: 4px;
}

.dir-grid {
  display: grid;
  grid-template-columns: 1fr;
}

.dir-check {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 16px;
  margin-right: 0;
  border-bottom: 1px solid var(--border);
  height: auto;
}

.dir-check :deep(.el-checkbox__label) {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
}

.dir-check .path {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.execute-bar {
  position: sticky;
  bottom: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  margin-top: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow-2);
}

.dedupe-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 0 10px;
}

.filter-chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 11px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text-2);
  font-size: 12px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}

.filter-chip:hover {
  color: var(--text);
  border-color: var(--border-strong);
}

.filter-chip.on {
  color: var(--accent);
  border-color: var(--accent-line);
  background: var(--accent-soft);
}

.chip-count {
  min-width: 20px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--surface);
  border: 1px solid var(--border);
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.filter-note {
  margin-left: auto;
}

.dedupe-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 8px 12px;
  margin-bottom: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.dedupe-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.group .card-head {
  flex-wrap: wrap;
}

.dup-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
  gap: 10px;
  padding: 12px;
}

.dup-item {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
  background: var(--surface-2);
  min-width: 0;
}

.dup-item.keep {
  border-color: var(--ok);
  background: var(--ok-soft);
}

.dup-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.dup-title {
  font-weight: 600;
  margin: 8px 0 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dup-path {
  font-size: 11px;
  margin-top: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dup-meta {
  display: flex;
  gap: 10px;
  margin-top: 4px;
  flex-wrap: wrap;
}

.keep-reason {
  color: var(--ok);
}

@media (max-width: 720px) {
  .hint,
  .filter-note {
    display: none;
  }

  /* 选择区竖排等宽：三个控件横排时宽度各不相同（140/340/280），
     右边缘参差，"排除"标签还会挤在中间。竖排后每个控件都占满整行。 */
  .scope-bar {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }

  .scope-bar .root-sel,
  .scope-bar .dir-sel,
  .scope-bar .exclude-sel {
    width: 100%;
  }

  .scope-label {
    order: 3;
    margin-top: 2px;
  }

  .scope-bar .exclude-sel {
    order: 4;
  }

  /* 执行条不再悬浮：sticky 底部会让按钮压住列表内容，
     移动端尤其明显（列表长、浏览器地址栏还占一截）。 */
  .execute-bar {
    position: static;
    bottom: auto;
  }

  .dedupe-bar,
  .dedupe-actions {
    align-items: stretch;
  }

  .dedupe-actions {
    width: 100%;
  }

  .move-row {
    grid-template-columns: 1fr;
    gap: 4px;
    padding: 10px 12px;
  }

  /* 路径是判断依据：移动端换行显示完整，不截断。
     截断后看不出「目标是谁」，冲突就无从处理。 */
  .move-row .path,
  .conflict-paths .path {
    white-space: normal;
    overflow: visible;
    text-overflow: clip;
    word-break: break-all;
    line-height: 1.45;
  }

  .conflict-paths {
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
  }

  .conflict-paths .arrow {
    display: none;
  }

  .move-row.head {
    display: none;
  }

  .arrow {
    display: none;
  }

  .conflict-item {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }

  .conflict-actions :deep(.el-radio-group) {
    width: 100%;
    display: flex;
  }

  .conflict-actions :deep(.el-radio-button) {
    flex: 1;
  }

  .conflict-actions :deep(.el-radio-button__inner) {
    width: 100%;
  }

  .execute-bar {
    flex-direction: column;
    align-items: stretch;
  }
}

.root-option {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}

.root-option .mono {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
