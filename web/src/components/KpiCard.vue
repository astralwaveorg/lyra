<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    label: string
    value: string | number
    suffix?: string
    foot?: string
    /** 0–100；给出时底部显示进度条 */
    pct?: number | null
    tone?: 'default' | 'ok' | 'warn' | 'danger'
  }>(),
  { suffix: '', foot: '', pct: null, tone: 'default' },
)

const barColor = computed(() => {
  if (props.tone === 'ok') return 'var(--ok)'
  if (props.tone === 'warn') return 'var(--warn)'
  if (props.tone === 'danger') return 'var(--danger)'
  return 'var(--accent)'
})

const width = computed(() => Math.max(0, Math.min(100, props.pct ?? 0)))
</script>

<template>
  <div class="kpi">
    <div class="kpi-label">{{ label }}</div>
    <div class="kpi-value">
      {{ value }}<small v-if="suffix">{{ suffix }}</small>
    </div>
    <div class="kpi-foot">{{ foot }}</div>
    <div v-if="pct !== null" class="kpi-bar">
      <i :style="{ width: width + '%', background: barColor }" />
    </div>
  </div>
</template>
