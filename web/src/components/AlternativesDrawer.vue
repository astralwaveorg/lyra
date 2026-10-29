<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { AlternativeOption, Alternatives, StagedDetail } from '@/api/types'
import { fmtDelta, fmtDuration, fmtScore, splitPath } from '@/utils/format'
import { parseLrc, alignLyrics, lyricBodies } from '@/utils/lyrics'
import { providerLabel } from '@/utils/labels'
import { useNarrow } from '@/composables/useMedia'
import ProviderBadge from './ProviderBadge.vue'
import EmptyState from './EmptyState.vue'
import AppIcon from './AppIcon.vue'

const props = defineProps<{ modelValue: boolean; stagedId: number | null; relPath: string }>()
const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
  (event: 'chosen'): void
}>()

const narrow = useNarrow()
const loading = ref(false)
const error = ref('')
const data = ref<Alternatives | null>(null)
const detail = ref<StagedDetail | null>(null)
const selected = ref('')
const compare = ref(false)
const choosing = ref('')

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const currentLines = computed(() => (detail.value ? parseLrc(detail.value.content) : []))
const currentOption = computed(() => data.value?.options.find((o) => keyOf(o) === selected.value) ?? null)

const diff = computed(() => {
  if (!currentOption.value) return []
  return alignLyrics(lyricBodies(currentLines.value), lyricBodies(currentOption.value.lines ?? []))
})

function keyOf(option: AlternativeOption) {
  return `${option.provider}/${option.provider_id}`
}

