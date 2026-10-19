<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { Track, TrackPage } from '@/api/types'
import { useMeta } from '@/stores/meta'
import { useNarrow } from '@/composables/useMedia'
import { fmtBytes, splitPath } from '@/utils/format'
import { LYRIC_STATE, issueLabel, stateTone } from '@/utils/labels'
import PageHead from '@/components/PageHead.vue'
import IssueChip from '@/components/IssueChip.vue'
import EmptyState from '@/components/EmptyState.vue'
import TrackFixDrawer from '@/components/TrackFixDrawer.vue'
import ScrapeDialog from '@/components/ScrapeDialog.vue'
import AppIcon from '@/components/AppIcon.vue'

const meta = useMeta()
const narrow = useNarrow()

const query = ref('')
const issue = ref<string | undefined>()
const state = ref<string | undefined>()
const page = ref(1)
const size = ref(50)
const total = ref(0)
const items = ref<Track[]>([])
const loading = ref(false)
const error = ref('')
const issueKinds = ref<{ code: string; count: number }[]>([])

const drawerOpen = ref(false)
const drawerTab = ref<'tags' | 'cover' | 'lyrics' | 'file'>('tags')
const scrapeOpen = ref(false)
const active = ref<{ root: string; path: string } | null>(null)

const STATE_OPTIONS = [
  { value: 'synced', label: '合法歌词' },
  { value: 'plain_text', label: '无时间轴' },
  { value: 'synced_suspicious', label: '时间轴异常' },
  { value: 'absent', label: '无歌词' },
  { value: 'unparseable', label: '无法解析' },
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data: TrackPage = await api.tracks({
      q: query.value || undefined,
      issue: issue.value || undefined,
      state: state.value || undefined,
      page: page.value,
      size: size.value,
    })
    items.value = data.items
    total.value = data.total
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function applySearch() {
  page.value = 1
  void load()
}

watch([issue, state], () => {
  page.value = 1
  void load()
})
watch(page, load)

onMounted(async () => {
  await load()
  try {
    issueKinds.value = (await api.issueKinds()).kinds
  } catch {
    /* 下拉为空不影响列表 */
  }
})

function openFix(track: Track, tab?: 'tags' | 'cover' | 'lyrics' | 'file') {
  active.value = { root: track.root, path: track.rel_path }
  drawerTab.value = tab ?? routeFor(track.issues[0] ?? '')
  drawerOpen.value = true
}

function routeFor(code: string): 'tags' | 'cover' | 'lyrics' | 'file' {
  if (!code) return 'tags'
  if (/cover/.test(code)) return 'cover'
  if (/lyrics|sidecar/.test(code)) return 'lyrics'
  return 'tags'
}

function openScrape(track: Track) {
  active.value = { root: track.root, path: track.rel_path }
  scrapeOpen.value = true
}

async function afterChange() {
  await Promise.all([load(), meta.refresh().catch(() => undefined)])
  try {
    issueKinds.value = (await api.issueKinds()).kinds
  } catch {
    /* 忽略 */
  }
}

</script>

<template>
  <PageHead :count="total">
    <el-button :loading="loading" @click="load">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />刷新
    </el-button>
  </PageHead>

  <div class="filter-bar">
    <div class="filter-search">
      <el-input
        v-model="query"
        placeholder="搜索路径或曲名"
        clearable
        @keyup.enter="applySearch"
        @clear="applySearch"
      >
        <template #prefix><AppIcon name="Search" :size="14" /></template>
      </el-input>
      <el-button @click="applySearch">搜索</el-button>
    </div>
    <div class="filter-group">
      <span class="filter-label">过滤</span>
      <el-select v-model="issue" class="filter-select" placeholder="全部问题" clearable>
        <el-option
          v-for="kind in issueKinds"
          :key="kind.code"
          :label="`${issueLabel(kind.code)} · ${kind.count}`"
          :value="kind.code"
        />
      </el-select>
    </div>
    <div class="filter-group">
      <span class="filter-label">歌词</span>
      <el-select v-model="state" class="filter-select" placeholder="全部状态" clearable>
        <el-option
          v-for="option in STATE_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>
    </div>
  </div>

  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

  <section class="card">
    <el-table v-if="!narrow" v-loading="loading" :data="items" row-key="id">
      <el-table-column label="曲目" min-width="320">
        <template #default="{ row }">
          <div class="cell-stack">
            <span class="path">{{ splitPath(row.rel_path).name }}</span>
            <span class="cell-meta ellipsis">{{ splitPath(row.rel_path).dir }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="格式" width="76">
        <template #default="{ row }">
          <span class="cell-meta mono">
            {{ splitPath(row.rel_path).name.split('.').pop()?.toUpperCase() }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="大小" width="92">
        <template #default="{ row }">
          <span class="num cell-meta">{{ fmtBytes(row.size) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="歌词文件" width="106">
        <template #default="{ row }">
          <el-tag size="small" :type="stateTone(row.sidecar_state)">
            {{ row.sidecar_state ? LYRIC_STATE[row.sidecar_state] ?? row.sidecar_state : '无' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="内嵌歌词" width="112">
        <template #default="{ row }">
          <el-tag size="small" :type="stateTone(row.embedded_state)">
            {{ row.embedded_state ? LYRIC_STATE[row.embedded_state] ?? row.embedded_state : '无' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="问题" min-width="180">
        <template #default="{ row }">
          <div class="chips">
            <IssueChip
              v-for="code in row.issues.slice(0, 3)"
              :key="code"
              :code="code"
              clickable
              @pick="openFix(row)"
            />
            <span v-if="row.issues.length > 3" class="cell-meta">+{{ row.issues.length - 3 }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="" width="140" align="right">
        <template #default="{ row }">
          <div class="row-actions">
            <el-button size="small" class="row-btn accent" @click="openScrape(row)">刮削</el-button>
            <el-button size="small" class="row-btn" @click="openFix(row)">详情</el-button>
          </div>
        </template>
      </el-table-column>
      <template #empty>
        <EmptyState title="没有符合条件的曲目" hint="换个筛选条件，或先在刮削页运行一次扫描。" icon="Headset" />
      </template>
    </el-table>

    <div v-else class="cards">
      <div v-for="track in items" :key="track.id" class="track-card">
        <div class="track-card-head">
          <span class="path ellipsis">{{ splitPath(track.rel_path).name }}</span>
          <span class="cell-meta num">{{ fmtBytes(track.size) }}</span>
        </div>
        <div class="cell-meta ellipsis">{{ splitPath(track.rel_path).dir }}</div>
        <div class="track-card-tags">
          <el-tag size="small" :type="stateTone(track.sidecar_state)">
            歌词文件 {{ track.sidecar_state ? LYRIC_STATE[track.sidecar_state] ?? track.sidecar_state : '无' }}
          </el-tag>
          <IssueChip v-for="code in track.issues.slice(0, 2)" :key="code" :code="code" />
        </div>
        <div class="track-card-actions">
          <el-button size="small" class="row-btn accent" @click="openScrape(track)">刮削</el-button>
          <el-button size="small" class="row-btn" @click="openFix(track)">详情</el-button>
        </div>
      </div>
      <EmptyState v-if="!items.length" title="没有符合条件的曲目" icon="Headset" />
    </div>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        :page-size="size"
        :total="total"
        layout="total, prev, pager, next"
        background
      />
    </div>
  </section>

  <TrackFixDrawer
    v-if="active"
    v-model="drawerOpen"
    :root="active.root"
    :path="active.path"
    :tab="drawerTab"
    @refresh="afterChange"
  />

  <ScrapeDialog v-if="active" v-model="scrapeOpen" :root="active.root" :path="active.path" @done="afterChange" />
</template>

<style scoped>

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}

.pager {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
}

.cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
}

.track-card {
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: var(--surface-2);
}

.track-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.track-card-head .path {
  flex: 1;
  min-width: 0;
  font-weight: 550;
}

.track-card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 2px;
}

.track-card-actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
  margin-top: 4px;
}

</style>
