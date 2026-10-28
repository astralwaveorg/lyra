<script setup lang="ts">
/**
 * 待办 · 统一收件箱。
 *
 * 六类队列按「该先看什么」排序；歌词与孤儿复用完整的审查面板（批量、
 * 对比、来源切换都在），封面/归置/失败是轻量列表——它们的处理动作
 * 是「打开详情抽屉」或「跳自动化/整理页」。
 *
 * 页面标题的 count 是六类总数；每个页签再带自己的计数。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { TodoItem } from '@/api/types'
import { useMeta } from '@/stores/meta'
import {
  issueLabel,
} from '@/utils/labels'
import PageHead from '@/components/PageHead.vue'
import EmptyState from '@/components/EmptyState.vue'
import AppIcon from '@/components/AppIcon.vue'
import TrackFixDrawer from '@/components/TrackFixDrawer.vue'
import LyricsQueue from '@/components/queues/LyricsQueue.vue'
import TagsQueue from '@/components/queues/TagsQueue.vue'

type Kind = 'lyrics' | 'tags' | 'covers' | 'organize' | 'orphans' | 'failed'

const meta = useMeta()
const route = useRoute()
const router = useRouter()

const KINDS: { key: Kind; label: string }[] = [
  { key: 'lyrics', label: '歌词' },
  { key: 'tags', label: '标签' },
  { key: 'covers', label: '封面' },
  { key: 'organize', label: '归置' },
  { key: 'orphans', label: '孤儿' },
  { key: 'failed', label: '失败' },
]

const requested = typeof route.query.kind === 'string' ? route.query.kind : ''
const kind = ref<Kind>(
  (KINDS.find((k) => k.key === requested)?.key ?? 'lyrics') as Kind,
)
/** 歌词面板的内部页签（/orphans 旧地址 deep-link 用）。 */
const lyricsTab =
  typeof route.query.tab === 'string'
    ? (route.query.tab as 'safe' | 'pending' | 'ignored' | 'orphans')
    : undefined

const items = ref<TodoItem[]>([])
const loading = ref(false)
const drawerOpen = ref(false)
const drawerPath = ref('')
const drawerRoot = ref('')
const busyPath = ref('')

const counts = computed(() => meta.todoCounts)
const total = computed(() => meta.todoTotal)

function countOf(key: Kind): number {
  return counts.value[key] ?? 0
}

async function loadItems() {
  if (kind.value === 'lyrics' || kind.value === 'tags' || kind.value === 'orphans') {
    // 这三类由内嵌面板自行加载
    return
  }
  loading.value = true
  try {
    const page = await api.todo(kind.value, 1, 100)
    items.value = page.items
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '待办加载失败')
  } finally {
    loading.value = false
  }
}

watch(kind, () => {
  void router.replace({ query: { ...route.query, kind: kind.value } })
  void loadItems()
})

async function refreshAll() {
  await meta.refresh().catch(() => undefined)
  await loadItems()
}

function openDetail(item: TodoItem) {
  drawerRoot.value = String(item.root ?? '')
  drawerPath.value = String(item.rel_path ?? '')
  drawerOpen.value = true
}

function issuesOf(item: TodoItem): string[] {
  const raw = item.issues
  return Array.isArray(raw) ? (raw as string[]) : []
}

function detailText(item: TodoItem): string {
  const detail = item.detail as Record<string, unknown> | undefined
  if (!detail) return ''
  if (typeof detail.note === 'string') return detail.note
  if (typeof detail.conflict === 'string') return `目标已存在：${detail.conflict}`
  if (typeof detail.target === 'string') return `建议位置：${detail.target}`
  return ''
}

async function reingest(item: TodoItem) {
  const rel = String(item.rel_path ?? '')
  if (!rel) return
  busyPath.value = rel
  try {
    await api.startIngest({ root: String(item.root ?? ''), paths: [rel] })
    ElMessage.success('已重新入库，可在自动化页查看进度')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '重新入库失败')
  } finally {
    busyPath.value = ''
  }
}

onMounted(() => {
  void meta.refresh().catch(() => undefined)
  void loadItems()
})
</script>

