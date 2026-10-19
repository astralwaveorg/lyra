<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { OrganizeOp, RenameRow, TxnRow, WriteRow } from '@/api/types'
import { fmtDate, splitPath } from '@/utils/format'
import { txnStateLabel } from '@/utils/labels'
import PageHead from '@/components/PageHead.vue'
import EmptyState from '@/components/EmptyState.vue'
import AppIcon from '@/components/AppIcon.vue'

const tab = ref<'writes' | 'renames' | 'organize' | 'transactions'>('writes')
const loading = ref(false)
const error = ref('')
const writes = ref<WriteRow[]>([])
const transactions = ref<TxnRow[]>([])
const renames = ref<RenameRow[]>([])
const ops = ref<OrganizeOp[]>([])
const unfinished = ref(0)
const totalWrites = ref(0)
const selectedWrites = ref<WriteRow[]>([])
const selectedRenames = ref<RenameRow[]>([])
const selectedOps = ref<OrganizeOp[]>([])
const busy = ref(false)

function canSelectOp(row: OrganizeOp): boolean {
  return !row.undone_at
}

const OP_KIND: Record<string, string> = {
  move: '移动',
  delete: '删除',
  rmdir: '空目录',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    if (tab.value === 'writes') {
      const data = await api.writes()
      writes.value = data.items
      totalWrites.value = data.total
    } else if (tab.value === 'transactions') {
      const data = await api.transactions()
      transactions.value = data.items
      unfinished.value = data.unfinished
    } else if (tab.value === 'renames') {
      renames.value = (await api.renames()).items
    } else {
      ops.value = (await api.organizeOps(true)).items
    }
    selectedWrites.value = []
    selectedRenames.value = []
    selectedOps.value = []
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function undoWrite(row: WriteRow) {
  try {
    await ElMessageBox.confirm('会把这一个歌词文件恢复到写入之前的样子。', '撤销写入', {
      confirmButtonText: '撤销',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  await doUndoWrite(row)
}

async function doUndoWrite(row: WriteRow) {
  try {
    const result = await api.undoWrite(row.id)
    if (result.ok) ElMessage.success(result.message || '已撤销')
    else ElMessage.error(result.message || '撤销失败')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '撤销失败')
  }
}

async function batchUndoWrites() {
  if (!selectedWrites.value.length) return
  try {
    await ElMessageBox.confirm(
      `会撤销选中的 ${selectedWrites.value.length} 条写入，逐条恢复原歌词。`,
      '批量撤销写入',
      { confirmButtonText: '撤销', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  busy.value = true
  let ok = 0
  const errors: string[] = []
  try {
    for (const row of [...selectedWrites.value].sort((a, b) => b.at - a.at)) {
      try {
        const result = await api.undoWrite(row.id)
        if (result.ok) ok += 1
        else errors.push(result.message)
      } catch (err) {
        errors.push(err instanceof Error ? err.message : '撤销失败')
      }
    }
    if (errors.length) ElMessage.warning(`已撤销 ${ok} 条，${errors.length} 条失败：${errors[0]}`)
    else ElMessage.success(`已撤销 ${ok} 条`)
    await load()
  } finally {
    busy.value = false
  }
}

async function deleteWrite(row: WriteRow) {
  try {
    await api.deleteWrite(row.id)
    ElMessage.success('记录已删除，文件与备份未改动')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  }
}

async function clearWrites() {
  try {
    await ElMessageBox.confirm('只清理记录，不删除任何文件。', '清空写入记录', {
      confirmButtonText: '清空',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const result = await api.clearWrites()
    ElMessage.success(`已清理 ${result.deleted} 条记录`)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '清理失败')
  }
}

async function undoRename(row: RenameRow) {
  try {
    await ElMessageBox.confirm(`会把文件改回 ${row.old}。`, '撤销改名', {
      confirmButtonText: '撤销',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const result = await api.undoRename(row.id)
    if (result.ok) ElMessage.success(result.message || '已撤销')
    else ElMessage.error(result.message || '撤销失败')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '撤销失败')
  }
}

async function batchUndoRenames() {
  if (!selectedRenames.value.length) return
  try {
    await ElMessageBox.confirm(
      `会撤销选中的 ${selectedRenames.value.length} 条改名，按时间倒序恢复。`,
      '批量撤销改名',
      { confirmButtonText: '撤销', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  busy.value = true
  try {
    const result = await api.batchUndoRenames(selectedRenames.value.map((row) => row.id))
    if (result.failed) {
      ElMessage.warning(`已撤销 ${result.undone} 条，${result.failed} 条失败：${result.messages[0] ?? ''}`)
    } else {
      ElMessage.success(`已撤销 ${result.undone} 条`)
    }
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '批量撤销失败')
  } finally {
    busy.value = false
  }
}

async function batchUndoOps() {
  const active = selectedOps.value.filter((row) => !row.undone_at)
  if (!active.length) {
    ElMessage.warning('选中的记录都已经撤销过了')
    return
  }
  try {
    await ElMessageBox.confirm(
      `会撤销选中的 ${active.length} 项整理操作，移动会退回原路径，删除会从回收站还原。` +
        '按时间倒序处理；原路径已被占用时该项会跳过。',
      '批量撤销整理',
      { confirmButtonText: '撤销', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  busy.value = true
  try {
    const result = await api.organizeUndo(active.map((row) => row.id))
    if (result.failed) {
      ElMessage.warning(`已撤销 ${result.undone} 项，${result.failed} 项失败：${result.messages[0] ?? ''}`)
    } else {
      ElMessage.success(`已撤销 ${result.undone} 项`)
    }
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '批量撤销失败')
  } finally {
    busy.value = false
  }
}

async function deleteTxn(row: TxnRow) {
  try {
    await api.deleteTransaction(row.txn_id)
    ElMessage.success('已删除')
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  }
}

async function clearTxn() {
  try {
    await ElMessageBox.confirm('只清理已完结的事务记录。', '清空事务记录', {
      confirmButtonText: '清空',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const result = await api.clearTransactions()
    ElMessage.success(`已清理 ${result.deleted} 条记录`)
    await load()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '清理失败')
  }
}

onMounted(load)
</script>

<template>
  <PageHead>
    <el-button :loading="loading" @click="load">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
    </el-button>
  </PageHead>

  <el-tabs v-model="tab" @tab-change="load">
    <el-tab-pane label="写入记录" name="writes" />
    <el-tab-pane label="改名" name="renames" />
    <el-tab-pane label="整理记录" name="organize" />
    <el-tab-pane label="事务" name="transactions" />
  </el-tabs>

  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

  <template v-if="tab === 'writes'">
    <div class="bar">
      <span class="cell-meta">只保留最近 100 条 · 勾选后可批量撤销</span>
      <div class="bar-actions">
        <el-button
          v-if="selectedWrites.length"
          size="small"
          type="primary"
          plain
          :loading="busy"
          @click="batchUndoWrites"
        >
          撤销所选 {{ selectedWrites.length }} 条
        </el-button>
        <el-button size="small" class="row-btn danger" @click="clearWrites">清空记录</el-button>
      </div>
    </div>
    <section class="card">
      <el-table v-loading="loading" :data="writes" :show-header="writes.length > 0" @selection-change="selectedWrites = $event">
        <el-table-column type="selection" width="42" />
        <el-table-column label="文件" min-width="320">
          <template #default="{ row }">
            <div class="cell-stack">
              <span class="path">{{ splitPath(row.path).name }}</span>
              <span class="cell-meta ellipsis">{{ splitPath(row.rel_path || row.path).dir }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="方式" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.created_new ? 'success' : 'info'">
              {{ row.created_new ? '新建' : '更新' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="150">
          <template #default="{ row }"><span class="cell-meta num">{{ fmtDate(row.at) }}</span></template>
        </el-table-column>
        <el-table-column label="" width="150" align="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button size="small" class="row-btn accent" @click="undoWrite(row)">撤销</el-button>
              <el-button size="small" class="row-btn danger" @click="deleteWrite(row)">删除记录</el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState title="还没有写入记录" hint="抓取并写入歌词后，这里会保留最近 100 条，可逐条或批量撤销。" icon="Document" />
        </template>
      </el-table>
    </section>
  </template>

  <template v-else-if="tab === 'renames'">
    <div class="bar">
      <span class="cell-meta">改名后可随时撤销 · 勾选多条可一起撤销</span>
      <el-button
        v-if="selectedRenames.length"
        size="small"
        type="primary"
        plain
        :loading="busy"
        @click="batchUndoRenames"
      >
        撤销所选 {{ selectedRenames.length }} 条
      </el-button>
    </div>
    <section class="card">
      <el-table v-loading="loading" :data="renames" :show-header="renames.length > 0" @selection-change="selectedRenames = $event">
        <el-table-column type="selection" width="42" />
        <el-table-column label="原名" min-width="240">
          <template #default="{ row }"><span class="path">{{ splitPath(row.old).name }}</span></template>
        </el-table-column>
        <el-table-column label="新名" min-width="240">
          <template #default="{ row }"><span class="path">{{ splitPath(row.new).name }}</span></template>
        </el-table-column>
        <el-table-column label="原因" width="150">
          <template #default="{ row }"><span class="cell-meta">{{ row.reason }}</span></template>
        </el-table-column>
        <el-table-column label="时间" width="150">
          <template #default="{ row }"><span class="cell-meta num">{{ fmtDate(row.at) }}</span></template>
        </el-table-column>
        <el-table-column label="" width="90" align="right">
          <template #default="{ row }">
            <el-button size="small" class="row-btn accent" @click="undoRename(row)">撤销</el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyState title="没有改名记录" icon="Document" /></template>
      </el-table>
    </section>
  </template>

  <template v-else-if="tab === 'organize'">
    <div class="bar">
      <span class="cell-meta">
        移动改名按路径撤销，删除从回收站还原 · 勾选多条可一起撤销
      </span>
      <el-button
        v-if="selectedOps.filter((row) => !row.undone_at).length"
        size="small"
        type="primary"
        plain
        :loading="busy"
        @click="batchUndoOps"
      >
        撤销所选 {{ selectedOps.filter((row) => !row.undone_at).length }} 项
      </el-button>
    </div>
    <section class="card">
      <el-table v-loading="loading" :data="ops" :show-header="ops.length > 0" @selection-change="selectedOps = $event">
        <el-table-column type="selection" width="42" :selectable="canSelectOp" />
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.kind === 'delete' ? 'danger' : row.kind === 'rmdir' ? 'info' : 'success'">
              {{ OP_KIND[row.kind] ?? row.kind }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="原路径" min-width="260">
          <template #default="{ row }">
            <span class="path ellipsis" :title="row.old_rel_path">{{ row.old_rel_path }}</span>
          </template>
        </el-table-column>
        <el-table-column label="新路径 / 回收站" min-width="260">
          <template #default="{ row }">
            <span class="path ellipsis" :title="row.new_rel_path || row.trash_rel_path">
              {{ row.new_rel_path || row.trash_rel_path || '—' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="150">
          <template #default="{ row }"><span class="cell-meta num">{{ fmtDate(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.undone_at ? 'info' : 'success'">
              {{ row.undone_at ? '已撤销' : '可撤销' }}
            </el-tag>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState title="还没有整理记录" hint="在「曲库整理」执行移动或清理后，这里会逐条记录。" icon="Document" />
        </template>
      </el-table>
    </section>
  </template>

  <template v-else>
    <div class="bar">
      <span class="cell-meta">
        未完结 {{ unfinished }} 条 · 写入按事务记录，用于崩溃后恢复
      </span>
      <el-button size="small" class="row-btn danger" @click="clearTxn">清空记录</el-button>
    </div>
    <section class="card">
      <el-table v-loading="loading" :data="transactions" :show-header="transactions.length > 0">
        <el-table-column label="事务" prop="txn_id" min-width="220">
          <template #default="{ row }"><span class="mono">{{ row.txn_id }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="
                row.state === 'completed' || row.state === 'sidecar_committed'
                  ? 'success'
                  : row.state === 'rolled_back'
                    ? 'info'
                    : row.state === 'recovery_conflict'
                      ? 'danger'
                      : 'warning'
              "
            >
              {{ txnStateLabel(row.state) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="错误" min-width="200">
          <template #default="{ row }"><span class="cell-meta">{{ row.error || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="更新时间" width="150">
          <template #default="{ row }"><span class="cell-meta num">{{ fmtDate(row.updated_at) }}</span></template>
        </el-table-column>
        <el-table-column label="" width="90" align="right">
          <template #default="{ row }">
            <el-button size="small" class="row-btn danger" @click="deleteTxn(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyState title="没有事务记录" icon="Document" /></template>
      </el-table>
    </section>
  </template>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 2px 2px 0;
  flex-wrap: wrap;
}

.bar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
