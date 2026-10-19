<script setup lang="ts">
import { useRouter } from 'vue-router'
import EmptyState from './EmptyState.vue'

defineProps<{ hint: string; points: string[]; actionLabel?: string; to?: string }>()

const router = useRouter()
</script>

<template>
  <section class="card">
    <div class="card-body">
      <EmptyState title="功能开发中" :hint="hint" icon="MagicStick">
        <ul class="points">
          <li v-for="point in points" :key="point">{{ point }}</li>
        </ul>
        <el-button v-if="actionLabel && to" size="small" @click="router.push(to)">
          {{ actionLabel }}
        </el-button>
      </EmptyState>
    </div>
  </section>
</template>

<style scoped>
.points {
  margin: 6px auto 0;
  width: fit-content;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12.5px;
  color: var(--text-3);
  text-align: left;
}

.points li::before {
  content: '·';
  margin-right: 6px;
  color: var(--text-3);
}
</style>
