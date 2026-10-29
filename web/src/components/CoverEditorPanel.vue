<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { FileMeta } from '@/api/types'
import { fmtBytes } from '@/utils/format'
import AppIcon from './AppIcon.vue'

const props = defineProps<{ root: string; path: string; meta: FileMeta | null; hasCover: boolean }>()
const emit = defineEmits<{ (event: 'changed'): void }>()

const busy = ref('')
const bust = ref(Date.now())
const sibling = ref<{ found: boolean; name?: string; width?: number; height?: number; error?: string } | null>(null)
const url = ref('')
const fileInput = ref<HTMLInputElement>()

async function checkSibling() {
  try {
    sibling.value = await api.coverSibling(props.root, props.path)
  } catch {
    sibling.value = null
  }
}

watch(
  () => props.path,
  () => {
    void checkSibling()
    url.value = ''
    bust.value = Date.now()
  },
  { immediate: true },
)

watch(
  () => props.hasCover,
  () => {
    bust.value = Date.now()
  },
)

async function run(label: string, fn: () => Promise<void>) {
  busy.value = label
  try {
    await fn()
    bust.value = Date.now()
    emit('changed')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '操作失败')
  } finally {
    busy.value = ''
  }
}

async function onUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  await run('upload', async () => {
    await api.coverUpload(props.root, props.path, file)
    ElMessage.success('已写入封面')
  })
}

async function fromDir() {
  await run('dir', async () => {
    const result = await api.coverFromDir(props.root, props.path)
const embeddedSource = String((result as { source_file?: string }).source_file ?? '')
ElMessage.success(embeddedSource ? `已嵌入 ${embeddedSource}` : '已嵌入同目录图片')
  })
}

async function fromUrl() {
  if (!url.value.trim()) return
  await run('url', async () => {
    await api.coverFromUrl(props.root, props.path, url.value.trim())
    url.value = ''
    ElMessage.success('已下载并写入封面')
  })
}

async function remove() {
  try {
    await ElMessageBox.confirm('移除后这个文件就没有内嵌封面了。', '移除封面', {
      confirmButtonText: '移除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  await run('remove', async () => {
    await api.coverRemove(props.root, props.path)
    ElMessage.success('已移除封面')
  })
}
</script>

<template>
  <div class="cover-panel">
    <div class="cover-preview">
      <div v-if="hasCover" class="cover-frame">
        <img :src="api.coverUrl(root, path, bust)" :alt="`${path} 的封面`" />
      </div>
      <div v-else class="cover-empty">
        <AppIcon name="Picture" :size="22" />
        <span>没有内嵌封面</span>
      </div>
    </div>

    <div class="cover-actions">
      <div class="action">
        <div class="action-text">
          <div class="action-title">上传图片</div>
          <div class="cell-meta">JPG / PNG / WebP，最大 20 MB</div>
        </div>
        <el-button :loading="busy === 'upload'" @click="fileInput?.click()">选择文件</el-button>
        <input ref="fileInput" type="file" accept="image/*" hidden @change="onUpload" />
      </div>

      <div v-if="sibling?.found" class="action">
        <div class="action-text">
          <div class="action-title">使用同目录图片</div>
          <div class="cell-meta">
            {{ sibling.name }}
            <span v-if="sibling.width"> · {{ sibling.width }}×{{ sibling.height }}</span>
          </div>
        </div>
        <el-button :loading="busy === 'dir'" @click="fromDir">嵌入</el-button>
      </div>

      <div class="action column">
        <div class="action-text">
          <div class="action-title">从链接下载</div>
          <div class="cell-meta">服务端抓取，适合来源站点的封面地址</div>
        </div>
        <div class="url-row">
          <el-input v-model="url" placeholder="https://..." clearable @keyup.enter="fromUrl" />
          <el-button :loading="busy === 'url'" :disabled="!url.trim()" @click="fromUrl">下载</el-button>
        </div>
      </div>

      <div v-if="hasCover" class="action">
        <div class="action-text">
          <div class="action-title">移除封面</div>
          <div class="cell-meta">只改封面块，音频数据不变</div>
        </div>
        <el-button type="danger" plain :loading="busy === 'remove'" @click="remove">移除</el-button>
      </div>

      <div v-if="meta" class="cover-meta">
        文件 {{ fmtBytes(meta.size) }} · {{ meta.media_format.toUpperCase() }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.cover-panel {
  display: flex;
  gap: 18px;
  align-items: flex-start;
}

.cover-preview {
  flex: none;
}

.cover-frame {
  width: 160px;
  height: 160px;
  border-radius: var(--r-md);
  overflow: hidden;
  border: 1px solid var(--border);
  background: var(--surface-2);
}

.cover-frame img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.cover-empty {
  width: 160px;
  height: 160px;
  border-radius: var(--r-md);
  border: 1px dashed var(--border-strong);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--text-3);
  font-size: 12px;
  background: var(--surface-2);
}

.cover-actions {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.action {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 0;
  border-bottom: 1px solid var(--border);
}

.action:last-of-type {
  border-bottom: 0;
}

.action.column {
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
}

.action-text {
  flex: 1;
  min-width: 0;
}

.action-title {
  font-size: 13px;
  font-weight: 550;
}

.url-row {
  display: flex;
  gap: 8px;
}

.url-row :deep(.el-input) {
  flex: 1;
}

.cover-meta {
  font-size: 11.5px;
  color: var(--text-3);
  padding-top: 6px;
}

@media (max-width: 720px) {
  .cover-panel {
    flex-direction: column;
  }
}
</style>
