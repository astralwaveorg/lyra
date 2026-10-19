<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { FileMeta, LyricContent } from '@/api/types'
import { useNarrow } from '@/composables/useMedia'
import { fmtBytes, fmtDuration, splitPath } from '@/utils/format'
import { LYRIC_STATE, stateTone } from '@/utils/labels'
import AppIcon from './AppIcon.vue'
import TagEditorPanel from './TagEditorPanel.vue'
import CoverEditorPanel from './CoverEditorPanel.vue'
import FileOpsPanel from './FileOpsPanel.vue'
import LyricsViewer from './LyricsViewer.vue'
import ScrapeDialog from './ScrapeDialog.vue'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    root: string
    path: string
    kind?: string
    tab?: 'tags' | 'cover' | 'lyrics' | 'file'
  }>(),
  { kind: 'audio', tab: 'tags' },
)
const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
  (event: 'refresh'): void
  (event: 'deleted'): void
}>()

const narrow = useNarrow()
const active = ref(props.tab)
const meta = ref<FileMeta | null>(null)
const metaError = ref('')
const loading = ref(false)
const sidecar = ref<{ path: string | null; exists: boolean; state: string; name_differs: boolean; wanted: string } | null>(null)
const lyricContent = ref<LyricContent | null>(null)
const lyricLoading = ref(false)
const scrapeOpen = ref(false)

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const name = computed(() => splitPath(props.path).name)
const dir = computed(() => splitPath(props.path).dir)
const isAudio = computed(() => props.kind === 'audio')

async function loadMeta() {
  if (!isAudio.value) {
    meta.value = null
    return
  }
  loading.value = true
  metaError.value = ''
  try {
    meta.value = await api.fileMeta(props.root, props.path)
    sidecar.value = await api.fileSidecar(props.root, props.path).catch(() => null)
  } catch (err) {
    metaError.value = err instanceof Error ? err.message : '读取失败'
  } finally {
    loading.value = false
  }
}

async function loadLyrics() {
  if (!isAudio.value) return
  lyricLoading.value = true
  lyricContent.value = null
  try {
    const wanted = sidecar.value?.path
    if (wanted) lyricContent.value = await api.fileContent(props.root, wanted)
  } catch (err) {
    ElMessage.warning(err instanceof Error ? err.message : '读取歌词失败')
  } finally {
    lyricLoading.value = false
  }
}

async function renameSidecar() {
  const info = sidecar.value
  if (!info?.path || !info.name_differs) return
  try {
    await api.renameFile({
      root: props.root,
      path: info.path,
      new_name: splitPath(info.wanted).name,
    })
    ElMessage.success('歌词文件已改为与音频同名')
    await loadMeta()
    emit('refresh')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '改名失败')
  }
}

function refreshAll() {
  void loadMeta()
  lyricContent.value = null
  emit('refresh')
}

watch(
  () => [props.modelValue, props.path] as const,
  ([open]) => {
    if (!open) return
    active.value = props.tab
    meta.value = null
    sidecar.value = null
    lyricContent.value = null
    void loadMeta()
  },
  { immediate: true },
)

watch(active, (tab) => {
  if (tab === 'lyrics' && !lyricContent.value) void loadLyrics()
})
</script>

