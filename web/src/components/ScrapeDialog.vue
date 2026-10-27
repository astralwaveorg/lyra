<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, ApiError } from '@/api/client'
import type { LyricsPreview, TrackCandidate, TrackCandidates } from '@/api/types'
import { fmtDelta, fmtDuration, fmtScore, splitPath } from '@/utils/format'
import { DECISION_LABEL } from '@/utils/labels'
import { useNarrow } from '@/composables/useMedia'
import LyricsViewer from './LyricsViewer.vue'
import ProviderBadge from './ProviderBadge.vue'
import EmptyState from './EmptyState.vue'
import AppIcon from './AppIcon.vue'

const props = defineProps<{ modelValue: boolean; root: string; path: string }>()
const emit = defineEmits<{ (event: 'update:modelValue', value: boolean): void; (event: 'done'): void }>()

const narrow = useNarrow()
const loading = ref(false)
const error = ref('')
const data = ref<TrackCandidates | null>(null)
const selected = ref<string>('')
const preview = ref<LyricsPreview | null>(null)
const previewLoading = ref(false)
const previewError = ref('')
const replaceExisting = ref(false)
const applying = ref(false)

const linkUrl = ref('')
const linkLoading = ref(false)
const linkResult = ref<(LyricsPreview & { detail: unknown; url: string }) | null>(null)
const linkError = ref('')

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const fileName = computed(() => splitPath(props.path).name)
const options = computed<TrackCandidate[]>(() => data.value?.options ?? [])
const current = computed(() => options.value.find((item) => keyOf(item) === selected.value) ?? null)
const existing = computed(() => data.value?.has_existing_lyrics ?? false)

function keyOf(item: { provider: string; provider_id: string }) {
  return `${item.provider}/${item.provider_id}`
}

function failedGates(item: TrackCandidate) {
  return item.gates.filter((gate) => !gate.passed)
}

async function load() {
  loading.value = true
  error.value = ''
  data.value = null
  preview.value = null
  selected.value = ''
  linkResult.value = null
  linkUrl.value = ''
  linkError.value = ''
  replaceExisting.value = false
  try {
    data.value = await api.trackCandidates({ root: props.root, path: props.path })
    const first = data.value.options[0]
    if (first) {
      selected.value = keyOf(first)
      void loadPreview(first)
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '候选列表加载失败'
  } finally {
    loading.value = false
  }
}

async function loadPreview(option: TrackCandidate) {
  previewLoading.value = true
  previewError.value = ''
  preview.value = null
  try {
    preview.value = await api.lyricsPreview({
      root: props.root,
      path: props.path,
      provider: option.provider,
      provider_id: option.provider_id,
    })
  } catch (err) {
    previewError.value = err instanceof Error ? err.message : '取歌词失败'
  } finally {
    previewLoading.value = false
  }
}

function pick(option: TrackCandidate) {
  selected.value = keyOf(option)
  linkResult.value = null
  void loadPreview(option)
}

async function adopt() {
  const option = current.value
  if (!option) return
  if (existing.value && !replaceExisting.value) {
    ElMessage.warning('这份文件已有歌词，若要替换请先勾选「覆盖已有歌词」')
    return
  }
  applying.value = true
  try {
    const result = await api.applyLyrics({
      root: props.root,
      path: props.path,
      provider: option.provider,
      provider_id: option.provider_id,
      apply: true,
      replace_existing: replaceExisting.value,
    })
    if (result.applied && !result.applied.ok) {
      ElMessage.error(result.applied.error || result.applied.errors[0] || '写入失败')
      return
    }
    ElMessage.success(result.applied?.message || '已写入歌词')
    emit('done')
    visible.value = false
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '写入失败')
  } finally {
    applying.value = false
  }
}

async function resolveLink() {
  const url = linkUrl.value.trim()
  if (!url) return
  linkLoading.value = true
  linkError.value = ''
  linkResult.value = null
  try {
    linkResult.value = await api.resolveLink({ root: props.root, path: props.path, url })
    selected.value = ''
    preview.value = null
  } catch (err) {
    linkError.value = err instanceof Error ? err.message : '链接解析失败'
  } finally {
    linkLoading.value = false
  }
}

