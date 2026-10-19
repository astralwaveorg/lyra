<script setup lang="ts">
import { computed } from 'vue'
import type { LyricLine } from '@/utils/lyrics'

const props = withDefaults(
  defineProps<{
    lines: LyricLine[]
    empty?: string
    /** 显示时间轴列 */
    stamps?: boolean
    maxHeight?: string
  }>(),
  { empty: '没有解析出歌词行', stamps: true, maxHeight: '62vh' },
)

const rows = computed(() => props.lines.filter((line) => line.text.trim().length > 0))
</script>

<template>
  <div class="lyrics" :style="{ maxHeight }">
    <div v-if="!rows.length" class="dim">{{ empty }}</div>
    <div v-for="(line, index) in rows" :key="index" class="lyrics-line">
      <span v-if="stamps" class="lyrics-time">{{ line.stamp }}</span>
      <span class="lyrics-text" :class="{ 'lyrics-meta': !line.text.trim() }">{{ line.text }}</span>
    </div>
  </div>
</template>
