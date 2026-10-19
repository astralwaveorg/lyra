<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    items: { label: string; value: number; color?: string }[]
    center?: string
    sub?: string
    thickness?: number
  }>(),
  { center: '', sub: '', thickness: 13 },
)

const RADIUS = 39
const CIRC = 2 * Math.PI * RADIUS

const total = computed(() => props.items.reduce((sum, item) => sum + Math.max(0, item.value), 0))

const segments = computed(() => {
  let offset = 0
  return props.items
    .filter((item) => item.value > 0)
    .map((item) => {
      const length = total.value ? (item.value / total.value) * CIRC : 0
      const segment = { ...item, length, offset }
      offset += length
      return segment
    })
})
</script>

<template>
  <div class="donut-wrap">
    <svg viewBox="0 0 100 100" class="donut" role="img">
      <g transform="rotate(-90 50 50)">
        <circle cx="50" cy="50" :r="RADIUS" fill="none" stroke="var(--surface-3)" :stroke-width="thickness" />
        <circle
          v-for="(seg, index) in segments"
          :key="index"
          cx="50"
          cy="50"
          :r="RADIUS"
          fill="none"
          :stroke="seg.color ?? `var(--c${(index % 8) + 1})`"
          :stroke-width="thickness"
          :stroke-dasharray="`${seg.length} ${CIRC - seg.length}`"
          :stroke-dashoffset="-seg.offset"
        />
      </g>
      <text x="50" y="49" text-anchor="middle" class="donut-center">{{ center }}</text>
      <text x="50" y="60" text-anchor="middle" class="donut-sub">{{ sub }}</text>
    </svg>
    <ul class="legend">
      <li v-for="(item, index) in items" :key="item.label">
        <i :style="{ background: item.color ?? `var(--c${(index % 8) + 1})` }" />
        <span class="legend-label">{{ item.label }}</span>
        <span class="legend-value num">{{ item.value }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.donut-wrap {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
}

.donut {
  width: 148px;
  height: 148px;
  flex: none;
}

.donut-center {
  fill: var(--text);
  font-size: 15px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.donut-sub {
  fill: var(--text-3);
  font-size: 6.5px;
  letter-spacing: 0.06em;
}

.legend {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
  flex: 1;
  min-width: 140px;
}

.legend li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-2);
}

.legend i {
  width: 8px;
  height: 8px;
  border-radius: 3px;
  flex: none;
}

.legend-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.legend-value {
  color: var(--text);
  font-weight: 550;
}
</style>
