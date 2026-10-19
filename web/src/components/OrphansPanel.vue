<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { LyricContent, OrphanItem } from '@/api/types'
import { useMeta } from '@/stores/meta'
import { fmtBytes, splitPath } from '@/utils/format'
import { LYRIC_STATE, ORPHAN_ACTION_LABEL, stateTone } from '@/utils/labels'
import EmptyState from '@/components/EmptyState.vue'
import LyricsViewer from '@/components/LyricsViewer.vue'
import { useNarrow } from '@/composables/useMedia'
import AppIcon from '@/components/AppIcon.vue'

const emit = defineEmits<{ (event: 'count', value: number): void }>()

const meta = useMeta()
const narrow = useNarrow()
const items = ref<OrphanItem[]>([])
const duplicates = ref(0)
const sameContent = ref(0)
const loading = ref(false)
const error = ref('')
const busy = ref('')
const rootFilter = ref('')

const previewOpen = ref(false)
const previewItem = ref<OrphanItem | null>(null)
const previewContent = ref<LyricContent | null>(null)
const previewLoading = ref(false)

const aiOpen = ref(false)
const aiLoading = ref(false)
const aiApplying = ref(false)
const aiItems = ref<
  {
    rel_path: string
    paired_sidecar?: string
    audio?: string
    action?: string
    note?: string
    error?: string
    applied?: boolean
    overlap?: number
    judgement?: { same_song: boolean; confidence: number; keep: string; reason: string }
  }[]
>([])
const aiError = ref('')

