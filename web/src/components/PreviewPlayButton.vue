<script setup lang="ts">
import { onUnmounted } from 'vue'
import AppIcon from './AppIcon.vue'
import { useAudioPreview } from '@/composables/useAudioPreview'

const props = defineProps<{ root: string; path: string }>()

const { toggle, stop, isPlaying, isPending } = useAudioPreview()

// 离开页面时停掉声音：否则切到别的页面还在放。
onUnmounted(stop)
</script>

<template>
  <button
    class="play-btn"
    :class="{ on: isPlaying(props.root, props.path) }"
    :title="isPlaying(props.root, props.path) ? '停止试听' : '试听'"
    type="button"
    @click.stop="toggle(props.root, props.path)"
  >
    <span v-if="isPending(props.root, props.path)" class="spinner" />
    <AppIcon v-else :name="isPlaying(props.root, props.path) ? 'VideoPause' : 'VideoPlay'" :size="12" />
  </button>
</template>

<style scoped>
.play-btn {
  width: 24px;
  height: 24px;
  flex: none;
  display: inline-grid;
  place-items: center;
  padding: 0;
  border-radius: 50%;
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text-2);
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}

.play-btn:hover {
  color: var(--accent);
  border-color: var(--accent-line);
  background: var(--accent-soft);
}

.play-btn.on {
  color: var(--ok);
  border-color: var(--ok);
  background: var(--ok-soft);
}

.spinner {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid var(--border-strong);
  border-top-color: var(--accent);
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
