<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    items: { label: string; value: number; hint?: string; color?: string }[]
    max?: number
    unit?: string
    showRank?: boolean
  }>(),
  { max: 0, unit: '', showRank: false },
)

const peak = computed(() => {
  if (props.max > 0) return props.max
  return Math.max(1, ...props.items.map((item) => item.value))
})

const rows = computed(() =>
  props.items.map((item, index) => ({
    ...item,
    pct: Math.max(2, Math.round((item.value / peak.value) * 100)),
    color: item.color ?? `var(--c${(index % 8) + 1})`,
  })),
)
</script>

<template>
  <div class="bars">
    <div v-for="(row, index) in rows" :key="row.label" class="bar-row">
      <span v-if="showRank" class="rank num">{{ index + 1 }}</span>
      <span class="label ellipsis" :title="row.label">{{ row.label }}</span>
      <span class="track">
        <i :style="{ width: row.pct + '%', background: row.color }" />
      </span>
      <span class="value num">{{ row.value }}{{ unit }}</span>
    </div>
    <div v-if="!rows.length" class="dim">暂无数据</div>
  </div>
</template>

<style scoped>
.bars {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.bar-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
}

.rank {
  width: 16px;
  color: var(--text-3);
  font-size: 11px;
  text-align: right;
  flex: none;
}

.label {
  width: 116px;
  flex: none;
  color: var(--text-2);
}

.track {
  flex: 1;
  height: 6px;
  border-radius: 999px;
  background: var(--surface-3);
  overflow: hidden;
  min-width: 40px;
}

.track i {
  display: block;
  height: 100%;
  border-radius: 999px;
  transition: width 0.3s ease;
}

.value {
  width: 44px;
  text-align: right;
  color: var(--text);
  font-weight: 550;
  flex: none;
}

@media (max-width: 560px) {
  .label {
    width: 88px;
  }
}
</style>