async function adoptLink() {
  if (!linkResult.value) return
  if (existing.value && !replaceExisting.value) {
    ElMessage.warning('这份文件已有歌词，若要替换请先勾选「覆盖已有歌词」')
    return
  }
  applying.value = true
  try {
    const result = await api.applyLyrics({
      root: props.root,
      path: props.path,
      provider: linkResult.value.provider,
      provider_id: linkResult.value.provider_id,
      apply: true,
      replace_existing: replaceExisting.value,
      by_id: true,
    })
    if (result.applied && !result.applied.ok) {
      ElMessage.error(result.applied.error || result.applied.errors[0] || '写入失败')
      return
    }
    ElMessage.success(result.applied?.message || '已写入歌词')
    emit('done')
    visible.value = false
  } catch (err) {
    const message = err instanceof ApiError ? err.message : '写入失败'
    ElMessage.error(message)
  } finally {
    applying.value = false
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) void load()
  },
  // **必须 immediate。** 这个对话框由父组件 `v-if` 控制挂载，
  // 打开时组件是「带着 modelValue=true 出生」的——非 immediate 的 watch
  // 永远等不到 false→true 的变化，结果是弹出一个空白框（实际遇到过）。
  { immediate: true },
)
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`刮削歌词 · ${fileName}`"
    :width="narrow ? '94%' : '940px'"
    :top="narrow ? '3vh' : '6vh'"
    destroy-on-close
  >
    <div v-if="loading" class="loading">
      <el-skeleton animated :rows="6" />
      <div class="loading-note dim">正在向各来源查询，通常需要几秒</div>
    </div>

    <el-alert v-else-if="error" :title="error" type="error" show-icon :closable="false" />

    <template v-else-if="data">
      <div class="track-line">
        <div class="track-title">{{ data.track.title || fileName }}</div>
        <div class="track-meta">
          {{ data.track.artists.join(' / ') || '未知艺术家' }}
          <span v-if="data.track.album"> · {{ data.track.album }}</span>
          <span v-if="data.track.duration"> · {{ fmtDuration(data.track.duration) }}</span>
        </div>
      </div>

      <div class="sources">
        <span v-for="source in data.sources" :key="source.provider" class="source">
          <ProviderBadge :name="source.provider" :muted="!source.found" />
          <span class="cell-meta">
            {{ source.error ? source.error : source.found ? `${source.found} 条` : '无结果' }}
          </span>
        </span>
      </div>

      <el-alert
        v-if="data.verdict.reason"
        class="verdict"
        :type="data.verdict.decision === 'accept' ? 'success' : data.verdict.decision === 'reject' ? 'error' : 'warning'"
        :title="data.verdict.reason"
        :closable="false"
        show-icon
      />

      <div v-if="data.has_existing_lyrics" class="existing">
        <AppIcon name="WarningFilled" :size="14" />
        <span>这份文件已经有歌词，写入会替换它。</span>
      </div>

      <div class="split">
        <div class="col-list">
          <div class="col-head">候选 {{ options.length }}</div>
          <div class="options">
            <button
              v-for="option in options"
              :key="keyOf(option)"
              class="option"
              :class="{ on: keyOf(option) === selected }"
              @click="pick(option)"
            >
              <div class="option-top">
                <ProviderBadge :name="option.provider" />
                <span class="option-title ellipsis">{{ option.title || '未命名' }}</span>
                <span class="score num" :class="{ good: (option.score ?? 0) >= 0.8 }">
                  {{ fmtScore(option.score) }}
                </span>
              </div>
              <div class="option-meta">
                <span class="ellipsis">{{ option.artists.join(' / ') || '未知艺术家' }}</span>
                <span class="num">{{ option.duration ? fmtDuration(option.duration) : '—' }}</span>
                <span class="num dim">差 {{ fmtDelta(option.duration_delta) }}</span>
              </div>
              <div v-if="option.rejected && option.reject_reason" class="option-warn">
                {{ option.reject_reason }}
              </div>
              <div v-else-if="failedGates(option).length" class="gates">
                <span v-for="gate in failedGates(option)" :key="gate.key" class="gate">
                  {{ gate.label }} · {{ gate.detail }}
                </span>
              </div>
            </button>
            <EmptyState
              v-if="!options.length"
              title="各来源都没有返回候选"
              hint="可以直接粘贴歌曲链接，用你确定的那一条。"
              icon="Search"
            />
          </div>
        </div>

        <div class="col-preview">
          <div class="col-head">
            歌词预览
            <span v-if="preview" class="cell-meta">
              {{ preview.synced ? '有时间轴' : '无时间轴' }} · {{ preview.line_count }} 行
            </span>
          </div>
          <el-skeleton v-if="previewLoading" animated :rows="8" />
          <el-alert v-else-if="previewError" :title="previewError" type="warning" :closable="false" />
          <LyricsViewer v-else-if="preview" :lines="preview.lines" max-height="46vh" />
          <div v-else class="dim">选择左侧候选查看歌词</div>
        </div>
      </div>

    </template>

    <!-- 链接区始终可用：候选还没加载完、或者一条都不合适时，直接贴链接 -->
      <div class="link-box">
        <div class="link-head">用链接指定</div>
        <div class="link-row">
          <el-input
            v-model="linkUrl"
            placeholder="粘贴 QQ 音乐或网易云音乐的歌曲链接"
            clearable
            @keyup.enter="resolveLink"
          />
          <el-button :loading="linkLoading" @click="resolveLink">解析</el-button>
        </div>
        <el-checkbox v-model="replaceExisting" size="small">覆盖已有歌词</el-checkbox>
        <el-alert v-if="linkError" :title="linkError" type="error" :closable="false" show-icon />
        <div v-if="linkResult" class="link-result">
          <div class="link-info">
            <ProviderBadge :name="linkResult.provider" />
            <span class="cell-meta">
              {{ linkResult.synced ? '有时间轴' : '无时间轴' }} · {{ linkResult.line_count }} 行
            </span>
          </div>
          <LyricsViewer :lines="linkResult.lines" max-height="30vh" />
        </div>
      </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        v-if="linkResult"
        type="primary"
        :loading="applying"
        :disabled="data?.has_existing_lyrics && !replaceExisting"
        @click="adoptLink"
      >
        采用这一条
      </el-button>
      <el-button
        v-else
        type="primary"
        :loading="applying"
        :disabled="!current || (data?.has_existing_lyrics && !replaceExisting)"
        @click="adopt"
      >
        采用并写入
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.loading {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 4px 0 12px;
}

