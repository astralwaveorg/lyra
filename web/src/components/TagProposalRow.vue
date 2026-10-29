<script setup lang="ts">
/**
 * 一条标签提案。
 *
 * 三件事必须一眼能分清：
 *   可补（来源有、本地空）——默认勾选，写入不覆盖任何东西；
 *   冲突（两边都有且不同）——默认勾选，取消后不覆盖；写入前完整预览；
 *   一致——只展示，不提供操作。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { TagFieldRow, TagProposal } from '@/api/types'
import { fmtAgo, fmtDuration, splitPath } from '@/utils/format'
import { tagFieldLabel } from '@/utils/labels'
import AppIcon from './AppIcon.vue'
import PreviewPlayButton from './PreviewPlayButton.vue'

const props = defineProps<{
  item: TagProposal
  busy?: boolean
  /** 批量勾选:状态由父组件持有,本行只上报勾选变化。 */
  checked?: boolean
  checkable?: boolean
}>()
const emit = defineEmits<{
  (event: 'applied', message: string): void
  (event: 'ignored'): void
  (event: 'restored'): void
  (event: 'link', item: TagProposal): void
  (event: 'candidate', item: TagProposal, url: string): void
  (event: 'edit', item: TagProposal): void
  (event: 'check', checked: boolean): void
}>()

const expanded = ref(false)
const selected = ref<Record<string, boolean>>({})
const working = ref(false)

const rows = computed<TagFieldRow[]>(() => props.item.rows ?? [])
const fills = computed(() => rows.value.filter((row) => row.action === 'fill'))
const corrections = computed(() => rows.value.filter((row) => row.action === 'correct'))
const conflicts = computed(() => rows.value.filter((row) => row.action === 'conflict'))
/** 可自动写的行：补空 + 按目录纠正。 */
const autoChanges = computed(() => [...fills.value, ...corrections.value])
const sameCount = computed(() => rows.value.filter((row) => row.action === 'same').length)
const selectedRows = computed(() =>
  rows.value.filter((row) => selected.value[row.key] && row.action !== 'same'),
)
const selectedCount = computed(() => selectedRows.value.length)
const selectableCount = computed(
  () => rows.value.filter((row) => row.action !== 'same').length,
)
const isAuto = computed(() => props.item.state === 'auto')
/** 忽略时间只放在悬浮提示里，表格行高不受影响。 */
const ignoredTitle = computed(() =>
  props.item.dismissed_at
    ? `忽略时间：${fmtAgo(props.item.dismissed_at)}`
    : '已忽略',
)
const name = computed(() => splitPath(props.item.rel_path).name)
const title = computed(() => props.item.track.title || props.item.title || name.value)
const artists = computed(() => (props.item.track.artists ?? []).join(' / '))
const writable = computed(() => props.item.writable !== false)

/** 非音乐名称字段：专辑、日期、MBID、厂牌、曲序等发行信息。 */
const NON_NAME_FIELDS = new Set([
  'ALBUM', 'DATE', 'ORIGINALDATE', 'ORIGINALYEAR',
  'MUSICBRAINZ_ALBUMID', 'MUSICBRAINZ_RELEASEGROUPID',
  'MUSICBRAINZ_RELEASETRACKID', 'MUSICBRAINZ_TRACKID',
  'MUSICBRAINZ_ALBUMARTISTID',
  'LABEL', 'CATALOGNUMBER', 'BARCODE', 'ASIN',
  'RELEASECOUNTRY', 'RELEASESTATUS', 'RELEASETYPE', 'MEDIA', 'SCRIPT',
  'TRACKNUMBER', 'DISCNUMBER', 'TOTALTRACKS', 'TRACKTOTAL',
  'TOTALDISCS', 'DISCTOTAL',
  'ALBUMARTIST', 'ALBUMARTISTS', 'ALBUMARTISTSORT',
])
const nonNameConflicts = computed(() =>
  conflicts.value.filter((row) => NON_NAME_FIELDS.has(row.key)),
)

/** 批量勾选只对「能按建议写」的行开放:可写、有可写字段、来源经得起批量。 */
const batchEligible = computed(
  () => writable.value && selectableCount.value > 0 && props.item.source !== 'search',
)
const checkTitle = computed(() => {
  if (!writable.value) return '只读格式，不支持写入'
  if (props.item.source === 'search') return '标题搜索来源请逐条确认'
  if (selectableCount.value === 0) return '没有可写的字段'
  return ''
})

