<script setup lang="ts">
/**
 * 页面头（现在只剩「数量」和「操作」两样东西）。
 *
 * 标题由顶栏根据路由渲染——页面里再写一遍就是重复，而且会让人疑惑
 * 「到底哪个才是这一页的标题」。所以这个组件把内容 teleport 到顶栏：
 *   #page-count   标题右侧的数量徽标
 *   #page-actions 顶栏右侧的操作按钮
 *
 * `defer` 让 teleport 在挂载后再解析目标，避免顶栏尚未渲染时报警告。
 */
defineProps<{ count?: string | number | null }>()
</script>

<template>
  <Teleport to="#page-count" defer>
    <span v-if="count !== undefined && count !== null" class="head-count num">{{ count }}</span>
  </Teleport>
  <Teleport to="#page-actions" defer>
    <slot />
  </Teleport>
</template>

<style scoped>
.head-count {
  font-size: 12px;
  color: var(--text-3);
  padding: 2px 9px;
  border-radius: 999px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
}
</style>
