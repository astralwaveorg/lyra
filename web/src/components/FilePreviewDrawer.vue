<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { FileItem, FileMeta, LyricContent } from '@/api/types'
import { fmtBytes, fmtDate, fmtDuration, splitPath } from '@/utils/format'
import { LYRIC_STATE, tagFieldLabel } from '@/utils/labels'
import { useNarrow } from '@/composables/useMedia'
import AppIcon from './AppIcon.vue'
import LyricsViewer from './LyricsViewer.vue'

const props = defineProps<{ modelValue: boolean; item: FileItem | null }>()
const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
  (event: 'edit'): void
}>()

const narrow = useNarrow()
const loading = ref(false)
const error = ref('')
const content = ref<LyricContent | null>(null)
const meta = ref<FileMeta | null>(null)

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const name = computed(() => (props.item ? splitPath(props.item.rel_path).name : ''))
const dir = computed(() => (props.item ? splitPath(props.item.rel_path).dir : ''))

async function load() {
  const item = props.item
  if (!item) return
  loading.value = true
  error.value = ''
  content.value = null
  meta.value = null
  try {
    if (item.kind === 'lyrics' || item.kind === 'text') {
      content.value = await api.fileContent(item.root, item.rel_path)
    } else if (item.kind === 'audio') {
      meta.value = await api.fileMeta(item.root, item.rel_path)
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '预览失败'
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.item?.rel_path] as const,
  ([open]) => {
    if (open) void load()
  },
  { immediate: true },
)

const tagRows = computed(() => {
  if (!meta.value) return []
  return Object.entries(meta.value.all_tags)
    .filter(([, values]) => values.length > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, values]) => ({ key, label: tagFieldLabel(key), value: values.join(' / ') }))
})
</script>

<template>
  <el-drawer v-model="visible" :size="narrow ? '100%' : '520px'" :with-header="false" destroy-on-close>
    <div v-if="item" class="preview">
      <header class="head">
        <div class="title">{{ name }}</div>
        <div class="cell-meta ellipsis" :title="item.rel_path">{{ dir }}</div>
        <div class="facts">
          <el-tag size="small" type="info">{{ item.ext.replace('.', '').toUpperCase() }}</el-tag>
          <span class="cell-meta">{{ fmtBytes(item.size) }}</span>
          <span class="cell-meta">{{ fmtDate(item.mtime) }}</span>
        </div>
        <div class="head-actions">
          <el-button size="small" @click="emit('edit')">
            <AppIcon name="Edit" :size="12" style="margin-right: 4px" />编辑
          </el-button>
        </div>
      </header>

      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

      <el-skeleton v-else-if="loading" animated :rows="8" />

      <template v-else>
        <div v-if="item.kind === 'audio'" class="audio">
          <img
            v-if="meta?.has_cover"
            class="cover"
            :src="api.coverUrl(item.root, item.rel_path, Date.now())"
            alt="封面"
          />
          <div v-else class="cover cover-empty"><AppIcon name="Picture" :size="20" /></div>
          <div class="audio-side">
            <div class="audio-title ellipsis">{{ tagRows.find((r) => r.key === 'TITLE')?.value || name }}</div>
            <div class="cell-meta ellipsis">
              {{ tagRows.find((r) => r.key === 'ARTIST')?.value || '未知艺术家' }}
            </div>
            <audio
              :src="api.mediaUrl(item.root, item.rel_path)"
              controls
              preload="none"
              class="player"
            />
            <div class="cell-meta">
              {{ meta ? `${fmtDuration(meta.duration)} · ${LYRIC_STATE[meta.embedded_state] ?? meta.embedded_state}` : '' }}
            </div>
          </div>
        </div>

        <img
          v-else-if="item.kind === 'image'"
          class="image"
          :src="api.mediaUrl(item.root, item.rel_path)"
          :alt="name"
        />

        <template v-else-if="item.kind === 'lyrics'">
          <div v-if="content" class="lyric-meta">
            <el-tag size="small" :type="content.state === 'synced' ? 'success' : 'warning'">
              {{ LYRIC_STATE[content.state] ?? content.state }}
            </el-tag>
            <span class="cell-meta">{{ content.line_count }} 行</span>
            <span v-if="content.has_bom" class="cell-meta">带 BOM</span>
            <span v-if="content.truncated" class="cell-meta">已截断</span>
          </div>
          <LyricsViewer v-if="content" :lines="content.lines" max-height="60vh" />
        </template>

        <pre v-else-if="content?.text" class="text">{{ content.text }}</pre>

        <template v-if="item.kind === 'audio' && tagRows.length">
          <div class="tags">
            <div class="section-title">标签</div>
            <div v-for="row in tagRows" :key="row.key" class="tag-row">
              <span class="tag-key">{{ row.label }}</span>
              <span class="tag-value">{{ row.value }}</span>
            </div>
          </div>
        </template>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.preview {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.head {
  display: flex;
  flex-direction: column;
  gap: 4px;
  position: relative;
  padding-right: 90px;
}

.title {
  font-size: 16px;
  font-weight: 600;
  word-break: break-all;
}

.facts {
  display: flex;
  align-items: center;
  gap: 10px;
}

.head-actions {
  position: absolute;
  top: 0;
  right: 0;
}

.audio {
  display: flex;
  gap: 14px;
  align-items: center;
}

.cover {
  width: 108px;
  height: 108px;
  border-radius: var(--r-md);
  object-fit: cover;
  border: 1px solid var(--border);
  flex: none;
}

.cover-empty {
  display: grid;
  place-items: center;
  background: var(--surface-2);
  color: var(--text-3);
}

.audio-side {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.audio-title {
  font-size: 14px;
  font-weight: 600;
}

.player {
  width: 100%;
  height: 34px;
  margin-top: 4px;
}

.image {
  max-width: 100%;
  border-radius: var(--r-md);
  border: 1px solid var(--border);
}

.lyric-meta {
  display: flex;
  align-items: center;
  gap: 10px;
}

.text {
  margin: 0;
  padding: 12px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-2);
  max-height: 62vh;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.tags {
  border-top: 1px solid var(--border);
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.section-title {
  font-size: 11.5px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
}

.tag-row {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr);
  gap: 10px;
  font-size: 12px;
}

.tag-key {
  color: var(--text-3);
}

.tag-value {
  color: var(--text-2);
  word-break: break-word;
}
</style>