const tone = computed(() => {
  switch (props.item.source) {
    case 'mbid':
      return 'success'
    case 'album':
      return 'primary'
    case 'fingerprint':
      return 'warning'
    case 'link':
      return 'success'
    default:
      return 'info'
  }
})

watch(
  () => [props.item.id, props.item.updated_at] as const,
  () => {
    const picks: Record<string, boolean> = {}
    for (const row of rows.value) {
      // 冲突也默认勾选；写入前有一次完整预览，确认后才覆盖。
      picks[row.key] = row.action !== 'same'
    }
    selected.value = picks
  },
  { immediate: true },
)

function selectAll() {
  const next: Record<string, boolean> = { ...selected.value }
  for (const row of rows.value) {
    if (row.action !== 'same') next[row.key] = true
  }
  selected.value = next
}

function clearAll() {
  const next: Record<string, boolean> = { ...selected.value }
  for (const row of rows.value) next[row.key] = false
  selected.value = next
}

function showValue(values: string[] | undefined): string {
  return (values ?? []).filter(Boolean).join(' / ') || '空'
}

function actionLabel(action: string): string {
  if (action === 'conflict') return '冲突'
  if (action === 'correct') return '纠正'
  return action === 'fill' ? '可补' : '一致'
}

function actionType(action: string): 'danger' | 'warning' | 'primary' | 'info' {
  if (action === 'conflict') return 'danger'
  if (action === 'correct') return 'warning'
  return action === 'fill' ? 'primary' : 'info'
}

function sourceText(): string {
  if (props.item.source === 'search') {
    return props.item.match?.verified ? '标题搜索 · 已核验' : '标题搜索'
  }
  return props.item.source_label || 'MusicBrainz'
}

function deltaText(): string {
  const delta = props.item.duration_delta
  if (delta === null || delta === undefined) return ''
  return delta < 1 ? '时长一致' : `时长差 ${Math.round(delta)} 秒`
}

async function confirmApply(keys: string[]): Promise<boolean> {
  const picked = rows.value.filter((row) => keys.includes(row.key))
  const preview = picked
    .slice(0, 4)
    .map((row) => `${tagFieldLabel(row.key)}：${showValue(row.suggested)}`)
    .join('；')
  const more = picked.length > 4 ? ` 等 ${picked.length} 项` : ''
  const conflictsInPicked = picked.filter((row) => row.action === 'conflict').length
  const correctionsInPicked = picked.filter((row) => row.action === 'correct').length
  const critical = picked.some((row) =>
    ['TITLE', 'ARTIST', 'ARTISTS', 'ALBUMARTIST', 'ALBUM'].includes(row.key),
  )
  const format = String(props.item.track.format || '').toLowerCase()
  const isMp3 = format === 'mp3'
  const isOgg = format === 'ogg_vorbis' || format === 'ogg_opus'
  const parts: string[] = []
  if (correctionsInPicked) {
    parts.push(`${correctionsInPicked} 个发行字段按同目录专辑统一`)
  }
  if (conflictsInPicked) {
    parts.push(`${conflictsInPicked} 个冲突字段会覆盖本地值`)
  }
  if (critical) parts.push('标题/艺人等关键字段会改变')
  if (isMp3) parts.push('MP3 会重写 ID3 标签，音频数据不变')
  if (isOgg) parts.push('OGG 会重排注释头部，音频数据不变')
  const risk = parts.length ? `其中${parts.join('，')}。` : '只补充缺失内容，不删除已有值。'
  try {
    await ElMessageBox.confirm(
      `将在「${title.value}」写入 ${picked.length} 个字段：${preview}${more}。${risk}`,
      '写入标签',
      {
        confirmButtonText: '写入',
        cancelButtonText: '取消',
        type: conflictsInPicked || critical ? 'warning' : 'info',
      },
    )
    return true
  } catch {
    return false
  }
}

