<script setup lang="ts">
/**
 * 按 MusicBrainz 链接指定条目。
 *
 * 链接是使用者的明确判断，但同一首录音可能同时挂在单曲、原专辑、
 * 精选合辑上——所以**全部列出来让人选**，再由系统逐字段对照。
 * 选错发行会把专辑 MBID、发行组、曲序一起写错，比不填更糟。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { TagLinkCandidate, TagLinkResult, TagProposal } from '@/api/types'
import { useNarrow } from '@/composables/useMedia'
import { tagFieldLabel } from '@/utils/labels'
import AppIcon from './AppIcon.vue'

/** 窄屏收窄弹窗宽度，避免 760px 固定宽度在手机上横向溢出。 */
const narrow = useNarrow()

const props = defineProps<{
  modelValue: boolean
  item: TagProposal | null
  initialUrl?: string
}>()
const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
  (event: 'linked', item: TagProposal): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const url = ref('')
const loading = ref(false)
const error = ref('')
const result = ref<TagLinkResult | null>(null)
const chosen = ref(0)
const applying = ref(false)

const candidates = computed<TagLinkCandidate[]>(() => result.value?.candidates ?? [])
const current = computed<TagLinkCandidate | null>(
  () => candidates.value[chosen.value] ?? null,
)

watch(visible, (open) => {
  if (!open) return
  url.value = props.initialUrl ?? ''
  error.value = ''
  result.value = null
  chosen.value = 0
  if (url.value.trim()) void lookup()
})

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

function deltaText(candidate: TagLinkCandidate): string {
  if (candidate.duration_delta === null || candidate.duration_delta === undefined) return ''
  return candidate.duration_delta < 1
    ? '时长一致'
    : `时长差 ${Math.round(candidate.duration_delta)} 秒`
}

async function lookup() {
  const value = url.value.trim()
  if (!value || !props.item) return
  loading.value = true
  error.value = ''
  result.value = null
  try {
    result.value = await api.linkTagProposal(props.item.id, value)
    chosen.value = 0
  } catch (err) {
    error.value = err instanceof Error ? err.message : '查询失败'
  } finally {
    loading.value = false
  }
}

