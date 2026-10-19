<script setup lang="ts">
import { computed } from 'vue'

/**
 * 累计进度折线。
 *
 * SVG 只负责路径（`preserveAspectRatio="none"` + `vector-effect` 保证线宽不被拉伸），
 * 刻度文字全部用 DOM 渲染——否则文字会跟着 viewBox 一起变形。
 */
const props = withDefaults(
  defineProps<{
    points?: { label: string; value: number }[]
    series?: { name: string; color: string; points: { label: string; value: number }[] }[]
    height?: number
  }>(),
  { points: () => [], series: () => [], height: 150 },
)

const sets = computed(() => {
  const list =
    props.series.length > 0
      ? props.series
      : [{ name: '', color: 'var(--c1)', points: props.points ?? [] }]
  return list.filter((set) => set.points.length > 0)
})

const peak = computed(() => Math.max(1, ...sets.value.flatMap((s) => s.points.map((p) => p.value))))

function polyline(set: { points: { value: number }[] }): string {
  const n = set.points.length
  if (n === 0) return ''
  if (n === 1) return `0,${100 - (set.points[0].value / peak.value) * 96} 100,${
    100 - (set.points[0].value / peak.value) * 96
  }`
  return set.points
    .map((point, index) => {
      const x = (index / (n - 1)) * 100
      const y = 100 - (point.value / peak.value) * 96
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
}

function area(set: { points: { value: number }[] }): string {
  const line = polyline(set)
  if (!line) return ''
  return `0,100 ${line} 100,100`
}

const firstLabel = computed(() => sets.value[0]?.points[0]?.label ?? '')
const lastLabel = computed(() => {
  const points = sets.value[0]?.points ?? []
  return points.length ? points[points.length - 1].label : ''
})
const lastValue = computed(() => {
  const points = sets.value[0]?.points ?? []
  return points.length ? points[points.length - 1].value : 0
})
</script>

<template>
  <div class="line-wrap">
    <div class="line-canvas" :style="{ height: height + 'px' }">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" class="line-svg">
        <line v-for="n in 4" :key="n" x1="0" :y1="n * 25" x2="100" :y2="n * 25" stroke="var(--border)" stroke-width="1" vector-effect="non-scaling-stroke" />
        <g v-for="(set, index) in sets" :key="index">
          <polygon
            v-if="index === 0"
            :points="area(set)"
            fill="var(--accent)"
            opacity="0.12"
          />
          <polyline
            :points="polyline(set)"
            fill="none"
            :stroke="set.color"
            stroke-width="1.6"
            stroke-linejoin="round"
            stroke-linecap="round"
            vector-effect="non-scaling-stroke"
          />
        </g>
      </svg>
      <span class="line-peak num">{{ peak }}</span>
    </div>
    <div class="line-axis">
      <span>{{ firstLabel }}</span>
      <span class="dim">累计 {{ lastValue }}</span>
      <span>{{ lastLabel }}</span>
    </div>
    <div v-if="sets.length > 1" class="line-legend">
      <span v-for="set in sets" :key="set.name">
        <i :style="{ background: set.color }" />{{ set.name }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.line-wrap {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.line-canvas {
  position: relative;
  width: 100%;
}

.line-svg {
  width: 100%;
  height: 100%;
  display: block;
  overflow: visible;
}

.line-peak {
  position: absolute;
  top: -2px;
  left: 0;
  font-size: 10px;
  color: var(--text-3);
  transform: translateY(-100%);
}

.line-axis {
  display: flex;
  justify-content: space-between;
  font-size: 10.5px;
  color: var(--text-3);
}

.line-legend {
  display: flex;
  gap: 14px;
  font-size: 11.5px;
  color: var(--text-2);
}

.line-legend i {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 3px;
  margin-right: 6px;
}
</style>