async function load() {
  if (!props.stagedId) return
  loading.value = true
  error.value = ''
  data.value = null
  detail.value = null
  compare.value = false
  try {
    const [alternatives, staged] = await Promise.all([
      api.stagedAlternatives(props.stagedId),
      api.stagedDetail(props.stagedId).catch(() => null),
    ])
    data.value = alternatives
    detail.value = staged
    const current = alternatives.options.find(
      (option) =>
        option.provider === alternatives.current.provider &&
        option.provider_id === alternatives.current.provider_id,
    )
    selected.value = current ? keyOf(current) : alternatives.options[0] ? keyOf(alternatives.options[0]) : ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function choose(option: AlternativeOption) {
  if (!props.stagedId) return
  choosing.value = keyOf(option)
  try {
    await api.chooseAlternative(props.stagedId, option.provider, option.provider_id)
    ElMessage.success('已改用所选候选')
    emit('chosen')
    visible.value = false
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '切换失败')
  } finally {
    choosing.value = ''
  }
}

watch(
  () => [props.modelValue, props.stagedId] as const,
  ([open]) => {
    if (open) void load()
  },
  { immediate: true },
)
</script>

<template>
  <el-drawer v-model="visible" :size="narrow ? '100%' : '760px'" :with-header="false" destroy-on-close>
    <div class="alts">
      <header class="alts-head">
        <div class="title">{{ splitPath(relPath).name }}</div>
        <div class="cell-meta ellipsis">{{ splitPath(relPath).dir }}</div>
      </header>

      <div v-if="loading" class="loading">
        <el-skeleton animated :rows="7" />
        <div class="cell-meta">正在向各来源取回歌词，通常需要十几秒</div>
      </div>
      <el-alert v-else-if="error" :title="error" type="error" show-icon :closable="false" />

      <template v-else-if="data">
        <div class="sources">
          <span v-for="source in data.sources" :key="source.provider" class="source">
            <ProviderBadge :name="source.provider" :muted="!source.found" />
            <span class="cell-meta">{{ source.error ? source.error : source.found ? `${source.found} 条` : '无结果' }}</span>
          </span>
        </div>

        <div class="compare-toggle">
          <span class="cell-meta">当前待写入：{{ providerLabel(data.current.provider) }}</span>
          <el-switch v-model="compare" active-text="与当前对比" />
        </div>

        <div class="options">
          <div
            v-for="option in data.options"
            :key="keyOf(option)"
            class="option"
            :class="{ on: keyOf(option) === selected }"
            @click="selected = keyOf(option)"
          >
            <div class="option-top">
              <ProviderBadge :name="option.provider" />
              <span class="option-title ellipsis">{{ option.title || '未命名' }}</span>
              <el-tag v-if="option.is_current" size="small" type="success">当前待写入</el-tag>
              <span class="score num" :class="{ good: (option.score ?? 0) >= 0.8 }">{{ fmtScore(option.score) }}</span>
            </div>
            <div class="option-meta">
              <span class="ellipsis">{{ option.artists.join(' / ') || '未知艺术家' }}</span>
              <span class="num">{{ option.duration ? fmtDuration(option.duration) : '—' }}</span>
              <span class="num dim">差 {{ fmtDelta(option.duration_delta) }}</span>
              <span class="cell-meta">{{ option.synced ? '有时间轴' : '无时间轴' }}</span>
            <span v-if="option.has_translation" class="cell-meta">含翻译</span>
            </div>
            <div v-if="option.error" class="option-warn">{{ option.error }}</div>
            <div v-else-if="option.rejected && option.reject_reason" class="option-warn">
              {{ option.reject_reason }}
            </div>
            <div class="option-actions">
              <el-button
                size="small"
                :type="keyOf(option) === selected ? 'primary' : 'default'"
                :loading="choosing === keyOf(option)"
                @click.stop="choose(option)"
              >
                采用此候选
              </el-button>
            </div>
          </div>
          <EmptyState v-if="!data.options.length" title="没有可比较的候选" icon="Search" />
        </div>

        <section v-if="currentOption" class="lyrics-block">
          <div class="lyrics-head">
            <span class="section-title">{{ compare ? '与当前待写入的差异' : '歌词' }}</span>
            <span class="cell-meta">{{ currentOption.synced ? '有时间轴' : '无时间轴' }} · {{ currentOption.lines.length }} 行</span>
          </div>

          <div v-if="compare" class="diff">
            <div class="diff-head">
              <span>当前</span>
              <span>这一份</span>
            </div>
            <div
              v-for="(row, index) in diff"
              :key="index"
              class="diff-row"
              :class="row.kind"
            >
              <span class="diff-cell">{{ row.left }}</span>
              <span class="diff-cell">{{ row.right }}</span>
            </div>
          </div>

          <div v-else class="plain">
            <div v-for="(line, index) in currentOption.lines" :key="index" class="plain-row">
              <span class="stamp num">{{ line.stamp }}</span>
              <span>{{ line.text }}</span>
            </div>
          </div>
        </section>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.alts {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.loading {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.alts-head {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding-right: 24px;
}

.title {
  font-size: 16px;
  font-weight: 600;
}

.sources {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}

.source {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.compare-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}

.options {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 46vh;
  overflow: auto;
}

.option {
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  cursor: pointer;
  background: var(--surface-2);
}

.option.on {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.option-top {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.option-title {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 550;
  color: var(--text);
}

.score {
  font-size: 12px;
  color: var(--text-3);
}

.score.good {
  color: var(--ok);
}

.option-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  color: var(--text-3);
}

.option-meta > span:first-child {
  flex: 1;
  min-width: 0;
}

.option-warn {
  font-size: 11.5px;
  color: var(--danger);
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

.option-actions {
  display: flex;
  justify-content: flex-end;
}

.lyrics-block {
  border-top: 1px solid var(--border);
  padding-top: 10px;
}

.lyrics-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.section-title {
  font-size: 11.5px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
}

.plain {
  max-height: 40vh;
  overflow: auto;
  font-size: 13px;
  line-height: 1.9;
  color: var(--text-2);
}

.plain-row {
  display: grid;
  grid-template-columns: 54px minmax(0, 1fr);
  gap: 8px;
}

.stamp {
  color: var(--text-3);
  font-size: 11px;
  text-align: right;
  padding-top: 2px;
}

.diff {
  max-height: 42vh;
  overflow: auto;
  font-size: 13px;
  line-height: 1.8;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
}

.diff-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  font-size: 11px;
  color: var(--text-3);
  letter-spacing: 0.05em;
  padding: 6px 10px;
  background: var(--surface-2);
  position: sticky;
  top: 0;
}

.diff-head span:first-child {
  border-right: 1px solid var(--border);
  padding-right: 8px;
}

.diff-head span:last-child {
  padding-left: 8px;
}

.diff-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}

.diff-cell {
  padding: 1px 10px;
  min-height: 22px;
  word-break: break-word;
}

.diff-row.del .diff-cell:first-child {
  background: var(--danger-soft);
  color: var(--danger);
}

.diff-row.add .diff-cell:last-child {
  background: var(--ok-soft);
  color: var(--ok);
}

.diff-row.same .diff-cell {
  color: var(--text-3);
}

.diff-row .diff-cell:first-child {
  border-right: 1px solid var(--border);
}
</style>
