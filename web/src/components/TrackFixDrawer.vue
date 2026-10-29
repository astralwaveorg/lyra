<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
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
import LyricsEditor from '@/components/LyricsEditor.vue'
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

const editing = ref(false)

function startEditing() {
  editing.value = true
}

function editorText(): string {
  // 优先用接口返回的原文（保留元信息行与原始写法）；旧响应没有 text 时按行拼回
  if (lyricContent.value?.text !== undefined) return lyricContent.value.text
  return (lyricContent.value?.lines ?? [])
    .map((line) => (line.stamp ? `${line.stamp}${line.text}` : line.text))
    .join('\n')
}

function afterEditorSaved() {
  editing.value = false
  lyricContent.value = null
  void loadLyrics()
  emit('refresh')
}

function afterEditorCancel() {
  editing.value = false
}

async function loadLyrics() {
  if (!isAudio.value) return
  lyricLoading.value = true
  lyricContent.value = null
  try {
    const wanted = sidecar.value?.path
    if (wanted) {
      lyricContent.value = await api.fileContent(props.root, wanted)
    } else if (meta.value?.embedded_text) {
      // 没有 .lrc 但内嵌有词：以内嵌为底稿，预览与编辑都可用
      //（保存走 apply-lyrics 的 text 直写，会同时落内嵌与新建 .lrc）
      const text = meta.value.embedded_text
      lyricContent.value = {
        rel_path: props.path,
        kind: 'lyrics',
        size: text.length,
        truncated: false,
        has_bom: false,
        state: meta.value.embedded_state,
        line_count: text.split('\n').filter((l) => l.trim()).length,
        timestamp_count: (text.match(/\[[0-9]/g) ?? []).length,
        offset_ms: 0,
        has_word_level: false,
        tags: {},
        problems: [],
        lines: text.split('\n').map((line) => {
          const m = line.match(/^\[([^\]]+)\]/)
          return { ms: null, stamp: m ? m[0] : '', text: m ? line.slice(m[0].length) : line }
        }),
        text,
      }
    }
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
  void loadMeta().then(() => {
    // 刮削成功后用户多半还停在歌词页签：同链路刷新，别显示旧词或假空态
    if (active.value === 'lyrics' && !editing.value) void loadLyrics()
  })
  emit('refresh')
}

watch(
  () => [props.modelValue, props.path] as const,
  ([open], previous) => {
    const prevOpen = previous?.[0]
    if (!open) return
    const reopened = prevOpen === true && open
    // 重新打开且停留在歌词页签时，active 不变会让 watch(active) 失效，
    // 歌词页会停留在上一次清空的「没有可预览的歌词文件」假空态
    if (!reopened) editing.value = false
    active.value = props.tab
    meta.value = null
    sidecar.value = null
    lyricContent.value = null
    void loadMeta().then(() => {
      if (active.value === 'lyrics') void loadLyrics()
    })
  },
  { immediate: true },
)

watch(active, (tab, prev) => {
  if (tab === 'lyrics') {
    if (!lyricContent.value) void loadLyrics()
    return
  }
  // 离开歌词页签时若有未保存的编辑，先确认再切，避免静默丢失
  if (prev === 'lyrics' && editing.value && lyricContent.value) {
    const draft = (document.querySelector('.el-drawer textarea') as HTMLTextAreaElement | null)?.value
    if (draft !== undefined && draft !== editorText()) {
      ElMessageBox.confirm('歌词改动还没有保存，确定放弃吗？', '放弃编辑', {
        confirmButtonText: '放弃',
        cancelButtonText: '继续编辑',
        type: 'warning',
      })
        .then(() => {
          editing.value = false
        })
        .catch(() => {
          active.value = 'lyrics'
        })
      return
    }
  }
  editing.value = false
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
                <el-button size="small" type="primary" @click="scrapeOpen = true">抓取歌词</el-button>
                <el-button
                  v-if="lyricContent && !editing"
                  size="small"
                  @click="startEditing"
                >
                  编辑歌词
                </el-button>
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
            <LyricsEditor
              v-else-if="editing && lyricContent"
              :root="root"
              :path="path"
              :initial-text="editorText()"
              @saved="afterEditorSaved"
              @cancel="afterEditorCancel"
            />
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
