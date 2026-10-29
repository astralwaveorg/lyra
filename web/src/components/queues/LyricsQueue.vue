<script setup lang="ts">
/**
 * 「待办 · 歌词」面板：自动抓取产生的歌词候选队列。
 *
 * 可嵌入待办页作为队列组件；独立页面形态已并入待办页。
 * 深链兼容：?tab= 与父组件传入的 initialTab 都能定位初始标签。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type {
  LyricsPreview,
  ReviewPage,
  Rule,
  SafeItem,
  StagedCandidate,
  StagedGroup,
} from '@/api/types'
import { useMeta } from '@/stores/meta'
import { useNarrow } from '@/composables/useMedia'
import { fmtAgo, fmtScore, splitPath } from '@/utils/format'
import ProviderBadge from '@/components/ProviderBadge.vue'
import EmptyState from '@/components/EmptyState.vue'
import AlternativesDrawer from '@/components/AlternativesDrawer.vue'
import OrphansPanel from '@/components/OrphansPanel.vue'
import LyricsViewer from '@/components/LyricsViewer.vue'
import ScrapeDialog from '@/components/ScrapeDialog.vue'
import PreviewPlayButton from '@/components/PreviewPlayButton.vue'
import AppIcon from '@/components/AppIcon.vue'

type Tab = 'safe' | 'pending' | 'ignored' | 'orphans'

/** 父组件可指定初始标签；仅作初始值，之后仍以 ?tab= 深链为最高优先。 */
const props = defineProps<{ initialTab?: Tab }>()

const meta = useMeta()
const narrow = useNarrow()
const route = useRoute()
const router = useRouter()

/**
 * 四个标签按「该先看什么」排序：
 *   推荐采纳 —— 来源与曲目一致，只差分数，确认即可落盘；
 *   待审查   —— 需要逐首判断的候选；
 *   已忽略   —— 被人工忽略的曲目（path 级 skip_fetch 规则），可恢复；
 *   无音源   —— 没有对应音频的歌词（原「独立LRC」）。
 */
function asTab(value: string | undefined): Tab | null {
  return value === 'pending' || value === 'orphans' || value === 'safe' || value === 'ignored'
    ? value
    : null
}

/**
 * 初始标签：深链 > 父组件 prop > 默认「推荐采纳」。
 * query 键用 lyrics_tab 而不是 tab——本组件作为待办页的歌词面板嵌入，
 * 待办的类目（kind）与标签队列（TagsQueue）也各有自己的页签，
 * 共用一个 tab 键会互相污染（切到标签队列时把歌词的页签带给它）。
 */
const requested = asTab(typeof route.query.lyrics_tab === 'string' ? route.query.lyrics_tab : '') ?? asTab(props.initialTab)
const tab = ref<Tab>(requested ?? 'safe')

const data = ref<ReviewPage | null>(null)
const safeItems = ref<SafeItem[]>([])
const safe = ref(0)
const loading = ref(false)
const safeLoading = ref(false)
const error = ref('')
const page = ref(1)
/** 每页显示多少首。默认 20——复核时一屏能多看几首，少翻页。 */
const size = ref(20)
const sizeOptions = [20, 50, 100, 200]
const busyId = ref(0)
const batchBusy = ref(false)
const orphanCount = ref(0)
const orphansRef = ref<InstanceType<typeof OrphansPanel> | null>(null)

/**
 * 批量勾选。两个标签各自独立，key 都是曲目路径（rel_path）：
 * 勾住一首 = 采用这首歌的推荐候选，同一首的其他候选一并驳回。
 * 只属于当前列表——换页、刷新即清空，绝不把看不见的行带进批量。
 */
const checkedPaths = ref<Set<string>>(new Set())
const checkedSafePaths = ref<Set<string>>(new Set())
const pendingBatchBusy = ref(false)

/** 「已忽略」标签的数据：path 级 skip_fetch 规则。 */
const ruleItems = ref<Rule[]>([])
const restoreBusyId = ref(0)

const total = computed(() => data.value?.total ?? 0)

/**
 * 一首歌里「系统推荐」的那一条。
 *
 * 就是**分数最高**的那条（同分时按来源优先顺序，QQ 在前）——服务端已经
 * 排好序了。硬闸门（时长/版本/标题/艺人）有没有过，仍然逐条显示在界面上，
 * 由人决定采不采用。
 */
function recommendedId(group: StagedGroup): number {
  return group.candidates[0]?.id ?? group.best_id
}
const groups = computed<StagedGroup[]>(() => data.value?.groups ?? [])