<template>
  <PageHead :count="total">
    <el-button :loading="loading" @click="refreshAll">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
    </el-button>
  </PageHead>

  <el-tabs v-model="kind" class="todo-tabs">
    <el-tab-pane
      v-for="k in KINDS"
      :key="k.key"
      :name="k.key"
    >
      <template #label>
        <span class="tab-label">
          {{ k.label }}
          <span v-if="countOf(k.key)" class="tab-count num">{{ countOf(k.key) }}</span>
        </span>
      </template>

      <!-- 歌词与孤儿：复用完整的审查面板（批量采用/对比/来源切换/手动刮削） -->
      <LyricsQueue v-if="kind === 'lyrics'" :initial-tab="lyricsTab" />
      <LyricsQueue v-else-if="kind === 'orphans'" initial-tab="orphans" />
      <TagsQueue v-else-if="kind === 'tags'" />

      <!-- 封面：缺封面清单 + 批量补封面的入口 -->
      <template v-else-if="kind === 'covers'">
        <div class="pane-tip">
          按目录批量补封面（同目录图片与封面档案站）在「自动化」页；下面的清单
          可以逐个打开详情手工处理。
          <router-link class="tip-link" to="/automation">去自动化页</router-link>
        </div>
        <el-skeleton v-if="loading && !items.length" animated :rows="5" />
        <EmptyState
          v-else-if="!items.length"
          title="没有缺封面的曲目"
          hint="曲库里的封面都很完整。"
          icon="PictureFilled"
        />
        <div v-else class="row-list">
          <div v-for="item in items" :key="item.ref" class="row">
            <div class="row-main">
              <span class="path ellipsis">{{ item.rel_path }}</span>
              <span class="chips">
                <el-tag
                  v-for="code in issuesOf(item).slice(0, 2)"
                  :key="code"
                  size="small"
                  effect="plain"
                >
                  {{ issueLabel(code) }}
                </el-tag>
              </span>
            </div>
            <div class="row-actions">
              <el-button size="small" text @click="openDetail(item)">详情</el-button>
            </div>
          </div>
        </div>
      </template>

      <!-- 归置：机器给出的归位建议与冲突 -->
      <template v-else-if="kind === 'organize'">
        <div class="pane-tip">
          归置模式默认不动任何文件；存量曲库的批量整理在「曲库整理」页预览后执行。
          <router-link class="tip-link" to="/organize">去整理页</router-link>
        </div>
        <el-skeleton v-if="loading && !items.length" animated :rows="5" />
        <EmptyState
          v-else-if="!items.length"
          title="没有待处理的归置项"
          hint="开启自动归位后，新入库文件的处理结果会出现在这里。"
          icon="Sort"
        />
        <div v-else class="row-list">
          <div v-for="item in items" :key="item.ref" class="row">
            <div class="row-main">
              <span class="path ellipsis">{{ item.rel_path }}</span>
              <span class="detail ellipsis">{{ detailText(item) }}</span>
            </div>
          </div>
        </div>
      </template>

      <!-- 失败：写入失败与任务失败，逐条重跑 -->
      <template v-else>
        <div class="pane-tip">
          写入失败的任务会留在这里；网络类失败可以直接重试，格式问题请打开详情查看。
        </div>
        <el-skeleton v-if="loading && !items.length" animated :rows="5" />
        <EmptyState
          v-else-if="!items.length"
          title="没有失败项"
          hint="一切正常。写入或任务失败时会出现在这里。"
          icon="CircleCheck"
        />
        <div v-else class="row-list">
          <div v-for="item in items" :key="item.ref" class="row">
            <div class="row-main">
              <span class="path ellipsis">{{ item.rel_path }}</span>
              <span class="detail ellipsis">
                {{ item.state === 'failed' ? '写入失败' : '任务失败' }}
                <template v-if="item.provider"> · 来源 {{ item.provider }}</template>
              </span>
            </div>
            <div class="row-actions">
              <el-button size="small" text @click="openDetail(item)">详情</el-button>
              <el-button
                size="small"
                type="primary"
                plain
                :loading="busyPath === item.rel_path"
                @click="reingest(item)"
              >
                重新入库
              </el-button>
            </div>
          </div>
        </div>
      </template>
    </el-tab-pane>
  </el-tabs>

  <TrackFixDrawer
    v-model="drawerOpen"
    :root="drawerRoot"
    :path="drawerPath"
    tab="cover"
    @refresh="refreshAll"
  />
</template>

<style scoped>
.todo-tabs :deep(.el-tabs__header) {
  margin-bottom: 14px;
}
.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.tab-count {
  display: inline-block;
  min-width: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--el-color-danger);
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
}
.pane-tip {
  margin-bottom: 12px;
  padding: 9px 13px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.tip-link {
  margin-left: 6px;
  color: var(--el-color-primary);
  text-decoration: none;
}
.row-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}
.row-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  flex: 1;
}
.path {
  font-size: 13.5px;
  font-weight: 500;
}
.detail {
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
}
.chips {
  display: inline-flex;
  gap: 6px;
}
.row-actions {
  flex-shrink: 0;
  display: inline-flex;
  gap: 4px;
}
.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