async function apply(keys: string[]) {
  if (!keys.length) {
    ElMessage.info('先勾选要写入的字段')
    return
  }
  if (!writable.value) {
    ElMessage.warning('该格式暂不支持写入标签')
    return
  }
  if (!(await confirmApply(keys))) return
  working.value = true
  try {
    const result = await api.applyTagProposal(props.item.id, keys)
    const suffix = result.remaining
      ? `，还有 ${result.remaining} 项待处理`
      : ''
    ElMessage.success(`已写入 ${result.applied.length} 个字段${suffix}`)
    emit('applied', result.applied.join('、'))
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '写入失败')
  } finally {
    working.value = false
  }
}

async function applyRecommended() {
  await apply(autoChanges.value.map((row) => row.key))
}

/** 只采用发行信息，标题/艺人/ISRC/人员等音乐字段保持不变。 */
async function applyNonName() {
  const keys = [
    ...rows.value
      .filter((row) => row.action === 'fill' || row.action === 'correct')
      .map((row) => row.key),
    ...nonNameConflicts.value.map((row) => row.key),
  ]
  await apply([...new Set(keys)])
}

async function ignore() {
  working.value = true
  try {
    await api.ignoreTagProposal(props.item.id)
    ElMessage.success('已忽略，不会再出现在扫描结果中')
    emit('ignored')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '操作失败')
  } finally {
    working.value = false
  }
}

async function restore() {
  working.value = true
  try {
    await api.restoreTagProposal(props.item.id)
    ElMessage.success('已恢复，重新回到队列')
    emit('restored')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '操作失败')
  } finally {
    working.value = false
  }
}
</script>