const roots = computed(() => meta.summary?.roots.map((root) => root.name) ?? [])
const visible = computed(() =>
  rootFilter.value ? items.value.filter((item) => item.root === rootFilter.value) : items.value,
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api.orphans()
    items.value = data.items
    duplicates.value = data.duplicates
    sameContent.value = data.same_content
    emit('count', data.items.length)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function openPreview(item: OrphanItem) {
  previewItem.value = item
  previewOpen.value = true
  previewContent.value = null
  previewLoading.value = true
  try {
    previewContent.value = await api.fileContent(item.root, item.rel_path)
  } catch (err) {
    ElMessage.warning(err instanceof Error ? err.message : '读取失败')
  } finally {
    previewLoading.value = false
  }
}

async function remove(item: OrphanItem) {
  try {
    await ElMessageBox.confirm(
      '这份歌词会移到回收站，可以还原。它当前没有对应的音频。',
      '移入回收站',
      { confirmButtonText: '移入回收站', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  busy.value = item.rel_path
  try {
    await api.deleteFile(item.root, item.rel_path)
    ElMessage.success('已移入回收站')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  } finally {
    busy.value = ''
  }
}

async function repair(item: OrphanItem) {
  if (!item.guess_audio) return
  try {
    await ElMessageBox.confirm(
      item.paired_sidecar
        ? `这份歌词会成为 ${splitPath(item.guess_audio).name} 的歌词，原有的那一份会移到回收站。`
        : `这份歌词会改名为 ${splitPath(item.guess_audio).name} 的同名歌词。`,
      '设为这首音频的歌词',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  busy.value = item.rel_path
  try {
    await api.replaceOrphan({
      root: item.root,
      path: item.rel_path,
      target: item.guess_audio,
      confirm: true,
    })
    ElMessage.success('已设为这首音频的歌词')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '操作失败')
  } finally {
    busy.value = ''
  }
}

async function runAiReview() {
  aiOpen.value = true
  aiLoading.value = true
  aiError.value = ''
  aiItems.value = []
  try {
    const result = await api.aiReviewOrphans({ root: rootFilter.value || undefined })
    aiItems.value = result.items as typeof aiItems.value
  } catch (err) {
    aiError.value = err instanceof Error ? err.message : 'AI 判断失败'
  } finally {
    aiLoading.value = false
  }
}

async function applyAi() {
  aiApplying.value = true
  try {
    const result = await api.aiReviewOrphans({
      root: rootFilter.value || undefined,
      apply: true,
      min_confidence: 0.8,
    })
    aiItems.value = result.items as typeof aiItems.value
    const applied = result.items.filter((row) => (row as { applied?: boolean }).applied).length
    ElMessage.success(applied ? `已按建议处理 ${applied} 项` : '没有高置信度可执行的项')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '执行失败')
  } finally {
    aiApplying.value = false
  }
}

function aiActionLabel(action?: string) {
  if (action === 'cleanup') return '清理多余的那份'
  if (action === 'replace') return '用这份替换'
  return '保留不动'
}

onMounted(async () => {
  if (!meta.summary) await meta.refresh().catch(() => undefined)
  await load()
})

defineExpose({ reload: load })
</script>

<template>
  <div class="panel-tools">
    <span v-if="loading" class="cell-meta">正在检查无音源歌词…</span>
    <span v-else-if="visible.length" class="cell-meta">
      无音源 .lrc 共 {{ visible.length }} 份 · 与已有歌词重复 {{ duplicates }} 份
      <template v-if="sameContent">（内容完全相同 {{ sameContent }} 份）</template>
    </span>
    <span class="grow" />
    <el-select v-if="roots.length > 1" v-model="rootFilter" class="sel" placeholder="全部目录" clearable>
      <el-option v-for="name in roots" :key="name" :label="name" :value="name" />
    </el-select>
    <el-button :disabled="!duplicates" @click="runAiReview">AI 判断重复项</el-button>
    <el-button :loading="loading" @click="load">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
    </el-button>
  </div>

  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

  <el-skeleton v-if="loading" animated :rows="5" />

  <EmptyState
    v-else-if="!visible.length"
    title="没有无音源的歌词文件"
    hint="每个 .lrc 都能对上音频时，这里就是空的。"
    icon="Files"
  />

  <section v-else class="card">
    <el-table :data="visible">
      <el-table-column label="歌词文件" min-width="300">
        <template #default="{ row }">
          <div class="cell-stack">
            <span class="path">{{ splitPath(row.rel_path).name }}</span>
            <span class="cell-meta ellipsis">{{ splitPath(row.rel_path).dir }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="判断" width="150">
        <template #default="{ row }">
          <el-tag size="small" :type="row.action === 'duplicate' ? 'warning' : row.action === 'choose' ? 'primary' : 'info'">
            {{ ORPHAN_ACTION_LABEL[row.action] ?? row.action }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="歌词状态" width="112">
        <template #default="{ row }">
          <el-tag size="small" :type="stateTone(row.state)">
            {{ LYRIC_STATE[row.state] ?? row.state }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="大小" width="88">
        <template #default="{ row }"><span class="cell-meta num">{{ fmtBytes(row.size) }}</span></template>
      </el-table-column>
      <el-table-column label="推断目标" min-width="220">
        <template #default="{ row }">
          <div class="cell-stack">
            <span class="ellipsis">{{ row.guess_audio ? splitPath(row.guess_audio).name : '没有相关音频' }}</span>
            <span v-if="row.paired_sidecar" class="cell-meta">已有歌词：{{ splitPath(row.paired_sidecar).name }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="" width="190" align="right">
        <template #default="{ row }">
          <div class="row-actions">
            <el-button size="small" text @click="openPreview(row)">预览</el-button>
            <el-button
              v-if="row.action !== 'duplicate' && row.guess_audio"
              size="small"
              text
              :loading="busy === row.rel_path"
              @click="repair(row)"
            >
              设为这首的歌词
            </el-button>
            <el-button size="small" text type="danger" :loading="busy === row.rel_path" @click="remove(row)">
              移入回收站
            </el-button>
          </div>
        </template>
      </el-table-column>
      <template #empty><EmptyState title="没有无归属的歌词文件" icon="Files" /></template>
    </el-table>
  </section>


  <el-drawer v-model="previewOpen" :size="narrow ? '100%' : '520px'" :with-header="false" destroy-on-close>
    <div v-if="previewItem" class="preview">
      <div class="preview-head">
        <div class="preview-title">{{ splitPath(previewItem.rel_path).name }}</div>
        <div class="cell-meta">{{ splitPath(previewItem.rel_path).dir }}</div>
      </div>
      <el-skeleton v-if="previewLoading" animated :rows="8" />
      <LyricsViewer v-else-if="previewContent" :lines="previewContent.lines" max-height="70vh" />
    </div>
  </el-drawer>

  <el-dialog v-model="aiOpen" title="AI 判断重复歌词" :width="narrow ? '94%' : '720px'" top="8vh">
    <el-skeleton v-if="aiLoading" animated :rows="6" />
    <el-alert v-else-if="aiError" :title="aiError" type="error" show-icon :closable="false" />
    <template v-else>
      <div class="ai-list">
        <div v-for="row in aiItems" :key="row.rel_path" class="ai-row">
          <div class="ai-main">
            <div class="path">{{ splitPath(row.rel_path).name }}</div>
            <div v-if="row.judgement" class="cell-meta">
              {{ row.judgement.same_song ? '同一首歌' : '不是同一首' }} · 置信度
              {{ Math.round((row.judgement.confidence ?? 0) * 100) }}% · 重合
              {{ Math.round((row.overlap ?? 0) * 100) }}%
            </div>
            <div v-if="row.note" class="cell-meta">{{ row.note }}</div>
            <div v-if="row.error" class="ai-error">{{ row.error }}</div>
          </div>
          <el-tag size="small" :type="row.action === 'cleanup' || row.action === 'replace' ? 'warning' : 'info'">
            {{ aiActionLabel(row.action) }}
          </el-tag>
          <el-tag v-if="row.applied" size="small" type="success">已执行</el-tag>
        </div>
        <EmptyState v-if="!aiItems.length" title="没有需要判断的重复项" icon="CircleCheck" />
      </div>
      <div class="ai-note cell-meta">
        只有「确认是同一首歌」且「多余那份没有独有歌词行」时才会建议清理；执行是移入回收站，可还原。
      </div>
    </template>
    <template #footer>
      <el-button @click="aiOpen = false">关闭</el-button>
      <el-button type="primary" :loading="aiApplying" :disabled="aiLoading || !aiItems.length" @click="applyAi">
        按建议执行
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.panel-tools {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.panel-tools .grow {
  flex: 1;
}

.sel {
  width: 140px;
}

.preview {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.preview-head {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding-right: 24px;
}

.preview-title {
  font-size: 16px;
  font-weight: 600;
  word-break: break-all;
}

.ai-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 52vh;
  overflow: auto;
}

.ai-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
}

.ai-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.ai-error {
  font-size: 12px;
  color: var(--danger);
}

.ai-note {
  margin-top: 10px;
  font-size: 12px;
}
</style>
