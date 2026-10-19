<script setup lang="ts">
import { computed } from 'vue'
import { providerLabel } from '@/utils/labels'

const props = defineProps<{ name: string; muted?: boolean }>()

const short = computed(() => {
  const map: Record<string, string> = { qq: 'QQ', netease: '网易', lrclib: 'LRCLIB', acoustid: 'AcoustID' }
  return map[props.name] ?? providerLabel(props.name)
})
</script>

<template>
  <span class="provider" :class="{ muted }" :title="providerLabel(name)">{{ short }}</span>
</template>

<style scoped>
.provider {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border-radius: 5px;
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-size: 11px;
  font-weight: 550;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.provider.muted {
  background: var(--surface-3);
  color: var(--text-3);
}
</style>