<template>
  <div class="proposal" :class="{ open: expanded, auto: isAuto }">
    <div class="line">
      <div class="name-line">
        <span v-if="checkable" class="pick" :title="checkTitle || undefined">
          <el-checkbox
            :model-value="checked === true"
            :disabled="!batchEligible"
            @change="emit('check', Boolean($event))"
          />
        </span>
        <PreviewPlayButton :root="item.root" :path="item.rel_path" />
        <div class="names">
          <div class="title ellipsis" :title="title">{{ title }}</div>
          <div class="sub ellipsis">
            <span v-if="artists">{{ artists }}</span>
            <span v-if="artists && name" class="dot">·</span>
            <span class="mono">{{ name }}</span>
          </div>
        </div>
      </div>

      <div class="signals">
        <el-tag
          v-if="item.source && item.state !== 'unmatched'"
          size="small"
          :type="tone"
          effect="plain"
          class="source"
        >
          {{ sourceText() }}
        </el-tag>
        <span v-if="item.state === 'ignored'" class="chip muted" :title="ignoredTitle">已忽略</span>
        <span v-if="isAuto" class="conf num">{{ Math.round(item.confidence * 100) }}%</span>
        <span v-if="deltaText()" class="delta">{{ deltaText() }}</span>
        <span v-if="fills.length" class="chip fill num">可补 {{ fills.length }}</span>
        <span v-if="corrections.length" class="chip correct num">纠正 {{ corrections.length }}</span>
        <span v-if="conflicts.length" class="chip conflict num">冲突 {{ conflicts.length }}</span>
        <span v-if="sameCount" class="chip muted num">一致 {{ sameCount }}</span>
        <span v-if="!writable" class="chip muted">只读格式</span>
      </div>

      <div class="actions">
        <template v-if="item.state === 'ignored'">
          <el-button size="small" text :loading="working" @click="restore">恢复</el-button>
        </template>
        <template v-else-if="item.state === 'unmatched'">
          <el-button
            v-if="item.candidates.length"
            size="small"
            text
            @click="expanded = !expanded"
          >
            {{ expanded ? '收起候选' : '查看候选' }}
          </el-button>
          <el-button size="small" text @click="emit('link', item)">
            <AppIcon name="Link" :size="13" />链接指定
          </el-button>
          <el-button size="small" text @click="emit('edit', item)">
            <AppIcon name="Edit" :size="13" />手动编辑
          </el-button>
          <el-button size="small" text :loading="working" @click="ignore">忽略</el-button>
        </template>
        <template v-else>
          <el-button
            v-if="isAuto && fills.length"
            size="small"
            type="success"
            :loading="working"
            @click="applyRecommended"
          >
            采用
          </el-button>
          <el-button
            v-if="nonNameConflicts.length && item.source === 'search'"
            size="small"
            type="warning"
            plain
            :loading="working"
            @click="applyNonName"
          >
            采用采用专辑信息
          </el-button>
          <el-button size="small" :class="{ on: expanded }" @click="expanded = !expanded">
            {{ expanded ? '收起' : '查看对照' }}
          </el-button>
        </template>
      </div>
    </div>

    <div v-if="expanded && item.rows.length" class="details">
      <div class="field-head">
        <span />
        <span>字段</span>
        <span>本地</span>
        <span />
        <span>来源</span>
        <span>判断</span>
      </div>
      <div class="field-rows">
        <label
          v-for="row in item.rows"
          :key="row.key"
          class="field-row"
          :class="row.action"
        >
          <el-checkbox
            v-model="selected[row.key]"
            :disabled="row.action === 'same'"
            size="small"
          />
          <span class="field-name">{{ tagFieldLabel(row.key) }}</span>
          <span class="field-value ellipsis" :class="{ blank: !row.current.length }">
            {{ showValue(row.current) }}
          </span>
          <AppIcon name="ArrowRight" :size="11" class="arrow" />
          <span class="field-value ellipsis" :class="{ blank: !row.suggested.length }">
            {{ showValue(row.suggested) }}
          </span>
          <el-tag size="small" :type="actionType(row.action)" effect="plain">
            {{ actionLabel(row.action) }}
          </el-tag>
        </label>
      </div>

      <div v-if="corrections.length" class="correct-line">
        <AppIcon name="InfoFilled" :size="12" />
        纠正项按同目录专辑统一发行信息；内容不同时不会这样归类。
      </div>
      <div v-if="conflicts.length" class="warn-line">
        <AppIcon name="Warning" :size="12" />
        冲突字段默认勾选；取消勾选后不会覆盖本地值。
      </div>

      <div v-if="item.notes.length" class="notes">
        <span v-for="note in item.notes" :key="note" class="note">{{ note }}</span>
      </div>

      <div class="foot">
        <template v-if="selectableCount">
          <button type="button" class="select-all" @click="selectAll">
            全选（{{ selectableCount }}）
          </button>
          <button type="button" class="select-all" @click="clearAll">清除选择</button>
        </template>
        <span class="spacer" />
        <el-button
          v-if="nonNameConflicts.length"
          size="small"
          type="warning"
          plain
          :loading="working"
          @click="applyNonName"
        >
          采用专辑信息（{{ nonNameConflicts.length }}）
        </el-button>
        <el-button size="small" text @click="emit('link', item)">
          <AppIcon name="Link" :size="13" />链接指定条目
        </el-button>
        <el-button v-if="item.state === 'ignored'" size="small" text :loading="working" @click="restore">
          恢复
        </el-button>
        <el-button v-else size="small" text :loading="working" @click="ignore">忽略</el-button>
        <el-button
          size="small"
          type="primary"
          :disabled="!selectedCount || !writable"
          :loading="working"
          @click="apply(selectedRows.map((row) => row.key))"
        >
          写入所选{{ selectedCount ? `（${selectedCount}）` : '' }}
        </el-button>
      </div>
    </div>

    <div v-else-if="expanded && item.state === 'unmatched'" class="details">
      <div class="unmatched-note">
        没有找到可信的 MusicBrainz 条目。可以粘贴链接指定，或手动编辑标签。
      </div>
      <div v-if="item.candidates.length" class="candidates">
        <div class="candidate-title">搜索结果（需要人工确认）</div>
        <button
          v-for="candidate in item.candidates.slice(0, 3)"
          :key="candidate.provider_id"
          type="button"
          class="candidate"
          @click="
            emit(
              'candidate',
              item,
              `https://musicbrainz.org/recording/${candidate.provider_id}`,
            )
          "
        >
          <span class="ellipsis">{{ candidate.title }}</span>
          <span class="dim ellipsis">{{ candidate.album || candidate.artists.join(' / ') }}</span>
          <span class="dim num">{{ Math.round(candidate.score) }}</span>
          <span class="cand-action">对照</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.proposal {
  border-bottom: 1px solid var(--border);
}
.proposal:last-child {
  border-bottom: 0;
}
.proposal.open {
  background: var(--surface-2);
}
.line {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 14px;
  min-width: 0;
}
.name-line {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex: 1 1 320px;
}
.pick {
  display: flex;
  align-items: center;
  flex: none;
}
.names {
  min-width: 0;
}
.title {
  font-size: 13.5px;
  font-weight: 550;
  line-height: 1.35;
}
.sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-3);
}
.mono {
  font-family: var(--mono);
  font-size: 11.5px;
}
.dot {
  margin: 0 5px;
  opacity: 0.6;
}
.signals {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.source {
  flex: none;
}
.conf {
  font-size: 12px;
  color: var(--text-2);
}
.delta {
  font-size: 12px;
  color: var(--text-3);
}
.chip {
  font-size: 11.5px;
  padding: 1px 7px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--text-2);
  white-space: nowrap;
}
.chip.fill {
  color: var(--accent-strong);
  border-color: var(--accent-line);
  background: var(--accent-soft);
}
.chip.conflict {
  color: var(--danger);
  border-color: color-mix(in srgb, var(--danger) 40%, transparent);
  background: color-mix(in srgb, var(--danger) 9%, transparent);
}
.chip.correct {
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn) 40%, transparent);
  background: color-mix(in srgb, var(--warn) 9%, transparent);
}
.chip.muted {
  color: var(--text-3);
}
.actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.actions .on {
  color: var(--accent-strong);
}
.details {
  padding: 4px 14px 13px;
}
.field-head,
.field-row {
  display: grid;
  grid-template-columns: 20px 104px minmax(0, 1fr) 14px minmax(0, 1.1fr) 54px;
  align-items: center;
  gap: 8px;
}
.field-head {
  font-size: 11.5px;
  color: var(--text-3);
  padding: 4px 0 6px;
}
.field-rows {
  max-height: 330px;
  overflow: auto;
  border-top: 1px solid var(--border);
}
.field-row {
  padding: 5px 0;
  border-bottom: 1px solid var(--border);
  font-size: 12.5px;
}
.field-row.conflict {
  background: color-mix(in srgb, var(--danger) 5%, transparent);
}
.field-row.fill .field-name {
  color: var(--accent-strong);
}
.field-row.correct {
  background: color-mix(in srgb, var(--warn) 7%, transparent);
}
.field-row.correct .field-name {
  color: var(--warn);
}
.correct-line {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--warn);
}
.field-name {
  color: var(--text-3);
  font-size: 12px;
  white-space: nowrap;
}
.field-value {
  color: var(--text);
}
.field-value.blank {
  color: var(--text-3);
}
.arrow {
  color: var(--text-3);
}
.warn-line {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--danger);
}
.notes {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.note {
  font-size: 11.5px;
  color: var(--text-3);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 8px;
}
.foot {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
}
.spacer {
  flex: 1;
}
.select-all {
  border: 0;
  background: transparent;
  color: var(--accent-strong);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  padding: 0;
}
.unmatched-note {
  font-size: 12.5px;
  color: var(--text-2);
  padding: 6px 0;
}
.candidates {
  border-top: 1px solid var(--border);
  padding-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.candidate-title {
  font-size: 11.5px;
  color: var(--text-3);
}
.candidate {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr) 34px 46px;
  gap: 8px;
  font-size: 12px;
  align-items: center;
  width: 100%;
  text-align: left;
  border: 0;
  background: transparent;
  color: inherit;
  font-family: inherit;
  padding: 3px 0;
  cursor: pointer;
}
.candidate:hover {
  color: var(--accent-strong);
}
.cand-action {
  color: var(--accent-strong);
  text-align: right;
}
@media (max-width: 760px) {
  .line {
    flex-wrap: wrap;
    gap: 8px;
    padding: 11px 12px;
  }
  .name-line {
    flex-basis: 100%;
  }
  .signals {
    flex: 1 1 auto;
    justify-content: flex-start;
  }
  .actions {
    margin-left: auto;
  }
  .details {
    padding: 2px 12px 12px;
  }
  .field-head,
  .field-row {
    grid-template-columns: 20px 76px minmax(0, 1fr) 12px minmax(0, 1fr);
    gap: 6px;
  }
  .field-head span:last-child,
  .field-row :deep(.el-tag) {
    display: none;
  }
  .field-value {
    font-size: 12px;
  }
  .foot {
    flex-wrap: wrap;
  }
  .spacer {
    display: none;
  }
}
</style>