/** 推荐采纳按曲目聚合：同一首若有多个安全候选，只展示最高分的那个。 */
const safeGroups = computed(() => {
  const map = new Map<
    string,
    { rel_path: string; root: string; best: SafeItem; items: SafeItem[] }
  >()
  for (const item of safeItems.value) {
    const group = map.get(item.rel_path)
    if (!group) {
      map.set(item.rel_path, {
        rel_path: item.rel_path,
        root: item.root,
        best: item,
        items: [item],
      })
      continue
    }
    group.items.push(item)
    if ((item.score ?? 0) > (group.best.score ?? 0)) group.best = item
  }
  return [...map.values()]
})

/** 推荐采纳按「首」计：列表按曲目聚合，同一首可能有多条安全候选。 */
const safeTrackCount = computed(() => safeGroups.value.length)

/** 歌词审查的忽略规则（目前接口里只有这一种规则来源）。 */
const ignoredRules = computed(() =>
  ruleItems.value.filter((rule) => rule.action === 'skip_fetch' && rule.scope === 'path'),
)

/* ------------------------------------------------------------------ 查看歌词 */

type ViewTarget = {
  id?: number
  path: string
  root: string
  provider: string
  provider_id: string
  score: number | null
  reason: string | null
  /** 系统推荐的那一条（按分数取最高），按钮用绿色区分 */
  recommended?: boolean
  /** 覆盖展示用的说明：推荐项不该显示「建议对比其他来源」那种判定文案。 */
  note?: string
}

const viewOpen = ref(false)
const viewLoading = ref(false)
const viewError = ref('')
const viewPreview = ref<LyricsPreview | null>(null)
const viewTarget = ref<ViewTarget | null>(null)
const viewAction = ref<(() => Promise<void>) | null>(null)

/**
 * 来源侧已下架/取不到的候选。
 *
 * 暂存记录里存的是「当时」的来源 id，上游可能把歌删了。这种情况只有真的去取
 * 歌词才知道，所以列表里仍然保留（它是那次匹配的唯一记录），但一旦确认取不到，
 * 就把「采用」禁掉并给一个「重新查询」，避免点了必然失败。
 */
const unavailable = ref<Set<number>>(new Set())

function markUnavailable(id?: number) {
  if (!id) return
  const next = new Set(unavailable.value)
  next.add(id)
  unavailable.value = next
}

function isGone(err: unknown): boolean {
  if (!(err instanceof Error)) return false
  return err.message.includes('找不到这个候选') || err.message.includes('已下架')
}

async function openPreview(target: ViewTarget, action: () => Promise<void>) {
  viewTarget.value = target
  viewAction.value = action
  viewOpen.value = true
  viewPreview.value = null
  viewError.value = ''
  viewLoading.value = true
  try {
    viewPreview.value = await api.lyricsPreview({
      root: target.root,
      path: target.path,
      provider: target.provider,
      provider_id: target.provider_id,
    })
  } catch (err) {
    viewError.value = err instanceof Error ? err.message : '取歌词失败'
    if (isGone(err)) markUnavailable(target.id)
  } finally {
    viewLoading.value = false
  }
}

/* ---------------------------------------------------- 手动选词 / 重新查询 */

/**
 * 候选都不合适时的出口：按链接指定一首歌，服务端重新取词并写入。
 *
 * 与「音乐」页的刮削对话框是同一个组件——候选列表 + 链接输入都在里面，
 * 所以这里只需要把 root/path 和这首歌的旧候选 id 传进去。
 */
const manualOpen = ref(false)
const manualTarget = ref<{ root: string; path: string } | null>(null)
/** 写入成功后这一首的旧候选就作废了，需要一并清出队列。 */
const manualIds = ref<number[]>([])

function idsFor(path: string, fallbackId?: number): number[] {
  const group = groups.value.find((item) => item.rel_path === path)
  if (group) return group.candidates.map((item) => item.id)
  const safeGroup = safeGroups.value.find((item) => item.rel_path === path)
  if (safeGroup) return safeGroup.items.map((item) => item.id)
  return fallbackId ? [fallbackId] : []
}

function openManual(target: ViewTarget) {
  manualIds.value = idsFor(target.path, target.id)
  manualTarget.value = { root: target.root, path: target.path }
  manualOpen.value = true
}

function openManualForGroup(group: StagedGroup) {
  openManual({
    path: group.rel_path,
    root: group.root,
    provider: '',
    provider_id: '',
    score: null,
    reason: null,
  })
}

