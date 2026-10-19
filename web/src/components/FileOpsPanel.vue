<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import { fmtBytes, splitPath } from '@/utils/format'
import AppIcon from './AppIcon.vue'

const props = defineProps<{ root: string; path: string; kind: string; size?: number }>()
const emit = defineEmits<{ (event: 'changed', payload: { renamed?: boolean }): void; (event: 'deleted'): void }>()

const current = computed(() => splitPath(props.path))
const newName = ref(current.value.name)
const busy = ref('')
const warnings = ref<string[]>([])
const fileInput = ref<HTMLInputElement>()
const uploading = ref(false)

watch(
  () => props.path,
  () => {
    newName.value = splitPath(props.path).name
    warnings.value = []
  },
)

async function rename() {
  const name = newName.value.trim()
  if (!name || name === current.value.name) return
  try {
    await ElMessageBox.confirm(
      `原文件名：${current.value.name}\n新文件名：${name}` +
        (current.value.dir ? `\n目录不变：${current.value.dir}` : ''),
      '确认改名',
      { confirmButtonText: '改名', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  busy.value = 'rename'
  warnings.value = []
  try {
    const result = await api.renameFile({ root: props.root, path: props.path, new_name: name })
    warnings.value = result.warnings ?? []
    ElMessage.success('已改名' + (result.sidecar_renamed ? '，歌词文件一并改名' : ''))
    emit('changed', { renamed: true })
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '改名失败')
  } finally {
    busy.value = ''
  }
}

async function replaceFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  try {
    await ElMessageBox.confirm(
      `用 ${file.name} 替换这个文件。旧文件会移到数据目录的 replaced 里，可以找回。`,
      '替换文件',
      { confirmButtonText: '替换', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  uploading.value = true
  try {
    const result = await api.replaceFile(props.root, props.path, file)
    ElMessage.success(
      result.renamed ? `已替换，扩展名按实际格式改为 ${result.extension_used}` : '已替换',
    )
    emit('changed', { renamed: result.renamed })
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '替换失败')
  } finally {
    uploading.value = false
  }
}

async function remove() {
  const isAudio = props.kind === 'audio'
  try {
    await ElMessageBox.confirm(
      isAudio
        ? '文件与同名 .lrc 会一起移到回收站，可以在回收站还原。'
        : '文件会移到回收站，可以在回收站还原。',
      '移入回收站',
      { confirmButtonText: '移入回收站', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  busy.value = 'delete'
  try {
    await api.deleteFile(props.root, props.path)
    ElMessage.success('已移入回收站')
    emit('deleted')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '删除失败')
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="ops">
    <section class="op">
      <div class="op-title">重命名</div>
      <div class="op-row">
        <el-input v-model="newName" @keyup.enter="rename">
          <template #prepend>{{ current.dir ? current.dir + '/' : '' }}</template>
        </el-input>
        <el-button
          :loading="busy === 'rename'"
          :disabled="!newName.trim() || newName.trim() === current.name"
          @click="rename"
        >
          改名
        </el-button>
      </div>
      <div class="cell-meta">只改同目录内的文件名；同名的歌词文件会一起改名。</div>
      <el-alert
        v-for="warning in warnings"
        :key="warning"
        :title="warning"
        type="warning"
        :closable="false"
        show-icon
        class="gap"
      />
    </section>

    <section v-if="kind === 'audio'" class="op">
      <div class="op-title">替换文件</div>
      <div class="op-row">
        <el-button :loading="uploading" @click="fileInput?.click()">
          <AppIcon name="Upload" :size="13" style="margin-right: 4px" />选择音频替换
        </el-button>
        <span v-if="size" class="cell-meta">当前 {{ fmtBytes(size) }}</span>
        <input ref="fileInput" type="file" accept="audio/*" hidden @change="replaceFile" />
      </div>
      <div class="cell-meta">文件名沿用原来的，扩展名按上传文件的真实容器改写。</div>
    </section>

    <section class="op">
      <div class="op-title">删除</div>
      <div class="op-row">
        <el-button type="danger" plain :loading="busy === 'delete'" @click="remove">
          <AppIcon name="Delete" :size="13" style="margin-right: 4px" />移入回收站
        </el-button>
      </div>
      <div class="cell-meta">
        {{ kind === 'audio' ? '同名歌词会一起移走，还原时也会一起还原。' : '随时可以在回收站还原。' }}
      </div>
    </section>
  </div>
</template>

<style scoped>
.ops {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.op {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border);
}

.op:last-child {
  border-bottom: 0;
}

.op-title {
  font-size: 12px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--text-3);
}

.op-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.op-row :deep(.el-input) {
  flex: 1;
}

.gap {
  margin-top: 4px;
}
</style>
