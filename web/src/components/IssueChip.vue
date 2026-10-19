<script setup lang="ts">
import { computed } from 'vue'
import { issueLabel } from '@/utils/labels'

const props = defineProps<{ code: string; clickable?: boolean }>()
defineEmits<{ (event: 'pick', code: string): void }>()

const tone = computed(() => {
  if (/^(empty_sidecar|orphan_sidecar|duplicate_sidecar|sidecar_bom|sidecar_unparseable)$/.test(props.code))
    return 'danger'
  if (/^(sidecar_|lyrics_mismatch|suspicious_lyrics)/.test(props.code)) return 'warn'
  if (/^(missing_|no_lyrics|plain_embedded)/.test(props.code)) return 'info'
  if (/cover/.test(props.code)) return 'muted'
  return 'muted'
})
</script>

<template>
  <el-tag
    size="small"
    :type="tone === 'danger' ? 'danger' : tone === 'warn' ? 'warning' : tone === 'info' ? 'primary' : 'info'"
    :effect="clickable ? 'light' : 'plain'"
    :class="['issue-chip', { clickable }]"
    @click="clickable && $emit('pick', code)"
  >
    {{ issueLabel(code) }}
  </el-tag>
</template>

<style scoped>
.issue-chip {
  cursor: default;
}

.issue-chip.clickable {
  cursor: pointer;
}
</style>