async function afterManual() {
  await Promise.all(manualIds.value.map((id) => api.rejectStaged(id).catch(() => undefined)))
  viewOpen.value = false
  await refresh()
}

/* ------------------------------------------------------------------ 数据加载 */

async function load() {
  loading.value = true
  safeLoading.value = true
  error.value = ''
  // 勾选只属于当前这一页；列表一换就清空。
  checkedPaths.value = new Set()
  checkedSafePaths.value = new Set()
  try {
    const [review, safeList, ruleList] = await Promise.all([
      api.staged({
        state: 'needs_review',
        by_track: true,
        page: page.value,
        size: size.value,
        // 推荐项单独成一个标签，这里必须排除，否则同一首歌会出现两次
        exclude_safe: true,
      }),
      api.safeBatch().catch(() => ({ total: 0, items: [] as SafeItem[] })),
      api.listRules().catch(() => ({ total: 0, items: [] as Rule[] })),
    ])
    data.value = review
    safeItems.value = safeList.items
    safe.value = safeList.total
    ruleItems.value = ruleList.items
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
    safeLoading.value = false
  }
}

/** 改了每页条数：回到第一页，否则可能停在一个已经不存在的页码上。 */
function onSizeChange() {
  page.value = 1
  void load()
}

async function refresh() {
  await load()
  await meta.refresh().catch(() => undefined)
}

/** 首屏没有推荐项时直接落到「待审查」，别让人停在一个空标签上。 */
function chooseInitialTab() {
  if (requested) return
  if (!safe.value && tab.value === 'safe') tab.value = 'pending'
}

async function refreshActive() {
  if (tab.value === 'orphans') await orphansRef.value?.reload()
  else await refresh()
}

watch(tab, (value) => {
  void router.replace({ query: { ...route.query, lyrics_tab: value } })
})

/* ------------------------------------------------------------------ 操作 */

async function accept(group: StagedGroup, candidate: StagedCandidate) {
  busyId.value = candidate.id
  try {
    await api.acceptStaged(candidate.id)
    // 同一首歌的其他候选一并拒绝：否则它们会一直留在队列里。
    await Promise.all(
      group.candidates
        .filter((item) => item.id !== candidate.id)
        .map((item) => api.rejectStaged(item.id).catch(() => undefined)),
    )
    ElMessage.success('已采用，等待写入')
    viewOpen.value = false
    await refresh()
  } catch (err) {
    if (isGone(err)) markUnavailable(candidate.id)
    ElMessage.error(err instanceof Error ? err.message : '操作失败')
  } finally {
    busyId.value = 0
  }
}

async function acceptOne(group: { best: SafeItem; items: SafeItem[] }) {
  busyId.value = group.best.id
  try {
    await api.acceptStaged(group.best.id)
    await Promise.all(
      group.items
        .filter((item) => item.id !== group.best.id)
        .map((item) => api.rejectStaged(item.id).catch(() => undefined)),
    )
    ElMessage.success('已采用，等待写入')
    viewOpen.value = false
    await refresh()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '操作失败')
  } finally {
    busyId.value = 0
  }
}

