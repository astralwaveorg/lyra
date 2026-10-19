<script setup lang="ts">
import AppIcon from './AppIcon.vue'

withDefaults(
  defineProps<{ title: string; hint?: string; icon?: string }>(),
  { hint: '', icon: 'InfoFilled' },
)
</script>

<template>
  <div class="empty">
    <div class="empty-icon"><AppIcon :name="icon" :size="17" /></div>
    <div class="empty-title">{{ title }}</div>
    <div v-if="hint" class="empty-hint">{{ hint }}</div>
    <slot />
  </div>
</template>
