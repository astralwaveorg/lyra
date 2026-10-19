<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { FileItem, FilePage } from '@/api/types'
import { useNarrow } from '@/composables/useMedia'
import { fmtBytes, fmtDate, splitPath } from '@/utils/format'
import { FILE_KIND_LABEL } from '@/utils/labels'
import PageHead from '@/components/PageHead.vue'
import EmptyState from '@/components/EmptyState.vue'
import FilePreviewDrawer from '@/components/FilePreviewDrawer.vue'
import TrackFixDrawer from '@/components/TrackFixDrawer.vue'
import AppIcon from '@/components/AppIcon.vue'

const narrow = useNarrow()

const kind = ref('')
const query = ref('')
const page = ref(1)
const size = ref(100)
const total = ref(0)
const byKind = ref<Record<string, number>>({})
const items = ref<FileItem[]>([])
const loading = ref(false)
const error = ref('')
const selected = ref<FileItem[]>([])
const deleting = ref(false)

const previewOpen = ref(false)
const previewItem = ref<FileItem | null>(null)
const editOpen = ref(false)
const editItem = ref<FileItem | null>(null)
const editTab = ref<'tags' | 'cover' | 'lyrics' | 'file'>('file')

const KINDS = [
  { value: '', label: '全部' },
  { value: 'audio', label: '音频' },
  { value: 'lyrics', label: '歌词' },
  { value: 'image', label: '图片' },
  { value: 'text', label: '文本' },
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data: FilePage = await api.files({
      kind: kind.value || undefined,
      q: query.value || undefined,
      page: page.value,
      size: size.value,
    })
    items.value = data.items
    total.value = data.total
    byKind.value = data.by_kind
    selected.value = []
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function applySearch() {
  page.value = 1
  void load()
}

watch(kind, () => {
  page.value = 1
  void load()
})
watch(page, load)

onMounted(load)

function kindCount(value: string) {
  if (!value) return Object.values(byKind.value).reduce((sum, n) => sum + n, 0)
  return byKind.value[value] ?? 0
}

function preview(item: FileItem) {
  previewItem.value = item
  previewOpen.value = true
}

function edit(item: FileItem) {
  editItem.value = item
  // 音频进「标签」——那是打开这个文件最常要做的事；其它文件只有「文件」。
  editTab.value = item.kind === 'audio' ? 'tags' : 'file'
  editOpen.value = true
}

function onSelection(rows: FileItem[]) {
  selected.value = rows
}

const selectionLabel = computed(() => {
  if (!selected.value.length) return ''
  const bytes = selected.value.reduce((sum, item) => sum + item.size, 0)
  return `已选 ${selected.value.length} 项 · ${fmtBytes(bytes)}`
})

async function batchDelete() {
  const groups = new Map<string, FileItem[]>()
  for (const item of selected.value) {
    if (!groups.has(item.root)) groups.set(item.root, [])
    groups.get(item.root)!.push(item)
  }
  if (!groups.size) return
  try {
    await ElMessageBox.confirm(
      `这 ${selected.value.length} 项会移到回收站，可以在回收站还原。`,
      '批量删除',
      { confirmButtonText: '移入回收站', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  deleting.value = true
  let moved = 0
  const errors: string[] = []
  try {
    for (const [root, list] of groups) {
      const result = await api.batchDelete(root, list.map((item) => item.rel_path))
      moved += result.moved.length
      errors.push(...result.errors)
    }
    if (errors.length) {
      ElMessage.warning(`已移入回收站 ${moved} 项，${errors.length} 项失败：${errors[0]}`)
    } else {
      ElMessage.success(`已移入回收站 ${moved} 项`)
    }
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '批量删除失败')
  } finally {
    deleting.value = false
  }
}

async function removeOne(item: FileItem) {
  try {
    await ElMessageBox.confirm('文件会移到回收站，可以在回收站还原。', '移入回收站', {
      confirmButtonText: '移入回收站',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.deleteFile(item.root, item.rel_path)
    ElMessage.success('已移入回收站')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  }
}
</script>

<template>
  <PageHead :count="total">
    <el-button :loading="loading" @click="load">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
    </el-button>
  </PageHead>

  <div class="filter-bar">
    <div class="filter-search">
      <el-input
        v-model="query"
        placeholder="搜索路径"
        clearable
        @keyup.enter="applySearch"
        @clear="applySearch"
      >
        <template #prefix><AppIcon name="Search" :size="14" /></template>
      </el-input>
      <el-button @click="applySearch">搜索</el-button>
    </div>
    <div class="filter-group">
      <span class="filter-label">类型</span>
      <el-select v-model="kind" class="filter-select">
        <el-option
          v-for="item in KINDS"
          :key="item.value"
          :label="`${item.label} · ${kindCount(item.value)}`"
          :value="item.value"
        />
      </el-select>
    </div>
  </div>

  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

  <div v-if="selected.length" class="bulk">
    <span class="bulk-text">{{ selectionLabel }}</span>
    <el-button size="small" type="danger" plain :loading="deleting" @click="batchDelete">
      <AppIcon name="Delete" :size="13" style="margin-right: 4px" />移入回收站
    </el-button>
    <el-button size="small" text @click="selected = []">取消选择</el-button>
  </div>

  <section class="card">
    <el-table
      v-if="!narrow"
      v-loading="loading"
      :data="items"
      :row-key="(row: FileItem) => `${row.root}/${row.rel_path}`"
      @selection-change="onSelection"
    >
      <el-table-column type="selection" width="42" />
      <el-table-column label="文件" min-width="340">
        <template #default="{ row }">
          <div class="cell-stack">
            <span class="path">{{ row.name }}</span>
            <span class="cell-meta ellipsis">{{ splitPath(row.rel_path).dir }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="86">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ FILE_KIND_LABEL[row.kind] ?? row.kind }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="大小" width="96">
        <template #default="{ row }"><span class="num cell-meta">{{ fmtBytes(row.size) }}</span></template>
      </el-table-column>
      <el-table-column label="修改时间" width="150">
        <template #default="{ row }"><span class="cell-meta num">{{ fmtDate(row.mtime) }}</span></template>
      </el-table-column>
      <el-table-column label="" width="170" align="right">
        <template #default="{ row }">
          <div class="row-actions">
            <el-button size="small" class="row-btn" @click="preview(row)">预览</el-button>
            <el-button size="small" class="row-btn" @click="edit(row)">编辑</el-button>
            <el-button size="small" class="row-btn danger" @click="removeOne(row)">删除</el-button>
          </div>
        </template>
      </el-table-column>
      <template #empty>
        <EmptyState title="没有文件" hint="调整筛选条件试试。" icon="FolderOpened" />
      </template>
    </el-table>

    <div v-else class="cards">
      <div v-for="item in items" :key="`${item.root}/${item.rel_path}`" class="file-card">
        <div class="file-head">
          <span class="path ellipsis">{{ item.name }}</span>
          <span class="cell-meta num">{{ fmtBytes(item.size) }}</span>
        </div>
        <div class="cell-meta ellipsis">{{ splitPath(item.rel_path).dir }}</div>
        <div class="file-actions">
          <el-tag size="small" type="info">{{ FILE_KIND_LABEL[item.kind] ?? item.kind }}</el-tag>
          <span style="flex: 1" />
          <el-button size="small" class="row-btn" @click="preview(item)">预览</el-button>
          <el-button size="small" class="row-btn" @click="edit(item)">编辑</el-button>
          <el-button size="small" class="row-btn danger" @click="removeOne(item)">删除</el-button>
        </div>
      </div>
      <EmptyState v-if="!items.length" title="没有文件" icon="FolderOpened" />
    </div>

    <div v-if="total > size" class="pager">
      <el-pagination
        v-model:current-page="page"
        :page-size="size"
        :total="total"
        layout="total, prev, pager, next"
        background
      />
    </div>
  </section>

  <FilePreviewDrawer
    v-model="previewOpen"
    :item="previewItem"
    @edit="
      () => {
        previewOpen = false
        if (previewItem) edit(previewItem)
      }
    "
  />

  <TrackFixDrawer
    v-if="editItem"
    v-model="editOpen"
    :root="editItem.root"
    :path="editItem.rel_path"
    :kind="editItem.kind"
    :tab="editTab"
    @refresh="load"
    @deleted="load"
  />
</template>

<style scoped>
.bulk {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 14px;
  border: 1px solid var(--accent-line);
  background: var(--accent-soft);
  border-radius: var(--r-md);
}

.bulk-text {
  flex: 1;
  font-size: 12.5px;
  color: var(--text);
}

.pager {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
}

.cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
}

.file-card {
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: var(--surface-2);
}

.file-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.file-head .path {
  flex: 1;
  min-width: 0;
  font-weight: 550;
}

.file-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-top: 4px;
}

@media (max-width: 720px) {
  .grow {
    flex: 1;
  }
}
</style>