<template>
  <el-drawer v-model="visible" :size="narrow ? '100%' : '560px'" :with-header="false" destroy-on-close>
    <div class="detail">
      <header class="detail-head">
        <div class="detail-title">{{ name }}</div>
        <div class="cell-meta ellipsis" :title="path">{{ dir }}</div>
        <div v-if="meta" class="detail-facts">
          <el-tag size="small" type="info">{{ meta.media_format.toUpperCase() }}</el-tag>
          <span class="cell-meta">{{ fmtDuration(meta.duration) }}</span>
          <span class="cell-meta">{{ fmtBytes(meta.size) }}</span>
          <el-tag size="small" :type="stateTone(meta.embedded_state)">
            {{ LYRIC_STATE[meta.embedded_state] ?? meta.embedded_state }}
          </el-tag>
        </div>
      </header>

      <el-alert v-if="metaError" :title="metaError" type="error" show-icon :closable="false" />

      <el-tabs v-if="isAudio" v-model="active" class="detail-tabs">
        <el-tab-pane label="标签" name="tags">
          <el-skeleton v-if="loading" animated :rows="8" />
          <TagEditorPanel v-else :root="root" :path="path" :meta="meta" @saved="refreshAll" />
        </el-tab-pane>

        <el-tab-pane label="封面" name="cover">
          <el-skeleton v-if="loading" animated :rows="6" />
          <CoverEditorPanel
            v-else
            :root="root"
            :path="path"
            :meta="meta"
            :has-cover="meta?.has_cover ?? false"
            @changed="refreshAll"
          />
        </el-tab-pane>

        <el-tab-pane label="歌词" name="lyrics">
          <div class="lyrics-pane">
            <div class="lyric-head">
              <div>
                <div class="op-title">歌词文件</div>
                <div class="cell-meta">
                  <template v-if="sidecar?.exists">
                    {{ sidecar.path }}
                    <span v-if="sidecar.name_differs" class="warn-text">· 名称与音频不同</span>
                  </template>
                  <template v-else>没有 .lrc 文件</template>
                </div>
              </div>
              <div class="lyric-actions">
                <el-button size="small" type="primary" @click="scrapeOpen = true">刮削歌词</el-button>
                <el-button
                  v-if="sidecar?.exists && sidecar.name_differs"
                  size="small"
                  @click="renameSidecar"
                >
                  改为同名
                </el-button>
              </div>
            </div>

            <el-skeleton v-if="lyricLoading" animated :rows="6" />
            <template v-else-if="lyricContent">
              <div class="lyric-meta">
                <el-tag size="small" :type="stateTone(lyricContent.state)">
                  {{ LYRIC_STATE[lyricContent.state] ?? lyricContent.state }}
                </el-tag>
                <span class="cell-meta">{{ lyricContent.line_count }} 行</span>
                <span v-if="lyricContent.offset_ms" class="cell-meta">
                  偏移 {{ lyricContent.offset_ms }} ms
                </span>
                <span v-if="lyricContent.has_bom" class="cell-meta">带 BOM</span>
              </div>
              <LyricsViewer :lines="lyricContent.lines" max-height="48vh" />
            </template>
            <div v-else class="dim">没有可预览的歌词文件</div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="文件" name="file">
          <FileOpsPanel
            :root="root"
            :path="path"
            :kind="kind"
            :size="meta?.size"
            @changed="refreshAll"
            @deleted="
              () => {
                visible = false
                emit('deleted')
              }
            "
          />
        </el-tab-pane>
      </el-tabs>

      <FileOpsPanel
        v-else
        :root="root"
        :path="path"
        :kind="kind"
        @changed="refreshAll"
        @deleted="
          () => {
            visible = false
            emit('deleted')
          }
        "
      />
    </div>

    <ScrapeDialog v-model="scrapeOpen" :root="root" :path="path" @done="refreshAll" />
  </el-drawer>
</template>

<style scoped>
.detail {
  display: flex;
  flex-direction: column;
  gap: 14px;
  height: 100%;
}

.detail-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-right: 24px;
}

.detail-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
  word-break: break-all;
}

.detail-facts {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 4px;
}

.detail-tabs :deep(.el-tabs__content) {
  overflow: visible;
}

.lyrics-pane {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.lyric-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.lyric-actions {
  display: flex;
  gap: 6px;
  flex: none;
}

.lyric-meta {
  display: flex;
  align-items: center;
  gap: 10px;
}

.op-title {
  font-size: 12px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--text-3);
}

.warn-text {
  color: var(--warn);
}
</style>
