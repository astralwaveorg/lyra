<script setup lang="ts">
/**
 * 歌词编辑器：直接修改一份歌词并写回音频与同名 .lrc。
 *
 * 为什么需要它：此前歌词不对时只有「换一条候选」或「替换整个文件」两条路，
 * 想手动修一行、调一个时间轴必须借助外部编辑器——这是「处理能力弱」的
 * 直接原因之一。编辑器走 apply-lyrics 的 text 直写模式（不经过来源），
 * 与刮削写入同一套备份/校验/撤销链路。
 *
 * 编辑原文（含时间轴与元信息行）逐字节保留，不做任何自动改写；
 * 翻译可选一并写入（双语文体由服务端按设置合并）。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import { parseLrc } from '@/utils/lyrics'

const props = defineProps<{
  root: string
  path: string
  /** 初始内容：详情抽屉已加载的歌词原文。 */
  initialText: string
}>()

const emit = defineEmits<{
  (event: 'saved'): void
  (event: 'cancel'): void
}>()

const text = ref(props.initialText)
const translation = ref('')
const saving = ref(false)

watch(
  () => props.initialText,
  (value) => {
    text.value = value
  },
)

const stats = computed(() => {
  const lines = parseLrc(text.value)
  const timestamps = lines.filter((line) => line.ms !== null).length
  return {
    lines: text.value.split('\n').filter((line) => line.trim()).length,
    timestamps,
    synced: timestamps > 0,
  }
})

async function save() {
  if (!text.value.trim()) {
    ElMessage.warning('歌词内容为空')
    return
  }
  try {
    await ElMessageBox.confirm(
      '写入会覆盖这份文件现有的歌词（写入前完整备份，可撤销）。继续吗？',
      '写入歌词',
      { confirmButtonText: '写入', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  saving.value = true
  try {
    const result = await api.saveLyricsText({
      root: props.root,
      path: props.path,
      text: text.value,
      translation: translation.value.trim() || undefined,
      apply: true,
      replace_existing: true,
    })
    const applied = (result as { applied?: { ok?: boolean; error?: string } }).applied
    if (applied && applied.ok === false) {
      ElMessage.error(applied.error || '写入失败')
      return
    }
    ElMessage.success('歌词已写入（内嵌与同名 .lrc）')
    emit('saved')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '写入失败')
  } finally {
    saving.value = false
  }
}

function cancel() {
  if (text.value !== props.initialText) {
    ElMessageBox.confirm('歌词歌词改动还没有保存，确定放弃吗？', '放弃编辑', {
      confirmButtonText: '放弃',
      cancelButtonText: '继续编辑',
      type: 'warning',
    })
      .then(() => emit('cancel'))
      .catch(() => undefined)
    return
  }
  emit('cancel')
}
</script>

<template>
  <div class="editor">
    <div class="editor-bar">
      <span class="cell-meta">
        {{ stats.lines }} 行 · {{ stats.timestamps }} 个时间轴标记
        <template v-if="!stats.synced"> · 没有时间轴，写入后播放器无法逐行滚动</template>
      </span>
      <span class="cell-meta">[00:01.00] 分钟:秒.厘秒 —— 时间轴写法与 .lrc 一致</span>
    </div>
    <el-input
      v-model="text"
      type="textarea"
      class="editor-area"
      :autosize="{ minRows: 12, maxRows: 26 }"
      spellcheck="false"
    />
    <el-input
      v-model="translation"
      type="textarea"
      class="editor-translation"
      :autosize="{ minRows: 3, maxRows: 8 }"
      placeholder="翻译（可选）：带时间轴的整段翻译，写入时按双语文体合并；留空忽略"
      spellcheck="false"
    />
    <div class="editor-actions">
      <el-button type="primary" :loading="saving" @click="save">保存并写入</el-button>
      <el-button :disabled="saving" @click="cancel">取消</el-button>
    </div>
  </div>
</template>

<style scoped>
.editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.editor-bar {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.editor-area :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.6;
}
.editor-translation :deep(textarea) {
  font-size: 12.5px;
}
.editor-actions {
  display: flex;
  gap: 8px;
}
</style>