async function ignoreGroup(group: StagedGroup) {
  try {
    await ElMessageBox.confirm(
      `忽略后不再自动为这首歌匹配歌词，它不会再回到待审查队列；如需恢复，可在「已忽略」页签中还原。`,
      '忽略这首歌',
      { confirmButtonText: '忽略', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  busyId.value = group.best_id
  try {
    await api.ignoreStagedBatch(group.candidates.map((item) => item.id))
    ElMessage.success('已忽略，之后的自动抓取会跳过它')
    await refresh()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '操作失败')
  } finally {
    busyId.value = 0
  }
}

async function acceptSafe() {
  try {
    await ElMessageBox.confirm(
      `这 ${safeTrackCount.value} 条的来源与曲目一致，匹配分数未达到自动采用标准。采用后会在下次写入时生效。`,
      '批量采用',
      { confirmButtonText: '全部采用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  batchBusy.value = true
  try {
    // 界面上是一首一行；「全部采用」也只采用这些行，而不是接口里的全部候选。
    // 同一首可能有多条安全候选（不同来源都匹配上了），多出来的会被驳回，
    // 否则「看到 8 条、采用 10 条」这种数字对不上会让人怀疑记错了。
    const chosen = safeGroups.value.map((group) => group.best.id)
    const chosenSet = new Set(chosen)
    const extras = safeItems.value.filter((item) => !chosenSet.has(item.id)).map((item) => item.id)
    const result = await api.acceptSafeBatch(chosen)
    await Promise.all(extras.map((id) => api.rejectStaged(id).catch(() => undefined)))
    ElMessage.success(`已采用 ${result.accepted} 项`)
    page.value = 1
    await refresh()
    if (!safeTrackCount.value) tab.value = 'pending'
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '批量采用失败')
  } finally {
    batchBusy.value = false
  }
}

const drawerOpen = ref(false)
const drawerId = ref<number | null>(null)
const drawerPath = ref('')

/* ------------------------------------------------------------ 批量勾选 */

/** 推荐候选已确认下架的曲目不能进批量：采用必然失败，让用户单条重新查询。 */
function trackSelectable(group: StagedGroup): boolean {
  return !unavailable.value.has(recommendedId(group))
}

const checkedGroups = computed(() =>
  groups.value.filter((group) => checkedPaths.value.has(group.rel_path)),
)
const checkedCount = computed(() => checkedGroups.value.length)
const pageSelectableCount = computed(() => groups.value.filter(trackSelectable).length)

function toggleTrack(group: StagedGroup, value: boolean) {
  const next = new Set(checkedPaths.value)
  if (value) next.add(group.rel_path)
  else next.delete(group.rel_path)
  checkedPaths.value = next
}

function selectPage() {
  const next = new Set(checkedPaths.value)
  for (const group of groups.value) {
    if (trackSelectable(group)) next.add(group.rel_path)
  }
  checkedPaths.value = next
}

function clearChecked() {
  checkedPaths.value = new Set()
}

async function acceptChecked() {
  const list = checkedGroups.value
  if (!list.length) return
  try {
    await ElMessageBox.confirm(
      `将采用所选 ${list.length} 首的推荐候选（每首分数最高的一条），同一首的其他候选会被驳回；采用后等待写入。`,
      '批量采用',
      { confirmButtonText: '批量采用', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  pendingBatchBusy.value = true
  try {
    const result = await api.batchAcceptStaged(list.map((group) => recommendedId(group)))
    ElMessage.success(`已采用 ${result.accepted} 首，等待写入`)
    if (result.missing) {
      ElMessage.warning(`${result.missing} 项已不在待审查队列里，本次未处理`)
    }
    page.value = 1
    await refresh()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '批量采用失败')
  } finally {
    pendingBatchBusy.value = false
  }
}

async function ignoreChecked() {
  const list = checkedGroups.value
  if (!list.length) return
  try {
    await ElMessageBox.confirm(
      `将忽略所选 ${list.length} 首：不再自动匹配歌词，也不会再出现在待审查队列；如需恢复，可在「已忽略」页签中还原。`,
      '批量忽略',
      { confirmButtonText: '忽略', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  pendingBatchBusy.value = true
  try {
    const result = await api.ignoreStagedBatch(list.map((group) => recommendedId(group)))
    ElMessage.success(`已忽略 ${result.ignored} 首`)
    page.value = 1
    await refresh()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '批量忽略失败')
  } finally {
    pendingBatchBusy.value = false
  }
}

/* ------------------------------------------------- 推荐采纳的勾选批量 */

const checkedSafeGroups = computed(() =>
  safeGroups.value.filter((group) => checkedSafePaths.value.has(group.rel_path)),
)
const checkedSafeCount = computed(() => checkedSafeGroups.value.length)

function toggleSafeTrack(path: string, value: boolean) {
  const next = new Set(checkedSafePaths.value)
  if (value) next.add(path)
  else next.delete(path)
  checkedSafePaths.value = next
}

function selectAllSafe() {
  checkedSafePaths.value = new Set(
    safeGroups.value
      .filter((group) => !unavailable.value.has(group.best.id))
      .map((group) => group.rel_path),
  )
}

function clearSafeChecked() {
  checkedSafePaths.value = new Set()
}

async function acceptCheckedSafe() {
  const list = checkedSafeGroups.value
  if (!list.length) return
  try {
    await ElMessageBox.confirm(
      `将采用所选 ${list.length} 条（来源与曲目一致，只是分数未达自动采用标准）；同一首的其他候选会被驳回。`,
      '批量采用',
      { confirmButtonText: '批量采用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  batchBusy.value = true
  try {
    const result = await api.batchAcceptStaged(list.map((group) => group.best.id))
    ElMessage.success(`已采用 ${result.accepted} 条，等待写入`)
    if (result.missing) {
      ElMessage.warning(`${result.missing} 项已不在待审查队列里，本次未处理`)
    }
    await refresh()
    if (!safeTrackCount.value) tab.value = 'pending'
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '批量采用失败')
  } finally {
    batchBusy.value = false
  }
}

async function ignoreCheckedSafe() {
  const list = checkedSafeGroups.value
  if (!list.length) return
  try {
    await ElMessageBox.confirm(
      `将忽略所选 ${list.length} 条对应的曲目：不再自动匹配歌词；如需恢复，可在「已忽略」页签中还原。`,
      '批量忽略',
      { confirmButtonText: '忽略', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  batchBusy.value = true
  try {
    const result = await api.ignoreStagedBatch(list.map((group) => group.best.id))
    ElMessage.success(`已忽略 ${result.ignored} 首`)
    await refresh()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '批量忽略失败')
  } finally {
    batchBusy.value = false
  }
}

/* ---------------------------------------------------------- 已忽略标签 */

async function restoreRule(rule: Rule) {
  restoreBusyId.value = rule.id
  try {
    await api.removeRule(rule.id)
    ElMessage.success('已恢复，下一轮抓取会重新匹配这首歌')
    ruleItems.value = ruleItems.value.filter((item) => item.id !== rule.id)
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '恢复失败')
  } finally {
    restoreBusyId.value = 0
  }
}

function compare(group: StagedGroup) {
  drawerId.value = group.best_id
  drawerPath.value = group.rel_path
  drawerOpen.value = true
}

onMounted(async () => {
  await load()
  chooseInitialTab()
})
</script>

<template>
  <div class="queue-bar">
    <span class="queue-hint">自动抓取的歌词候选在这里确认，采用后写入文件。</span>
    <el-button :loading="loading || safeLoading" @click="refreshActive">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
    </el-button>
  </div>

  <el-tabs v-model="tab" class="review-tabs">
    <el-tab-pane :label="safeTrackCount ? `推荐采纳 ${safeTrackCount}` : '推荐采纳'" name="safe">
      <div v-if="safe" class="notice">
        <div class="notice-text">
          <div class="notice-title">推荐采纳</div>
          <div class="notice-sub">
            共 {{ safeTrackCount }} 条，来源与曲目一致，匹配分数未达到自动采用标准。
          </div>
        </div>
        <div class="notice-actions">
          <el-button text :disabled="!safeTrackCount" @click="selectAllSafe">全选</el-button>
          <el-button text :disabled="!checkedSafeCount" @click="clearSafeChecked">
            清除选择
          </el-button>
          <el-button
            type="primary"
            :loading="batchBusy"
            :disabled="!checkedSafeCount"
            @click="acceptCheckedSafe"
          >
            采用所选{{ checkedSafeCount ? `（${checkedSafeCount}）` : '' }}
          </el-button>
          <el-button
            text
            type="warning"
            :loading="batchBusy"
            :disabled="!checkedSafeCount"
            @click="ignoreCheckedSafe"
          >
            忽略所选
          </el-button>
          <el-button type="primary" :loading="batchBusy" @click="acceptSafe">全部采用</el-button>
        </div>
      </div>

      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

      <el-skeleton v-if="safeLoading && !safeItems.length" animated :rows="6" />

      <div v-else-if="safeGroups.length" class="safe-list">
        <div v-for="group in safeGroups" :key="group.rel_path" class="safe-row">
          <el-checkbox
            class="track-check"
            :model-value="checkedSafePaths.has(group.rel_path)"
            :disabled="unavailable.has(group.best.id)"
            :title="unavailable.has(group.best.id) ? '来源已下架，请点「查看」后重新查询' : ''"
            @change="toggleSafeTrack(group.rel_path, Boolean($event))"
          />
          <div class="safe-main">
            <div class="name-line">
              <span class="path ellipsis">{{ splitPath(group.rel_path).name }}</span>
              <PreviewPlayButton :root="group.root" :path="group.rel_path" />
            </div>
            <span class="cell-meta ellipsis">{{ splitPath(group.rel_path).dir }}</span>
          </div>
          <div class="safe-source">
            <ProviderBadge :name="group.best.provider" />
            <span class="score num" :class="{ good: (group.best.score ?? 0) >= 0.8 }">
              {{ fmtScore(group.best.score) }}
            </span>
            <el-tag v-if="unavailable.has(group.best.id)" size="small" type="danger" effect="plain">
              来源已下架
            </el-tag>
            <span v-else-if="group.items.length > 1" class="cell-meta">+{{ group.items.length - 1 }}</span>
          </div>
          <div class="safe-actions">
            <el-button
              size="small"
              text
              @click="
                openPreview(
                  {
                    id: group.best.id,
                    path: group.rel_path,
                    root: group.root,
                    provider: group.best.provider,
                    provider_id: group.best.provider_id,
                    score: group.best.score,
                    reason: group.best.reason,
                    recommended: true,
                    note: '时长、版本、标题、艺人四项检查全部通过，只是总分未达自动采用标准。',
                  },
                  () => acceptOne(group),
                )
              "
            >
              查看
            </el-button>
            <el-button
              class="adopt-btn recommended"
              size="small"
              type="primary"
              plain
              :loading="busyId === group.best.id"
              :disabled="unavailable.has(group.best.id)"
              @click="acceptOne(group)"
            >
              采用
            </el-button>
          </div>
        </div>
      </div>

      <EmptyState
        v-else
        title="没有推荐采纳的项"
        hint="来源与曲目完全一致、只是分数略低的候选会出现在这里；其余请到「待审查」逐首确认。"
        icon="CircleCheck"
      />
    </el-tab-pane>

    <el-tab-pane :label="total ? `待审查 ${total}` : '待审查'" name="pending">
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

      <el-skeleton v-if="loading && !data" animated :rows="8" />

      <template v-else>
        <div v-if="groups.length" class="select-bar">
          <el-button size="small" text :disabled="!pageSelectableCount" @click="selectPage">
            全选本页
          </el-button>
          <el-button size="small" text :disabled="!checkedCount" @click="clearChecked">
            清除选择
          </el-button>
          <span class="select-tip">勾选一首即采用其推荐候选，同一首的其他候选会被驳回</span>
          <el-button
            size="small"
            text
            type="warning"
            :loading="pendingBatchBusy"
            :disabled="!checkedCount"
            @click="ignoreChecked"
          >
            批量忽略
          </el-button>
          <el-button
            size="small"
            type="primary"
            :loading="pendingBatchBusy"
            :disabled="!checkedCount"
            @click="acceptChecked"
          >
            批量采用{{ checkedCount ? `（${checkedCount}）` : '' }}
          </el-button>
        </div>

        <section v-for="group in groups" :key="group.rel_path" class="card group">
          <header class="group-head">
            <el-checkbox
              class="track-check"
              :model-value="checkedPaths.has(group.rel_path)"
              :disabled="!trackSelectable(group)"
              :title="trackSelectable(group) ? '' : '推荐候选的来源已下架，请点「查看」后重新查询'"
              @change="toggleTrack(group, Boolean($event))"
            />
            <div class="group-title">
              <div class="name-line">
                <span class="path ellipsis">{{ splitPath(group.rel_path).name }}</span>
                <PreviewPlayButton :root="group.root" :path="group.rel_path" />
              </div>
              <span class="cell-meta ellipsis">{{ splitPath(group.rel_path).dir }}</span>
            </div>
            <div class="group-actions">
              <el-button size="small" text @click="compare(group)">对比来源</el-button>
              <el-button size="small" text class="manual-btn" @click="openManualForGroup(group)">
                手动选词
              </el-button>
              <el-button
                size="small"
                text
                type="warning"
                :loading="busyId === group.best_id"
                @click="ignoreGroup(group)"
              >
                忽略
              </el-button>
            </div>
          </header>

          <div class="candidates">
            <div v-for="candidate in group.candidates" :key="candidate.id" class="candidate">
              <div class="cand-main">
                <div class="cand-line">
                  <ProviderBadge :name="candidate.provider" />
                  <span class="score num" :class="{ good: (candidate.score ?? 0) >= 0.8 }">
                    {{ fmtScore(candidate.score) }}
                  </span>
                  <el-tag v-if="unavailable.has(candidate.id)" size="small" type="danger" effect="plain">
                    来源已下架
                  </el-tag>
                  <el-tag v-else-if="candidate.safe" size="small" type="success">来源一致</el-tag>
                  <el-tag
                    v-if="candidate.ai && candidate.ai.best === candidate.provider_id"
                    size="small"
                    :type="candidate.ai.relation === 'same_recording' ? 'success' : 'warning'"
                    effect="plain"
                    :title="candidate.ai.reason"
                  >
                    AI 判定：{{ candidate.ai.relation === 'same_recording' ? '同一录音' : '同曲异版' }}
                    · {{ Math.round(candidate.ai.confidence * 100) }}%
                  </el-tag>
                  <span v-else-if="!candidate.has_evidence" class="cell-meta">无判定记录</span>
                </div>
                <div v-if="candidate.reason" class="reason">{{ candidate.reason }}</div>
                <div v-if="candidate.failed_checks.length" class="gates">
                  <span v-for="gate in candidate.failed_checks" :key="gate.key" class="gate">
                    {{ gate.label }} · {{ gate.detail }}
                  </span>
                </div>
              </div>
              <div class="cand-actions">
                <el-button
                  size="small"
                  text
                  @click="
                    openPreview(
                      {
                        id: candidate.id,
                        path: group.rel_path,
                        root: group.root,
                        provider: candidate.provider,
                        provider_id: candidate.provider_id,
                        score: candidate.score,
                        reason: candidate.reason,
                        recommended: candidate.id === recommendedId(group),
                      },
                      () => accept(group, candidate),
                    )
                  "
                >
                  查看
                </el-button>
                <el-button
                  class="adopt-btn"
                  :class="{ recommended: candidate.id === recommendedId(group) }"
                  size="small"
                  type="primary"
                  plain
                  :loading="busyId === candidate.id"
                  :disabled="unavailable.has(candidate.id)"
                  :title="
                    unavailable.has(candidate.id)
                      ? '来源已下架，请点「查看」后重新查询'
                      : candidate.id === recommendedId(group)
                        ? '系统推荐'
                        : ''
                  "
                  @click="accept(group, candidate)"
                >
                  采用
                </el-button>
              </div>
            </div>
          </div>
        </section>

        <EmptyState
          v-if="!groups.length"
          title="没有待审查的曲目"
          hint="自动匹配已经处理完当前曲库，之后新抓取的曲目会出现在这里。"
          icon="CircleCheck"
        />

        <div v-if="total > 0" class="pager">
          <el-pagination
            v-model:current-page="page"
            v-model:page-size="size"
            :page-sizes="sizeOptions"
            :total="total"
            layout="total, sizes, prev, pager, next"
            background
            @current-change="load"
            @size-change="onSizeChange"
          />
        </div>
      </template>
    </el-tab-pane>

    <el-tab-pane
      :label="ignoredRules.length ? `已忽略 ${ignoredRules.length}` : '已忽略'"
      name="ignored"
    >
      <div v-if="ignoredRules.length" class="safe-list">
        <div v-for="rule in ignoredRules" :key="rule.id" class="safe-row">
          <div class="safe-main">
            <div class="name-line">
              <span class="path ellipsis">{{ splitPath(rule.target).name }}</span>
            </div>
            <span class="cell-meta ellipsis">{{ splitPath(rule.target).dir }}</span>
          </div>
          <span class="cell-meta">忽略时间：{{ fmtAgo(rule.created_at) }}</span>
          <div class="safe-actions">
            <el-button
              size="small"
              text
              :loading="restoreBusyId === rule.id"
              @click="restoreRule(rule)"
            >
              恢复
            </el-button>
          </div>
        </div>
      </div>
      <EmptyState
        v-else
        title="还没有忽略的曲目"
        hint="点「忽略」的曲目会记在这里，之后的自动抓取会跳过它们；恢复后下一轮抓取会重新匹配。"
        icon="Hide"
      />
    </el-tab-pane>

    <el-tab-pane :label="orphanCount ? `无音源 ${orphanCount}` : '无音源'" name="orphans" lazy>
      <OrphansPanel ref="orphansRef" @count="orphanCount = $event" />
    </el-tab-pane>
  </el-tabs>

  <el-dialog
    v-model="viewOpen"
    :title="viewTarget ? splitPath(viewTarget.path).name : '查看歌词'"
    :width="narrow ? '94%' : '720px'"
    :top="narrow ? '3vh' : '8vh'"
  >
    <div v-if="viewTarget" class="view">
      <div class="view-meta">
        <ProviderBadge :name="viewTarget.provider" />
        <span class="cell-meta num">匹配度 {{ fmtScore(viewTarget.score) }}</span>
        <span v-if="viewPreview" class="cell-meta">
          {{ viewPreview.synced ? '有时间轴' : '无时间轴' }} · {{ viewPreview.line_count }} 行
        </span>
        <span v-if="viewPreview?.has_translation" class="cell-meta">含翻译</span>
      </div>
      <div v-if="viewTarget.note || viewTarget.reason" class="view-reason">
        {{ viewTarget.note || viewTarget.reason }}
      </div>
      <el-skeleton v-if="viewLoading" animated :rows="8" />
      <el-alert v-else-if="viewError" :title="viewError" type="warning" show-icon :closable="false" />
      <LyricsViewer v-else-if="viewPreview" :lines="viewPreview.lines" max-height="58vh" />
    </div>
    <template #footer>
      <el-button @click="viewOpen = false">关闭</el-button>
      <el-button
        v-if="viewTarget?.id && unavailable.has(viewTarget.id)"
        type="warning"
        plain
        @click="viewTarget && openManual(viewTarget)"
      >
        重新查询候选
      </el-button>
      <el-button
        v-else-if="viewTarget"
        class="manual-btn"
        @click="openManual(viewTarget)"
      >
        手动选词
      </el-button>
      <el-button
        class="adopt-btn"
        :class="{ recommended: viewTarget?.recommended }"
        type="primary"
        :loading="busyId !== 0"
        :disabled="
          viewLoading || !viewAction || (!!viewTarget?.id && unavailable.has(viewTarget.id))
        "
        @click="viewAction?.()"
      >
        采用这一条
      </el-button>
    </template>
  </el-dialog>

  <AlternativesDrawer
    v-model="drawerOpen"
    :staged-id="drawerId"
    :rel-path="drawerPath"
    @chosen="refresh"
  />

  <ScrapeDialog
    v-if="manualTarget"
    v-model="manualOpen"
    :root="manualTarget.root"
    :path="manualTarget.path"
    @done="afterManual"
  />
</template>

<style scoped>
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

.review-tabs :deep(.el-tabs__header) {
  margin-bottom: 14px;
}

.review-tabs :deep(.el-tabs__content) {
  overflow: visible;
}

.notice {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 16px;
  border: 1px solid var(--accent-line);
  background: var(--accent-soft);
  border-radius: var(--r-lg);
  margin-bottom: 12px;
}

.notice-text {
  flex: 1;
  min-width: 0;
}

.notice-title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
}

.notice-sub {
  font-size: 12.5px;
  color: var(--text-2);
}

.notice-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: none;
  flex-wrap: wrap;
}

.select-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 10px;
}

.select-bar .select-tip {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-3);
}

.track-check {
  flex: none;
  height: auto;
}

.name-line {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}

.name-line .path {
  min-width: 0;
}

.safe-list {
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  overflow: hidden;
}

.safe-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}

.safe-row:last-child {
  border-bottom: 0;
}

.safe-row:hover {
  background: var(--surface-2);
}

.safe-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.safe-source {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.safe-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: none;
}

.group {
  overflow: hidden;
}

.group-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.group-title {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.group-title .path {
  font-weight: 550;
}

.group-actions {
  display: flex;
  gap: 2px;
  flex: none;
}

.candidates {
  display: flex;
  flex-direction: column;
}

.candidate {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 11px 16px;
  border-bottom: 1px solid var(--border);
}

.candidate:last-child {
  border-bottom: 0;
}

.cand-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.cand-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.score {
  font-size: 12.5px;
  color: var(--text-3);
}

.score.good {
  color: var(--ok);
}

.reason {
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.6;
}

.gates {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
}

.gate {
  font-size: 11px;
  color: var(--warn);
}

.cand-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: none;
}

.view {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.view-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.view-reason {
  font-size: 12.5px;
  color: var(--text-2);
}

.adopt-btn.recommended:not(.is-disabled) {
  --el-button-text-color: var(--ok);
  --el-button-border-color: var(--ok);
  --el-button-hover-text-color: var(--text-invert);
  --el-button-hover-bg-color: var(--ok);
  --el-button-hover-border-color: var(--ok);
  --el-button-active-bg-color: var(--ok);
  --el-button-active-border-color: var(--ok);
}

.manual-btn.el-button {
  color: var(--accent-strong);
}

.pager {
  display: flex;
  justify-content: flex-end;
  padding-top: 12px;
}

@media (max-width: 720px) {
  .queue-bar {
    flex-wrap: wrap;
  }

  .notice {
    flex-direction: column;
    align-items: stretch;
  }

  .notice-actions {
    justify-content: flex-end;
  }

  .select-bar {
    flex-wrap: wrap;
  }

  .select-bar .select-tip {
    display: none;
  }

  .safe-row {
    flex-wrap: wrap;
    gap: 8px;
  }

  .safe-main {
    flex-basis: 100%;
  }

  .safe-actions {
    margin-left: auto;
  }

  .group-head {
    flex-wrap: wrap;
    gap: 6px;
  }

  .group-actions {
    flex-basis: 100%;
    justify-content: flex-end;
  }

  .candidate {
    flex-direction: column;
    gap: 8px;
  }

  .cand-actions {
    align-self: flex-end;
  }
}
</style>