async function apply() {
  if (!props.item || !result.value) return
  const candidate = current.value
  if (!candidate) return
  applying.value = true
  try {
    const linked = await api.linkTagProposal(props.item.id, url.value.trim(), candidate.index)
    if (linked.proposal) {
      ElMessage.success('已切换到这个来源，请核对字段后写入')
      emit('linked', linked.proposal)
      visible.value = false
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '切换来源失败'
  } finally {
    applying.value = false
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="按链接指定条目"
    :width="narrow ? '94%' : '760px'"
    :fullscreen="false"
    class="tag-link-dialog"
    append-to-body
  >
    <div class="link-row">
      <el-input
        v-model="url"
        placeholder="粘贴 MusicBrainz 的发行、录音或发行组链接"
        clearable
        @keyup.enter="lookup"
      />
      <el-button type="primary" plain :loading="loading" :disabled="!url.trim()" @click="lookup">
        查询
      </el-button>
    </div>
    <div class="hint dim">
      支持 musicbrainz.org 的发行、录音、发行组与艺术家链接。查询只读，不会改文件。
    </div>

    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      show-icon
      class="gap"
    />

    <div v-if="candidates.length" class="candidates">
      <div class="section-title">选择这一条</div>
      <button
        v-for="candidate in candidates"
        :key="candidate.index"
        type="button"
        class="candidate"
        :class="{ on: chosen === candidate.index }"
        @click="chosen = candidate.index"
      >
        <span class="radio" :class="{ on: chosen === candidate.index }" />
        <span class="cand-main">
          <span class="cand-title ellipsis">{{ candidate.title }}</span>
          <span class="cand-sub ellipsis">
            {{ candidate.album || '没有专辑名' }}
            <template v-if="candidate.artists.length"> · {{ candidate.artists.join(' / ') }}</template>
          </span>
        </span>
        <span class="cand-meta">
          <span v-if="candidate.duration" class="num">{{ Math.round(candidate.duration) }} 秒</span>
          <span v-if="deltaText(candidate)" class="dim">{{ deltaText(candidate) }}</span>
          <span v-if="candidate.summary.fill" class="chip fill num">可补 {{ candidate.summary.fill }}</span>
          <span v-if="candidate.summary.conflict" class="chip conflict num">
            冲突 {{ candidate.summary.conflict }}
          </span>
        </span>
      </button>
    </div>

    <div v-else-if="result" class="empty-result dim">
      链接有效，但没有取到可用的标签字段。请更换一条条目后重试。
    </div>

    <div v-if="current" class="preview">
      <div class="section-title">逐字段对照</div>
      <div class="field-head">
        <span>字段</span><span>本地</span><span /><span>来源</span><span>判断</span>
      </div>
      <div class="field-rows">
        <div
          v-for="row in current.rows"
          :key="row.key"
          class="field-row"
          :class="row.action"
        >
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
        </div>
      </div>
      <div v-if="current.extra['本地对照']" class="local-align dim">
        {{ current.extra['本地对照'] }}
      </div>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :disabled="!current" :loading="applying" @click="apply">
        使用这个来源
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.link-row {
  display: flex;
  gap: 8px;
}
.link-row :deep(.el-input) {
  flex: 1;
}
.hint {
  margin-top: 6px;
  font-size: 12px;
}
.gap {
  margin-top: 10px;
}
.section-title {
  margin: 14px 0 6px;
  font-size: 12px;
  color: var(--text-3);
  letter-spacing: 0.02em;
}
.candidates {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.candidate {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  text-align: left;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
  padding: 9px 11px;
  cursor: pointer;
  font: inherit;
  color: inherit;
}
.candidate.on {
  border-color: var(--accent-line);
  background: var(--accent-soft);
}
.radio {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  border: 1px solid var(--text-3);
  flex: none;
}
.radio.on {
  border-color: var(--accent);
  background: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.cand-main {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.cand-title {
  font-size: 13px;
  font-weight: 550;
}
.cand-sub {
  font-size: 12px;
  color: var(--text-3);
}
.cand-meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  font-size: 12px;
  flex: none;
}
.chip {
  font-size: 11px;
  padding: 0 6px;
  border-radius: 999px;
  border: 1px solid var(--border);
}
.chip.fill {
  color: var(--accent-strong);
  border-color: var(--accent-line);
}
.chip.conflict {
  color: var(--danger);
  border-color: color-mix(in srgb, var(--danger) 40%, transparent);
}
.empty-result {
  padding: 16px 0;
  font-size: 12.5px;
}
.preview {
  margin-top: 4px;
}
.field-head,
.field-row {
  display: grid;
  grid-template-columns: 92px minmax(0, 1fr) 14px minmax(0, 1.1fr) 52px;
  align-items: center;
  gap: 8px;
}
.field-head {
  font-size: 11.5px;
  color: var(--text-3);
  padding: 2px 0 5px;
}
.field-rows {
  max-height: 300px;
  overflow: auto;
  border-top: 1px solid var(--border);
}
.field-row {
  padding: 4px 0;
  border-bottom: 1px solid var(--border);
  font-size: 12.5px;
}
.field-row.conflict {
  background: color-mix(in srgb, var(--danger) 5%, transparent);
}
.field-name {
  color: var(--text-3);
  font-size: 12px;
}
.field-value.blank {
  color: var(--text-3);
}
.arrow {
  color: var(--text-3);
}
.local-align {
  margin-top: 8px;
  font-size: 12px;
}
@media (max-width: 760px) {
  :deep(.el-dialog) {
    width: calc(100vw - 24px) !important;
    margin-top: 4vh;
  }
  .link-row {
    flex-wrap: wrap;
  }
  .candidate {
    align-items: flex-start;
  }
  .cand-meta {
    display: none;
  }
  .field-head,
  .field-row {
    grid-template-columns: 74px minmax(0, 1fr) 12px minmax(0, 1fr);
  }
  .field-head span:last-child,
  .field-row :deep(.el-tag) {
    display: none;
  }
}
</style>
