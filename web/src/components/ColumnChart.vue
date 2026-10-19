<script setup lang="ts">
import { computed } from 'vue'

/**
 * 用 DOM 而不是 SVG 画柱状图：
 * SVG 要么被拉伸变形，要么在高 DPI 下靠 viewBox 缩放导致文字糊；
 * 柱状图本来就是一维的，div 的高度百分比是最直接也最稳的实现。
 */
const props = withDefaults(
  defineProps<{
    items: { label: string; value: number }[]
    height?: number
    unit?: string
  }>(),
  { height: 140, unit: '' },
)

const peak = computed(() => Math.max(1, ...props.items.map((item) => item.value)))

const bars = computed(() =>
  props.items.map((item, index) => ({
    ...item,
    pct: Math.max(1.5, (item.value / peak.value) * 100),
    color: `var(--c${(index % 8) + 1})`,
    // 标签太密时隔几个显示一次，避免互相压字
    showLabel:
      props.items.length <= 9 || index % Math.ceil(props.items.length / 9) === 0,
  })),
)
</script>

<template>
  <div class="columns" :style="{ height: height + 22 + 'px' }">
    <div v-for="(bar, index) in bars" :key="index" class="col" :title="`${bar.label}：${bar.value}${unit}`">
      <div class="col-plot" :style="{ height: height + 'px' }">
        <div class="col-bar" :style="{ height: bar.pct + '%', background: bar.color }" />
      </div>
      <div class="col-label">{{ bar.showLabel ? bar.label : '' }}</div>
    </div>
  </div>
</template>

<style scoped>
.columns {
  display: flex;
  align-items: flex-end;
  gap: 3px;
}

.col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.col-plot {
  width: 100%;
  display: flex;
  align-items: flex-end;
  border-bottom: 1px solid var(--border);
  padding: 0 1px;
}

.col-bar {
  width: 100%;
  border-radius: 3px 3px 0 0;
  min-height: 2px;
  opacity: 0.92;
  transition: height 0.3s ease;
}

.col-label {
  font-size: 10px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  max-width: 100%;
  height: 12px;
  line-height: 12px;
}
</style>