.loading-note {
  font-size: 12px;
}

.track-line {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: 10px;
}

.track-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}

.track-meta {
  font-size: 12.5px;
  color: var(--text-2);
}

.sources {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  padding-bottom: 10px;
}

.source {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.verdict {
  margin-bottom: 10px;
}

.existing {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  margin-bottom: 10px;
  border-radius: var(--r-md);
  background: var(--warn-soft);
  color: var(--warn);
  font-size: 12.5px;
}

.split {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}

.col-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
  margin-bottom: 8px;
}

.options {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 42vh;
  overflow: auto;
  padding-right: 2px;
}

.option {
  text-align: left;
  border: 1px solid var(--border);
  background: var(--surface-2);
  border-radius: var(--r-md);
  padding: 9px 11px;
  cursor: pointer;
  font: inherit;
  color: var(--text-2);
  display: flex;
  flex-direction: column;
  gap: 5px;
  transition: border-color 0.15s, background 0.15s;
}

.option:hover {
  border-color: var(--border-strong);
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
  color: var(--text);
  font-weight: 500;
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

.col-preview {
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 10px 12px;
  background: var(--surface-2);
  min-height: 180px;
}

.link-box {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.link-head {
  font-size: 11.5px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
}

.link-row {
  display: flex;
  gap: 8px;
}

.link-row :deep(.el-input) {
  flex: 1;
}

.link-result {
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 10px 12px;
  background: var(--surface-2);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.link-info {
  display: flex;
  align-items: center;
  gap: 8px;
}

.footer-check {
  margin-right: auto;
}

@media (max-width: 860px) {
  .split {
    grid-template-columns: minmax(0, 1fr);
  }

  .options {
    max-height: none;
  }
}
</style>
