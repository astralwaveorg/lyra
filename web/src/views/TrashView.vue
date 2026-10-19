<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { TrashBatch, TrashList } from '@/api/types'
import { useMeta } from '@/stores/meta'
import { fmtBytes, splitPath } from '@/utils/format'
import { FILE_KIND_LABEL } from '@/utils/labels'
import PageHead from '@/components/PageHead.vue'
import EmptyState from '@/components/EmptyState.vue'
import AppIcon from '@/components/AppIcon.vue'

const meta = useMeta()
const data = ref<TrashList | null>(null)
const loading = ref(false)
const error = ref('')
const root = ref('')
const busy = ref('')

const roots = computed(() => meta.summary?.roots.map((item) => item.name) ?? [])

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.trash()
    if (!root.value && roots.value.length) root.value = roots.value[0]
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function restore(batch: TrashBatch, relPath: string) {
  if (!root.value) {
    ElMessage.warning('先选择要还原到哪个受管目录')
    return
  }
  busy.value = relPath
  try {
    const result = await api.restoreTrash(batch.batch, relPath, root.value)
    const extra = result.also_restored.length ? `，同时还原了 ${result.also_restored.length} 个歌词文件` : ''
    ElMessage.success(`已还原${extra}`)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '还原失败')
  } finally {
    busy.value = ''
  }
}

async function empty(batch?: string) {
  try {
    await ElMessageBox.confirm(
      batch
        ? '这个批次的文件会被永久删除，无法恢复。'
        : '回收站里的全部文件会被永久删除，无法恢复。',
      '永久删除',
      {
        confirmButtonText: '永久删除',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  busy.value = batch ?? 'all'
  try {
    const result = await api.emptyTrash(batch)
    ElMessage.success(`已删除 ${result.removed_files} 个文件，释放 ${fmtBytes(result.removed_bytes)}`)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  if (!meta.summary) await meta.refresh().catch(() => undefined)
  await load()
})
</script>

<template>
  <PageHead :count="data?.total">
    <el-select v-if="roots.length > 1" v-model="root" class="root-sel">
      <el-option v-for="name in roots" :key="name" :label="`还原到 ${name}`" :value="name" />
    </el-select>
    <el-button :loading="loading" @click="load">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
    </el-button>
    <el-button
      v-if="data?.total"
      type="danger"
      plain
      :loading="busy === 'all'"
      @click="empty()"
    >
      清空回收站
    </el-button>
  </PageHead>

  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

  <div v-if="data?.total" class="bar">
    <span class="cell-meta">{{ data.total }} 个文件 · {{ fmtBytes(data.bytes) }}</span>
    <span class="cell-meta">删除是移动，还原会把文件放回原位置</span>
  </div>

  <el-skeleton v-if="loading && !data" animated :rows="6" />

  <template v-else>
    <section v-for="batch in data?.batches ?? []" :key="batch.batch" class="card">
      <div class="card-head">
        <span class="card-title num">{{ batch.batch }}</span>
        <span class="card-hint">{{ batch.items.length }} 个文件</span>
        <div class="card-actions">
          <el-button size="small" text type="danger" :loading="busy === batch.batch" @click="empty(batch.batch)">
            删除该批次
          </el-button>
        </div>
      </div>
      <div class="items">
        <div v-for="item in batch.items" :key="item.rel_path" class="item">
          <div class="item-main">
            <span class="path">{{ item.name }}</span>
            <span class="cell-meta ellipsis">{{ splitPath(item.rel_path).dir }}</span>
          </div>
          <el-tag size="small" type="info">{{ FILE_KIND_LABEL[item.kind] ?? '其它' }}</el-tag>
          <span class="cell-meta num size">{{ fmtBytes(item.size) }}</span>
          <el-button
            size="small"
            class="row-btn accent"
            :loading="busy === item.rel_path"
            @click="restore(batch, item.rel_path)"
          >
            还原
          </el-button>
        </div>
      </div>
    </section>

    <EmptyState
      v-if="!(data?.batches ?? []).length"
      title="回收站是空的"
      hint="删除的文件会先放到这里，随时可以还原。"
      icon="Delete"
    />
  </template>
</template>

<style scoped>
.root-sel {
  width: 150px;
}

.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.items {
  display: flex;
  flex-direction: column;
}

.item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 16px;
  border-bottom: 1px solid var(--border);
}

.item:last-child {
  border-bottom: 0;
}

.item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.size {
  width: 76px;
  text-align: right;
}

@media (max-width: 720px) {
  .item {
    flex-wrap: wrap;
  }

  .item-main {
    flex-basis: 100%;
  }
}
</style>
