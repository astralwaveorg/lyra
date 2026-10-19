<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import type { StatsResponse } from '@/api/types'
import { useMeta } from '@/stores/meta'
import { fmtNumber, fmtPct, fmtSpan } from '@/utils/format'
import { tagFieldLabel } from '@/utils/labels'
import PageHead from '@/components/PageHead.vue'
import KpiCard from '@/components/KpiCard.vue'
import DonutChart from '@/components/DonutChart.vue'
import BarList from '@/components/BarList.vue'
import ColumnChart from '@/components/ColumnChart.vue'
import LineChart from '@/components/LineChart.vue'
import EmptyState from '@/components/EmptyState.vue'
import AppIcon from '@/components/AppIcon.vue'

const router = useRouter()
const meta = useMeta()

const stats = ref<StatsResponse | null>(null)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')

/** 多受管根合并：数量相加，比率按曲目数加权，避免「小库拉低大库」。 */
const agg = computed(() => {
  const roots = stats.value?.roots ?? []
  const sum = (pick: (root: (typeof roots)[number]) => number) =>
    roots.reduce((total, root) => total + (pick(root) || 0), 0)
  const audio = sum((r) => r.audio_total)
  const weighted = (pick: (root: (typeof roots)[number]) => number) =>
    audio > 0 ? sum((r) => (pick(r) || 0) * r.audio_total) / audio : 0

  const tagMap = new Map<string, number>()
  for (const root of roots) {
    for (const row of root.tag_coverage ?? []) {
      tagMap.set(row.name, (tagMap.get(row.name) ?? 0) + row.count)
    }
  }

  return {
    audio,
    seconds: sum((r) => r.total_seconds),
    albums: sum((r) => r.album_total),
    genres: sum((r) => r.genre_total),
    artists: sum((r) => r.artist_total),
    sidecarPct: weighted((r) => r.sidecar_coverage_pct),
    embeddedPct: weighted((r) => r.embedded_coverage_pct),
    coverPct: weighted((r) => r.cover_pct),
    sidecarTimed: sum((r) => r.sidecar_timed),
    sidecarUntimed: sum((r) => r.sidecar_untimed),
    sidecarMissingEmbedded: sum((r) => r.sidecar_missing_embedded),
    sidecarMissingNone: sum((r) => r.sidecar_missing_none),
    coverTotal: sum((r) => r.cover_total),
    embeddedSynced: sum((r) => r.embedded_synced),
    embeddedOnly: sum((r) => r.sidecar_missing_embedded),
    genresTop: mergeTop(roots.map((r) => r.genres ?? []), 8),
    albumsTop: mergeTop(roots.map((r) => r.albums ?? []), 8),
    artistsTop: mergeTop(roots.map((r) => r.artists ?? []), 8),
    decades: mergeTop(roots.map((r) => r.decades ?? []), 12).sort((a, b) =>
      String(a.label).localeCompare(String(b.label)),
    ),
    // 时长分桶**必须保持后端顺序**（<1 分 → 9 分以上），
    // 按数量排序会把直方图排成乱序，看起来像数据错了。
    durations: mergeByLabel(roots.map((r) => r.duration_histogram ?? [])),
    tags: [...tagMap.entries()]
      .map(([name, count]) => ({
        name,
        count,
        pct: audio > 0 ? (count / audio) * 100 : 0,
      }))
      .sort((a, b) => b.pct - a.pct),
  }
})

function mergeByLabel(
  lists: { name: string; count: number }[][],
): { label: string; value: number }[] {
  const order: string[] = []
  const totals = new Map<string, number>()
  for (const list of lists) {
    for (const item of list) {
      const name = String(item.name)
      if (!totals.has(name)) {
        totals.set(name, 0)
        order.push(name)
      }
      totals.set(name, (totals.get(name) ?? 0) + item.count)
    }
  }
  return order.map((label) => ({ label, value: totals.get(label) ?? 0 }))
}

function mergeTop(
  lists: { name: string; count: number }[][],
  limit: number,
): { label: string; value: number }[] {
  const map = new Map<string, number>()
  for (const list of lists) {
    for (const item of list) map.set(String(item.name), (map.get(String(item.name)) ?? 0) + item.count)
  }
  return [...map.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value)
    .slice(0, limit)
}

/** 流派覆盖率：与「标签完整度」用的是同一份数据，口径必须一致。 */
const genreRow = computed(() => agg.value.tags.find((row) => row.name === 'GENRE'))
const genrePct = computed(() => genreRow.value?.pct ?? 0)

const coverageItems = computed(() => [
  { label: '有时间轴', value: agg.value.sidecarTimed, color: 'var(--c2)' },
  { label: '无时间轴', value: agg.value.sidecarUntimed, color: 'var(--c3)' },
  { label: '可从内嵌补', value: agg.value.sidecarMissingEmbedded, color: 'var(--c4)' },
  { label: '都没有', value: agg.value.sidecarMissingNone, color: 'var(--c8)' },
])

const tagItems = computed(() =>
  agg.value.tags.slice(0, 10).map((row) => ({
    label: tagFieldLabel(row.name),
    value: Math.round(row.pct * 10) / 10,
    color:
      row.pct >= 95 ? 'var(--c2)' : row.pct >= 80 ? 'var(--c4)' : row.pct >= 50 ? 'var(--c3)' : 'var(--c8)',
  })),
)

const history = computed(() => meta.summary?.coverage_history ?? [])

const reviewCount = computed(() => meta.summary?.counts.needs_review ?? 0)
const pendingCount = computed(() => meta.summary?.counts.staged_pending ?? 0)

const todos = computed(() => [
  {
    label: '歌词审查',
    value: reviewCount.value,
    hint: '推荐采纳可直接确认，其余逐首判断',
    to: '/review',
  },
  { label: '待写入', value: pendingCount.value, hint: '已确认，等待写入文件', to: '/scrape' },
  { label: '标签与封面问题', value: meta.issueTotal, hint: '在音乐库里筛选后可逐项处理', to: '/library' },
])

async function load(refresh = false) {
  if (refresh) refreshing.value = true
  else loading.value = true
  error.value = ''
  try {
    if (refresh) {
      stats.value = await api.stats(true)
    } else {
      const [statsData] = await Promise.all([
        api.stats(false),
        meta.summary ? Promise.resolve(null) : meta.refresh(),
      ])
      stats.value = statsData
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

onMounted(async () => {
  await Promise.all([load(false), meta.refresh().catch(() => undefined)])
})
</script>

<template>
  <PageHead>
    <el-button :loading="refreshing" @click="load(true)">
      <AppIcon name="Refresh" :size="14" style="margin-right: 5px" />重新统计
    </el-button>
  </PageHead>

  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />

  <template v-if="loading">
    <div class="loading-hint dim">
      <span class="spinner" />
      正在统计曲库，首次需要扫描全部文件
    </div>
    <div class="kpi-grid">
      <el-skeleton v-for="n in 7" :key="n" animated>
        <template #template>
          <el-skeleton-item variant="rect" style="height: 104px; border-radius: 14px" />
        </template>
      </el-skeleton>
    </div>
    <el-skeleton animated :rows="6" />
  </template>

  <template v-else-if="agg.audio > 0">
    <div class="kpi-grid">
      <KpiCard label="音乐总数" :value="fmtNumber(agg.audio)" suffix="首" :foot="`${fmtNumber(stats?.roots.length ?? 0)} 个受管目录`" />
      <KpiCard label="曲库时长" :value="fmtSpan(agg.seconds)" :foot="`平均 ${fmtSpan(agg.seconds / Math.max(1, agg.audio))}`" />
      <KpiCard label="专辑 / 流派" :value="fmtNumber(agg.albums)" suffix="张" :foot="`${agg.genres} 个流派`" />
      <KpiCard label="艺术家" :value="fmtNumber(agg.artists)" suffix="位" foot="按署名统计" />
      <KpiCard
        label="歌词文件覆盖"
        :value="fmtPct(agg.sidecarPct)"
        :pct="agg.sidecarPct"
        tone="ok"
        :foot="`${fmtNumber(agg.sidecarTimed)} 首带时间轴`"
      />
      <KpiCard
        label="内嵌歌词覆盖"
        :value="fmtPct(agg.embeddedPct)"
        :pct="agg.embeddedPct"
        :foot="`${fmtNumber(agg.embeddedSynced)} 首内嵌有时间轴`"
      />
      <KpiCard
        label="流派覆盖"
        :value="fmtPct(genrePct)"
        :pct="genrePct"
        :tone="genrePct >= 95 ? 'ok' : 'warn'"
        :foot="`${fmtNumber(genreRow?.count ?? 0)} 首有流派标签`"
      />
      <KpiCard
        label="封面完整"
        :value="fmtPct(agg.coverPct)"
        :pct="agg.coverPct"
        :tone="agg.coverPct >= 95 ? 'ok' : 'warn'"
        :foot="`${fmtNumber(agg.coverTotal)} 首有内嵌封面`"
      />
    </div>

    <div class="grid-2">
      <section class="card">
        <div class="card-head">
          <span class="card-title">.lrc 覆盖构成</span>
          <span class="card-hint">{{ fmtNumber(agg.audio) }} 首</span>
        </div>
        <div class="card-body">
          <DonutChart
            :items="coverageItems"
            :center="fmtPct(agg.sidecarPct)"
            sub="带时间轴"
          />
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">标签完整度</span>
          <span class="card-hint">占全部曲目</span>
        </div>
        <div class="card-body">
          <BarList :items="tagItems" :max="100" unit="%" />
        </div>
      </section>
    </div>

    <div class="grid-2">
      <section class="card">
        <div class="card-head"><span class="card-title">流派分布</span></div>
        <div class="card-body">
          <BarList :items="agg.genresTop" :show-rank="true" />
        </div>
      </section>
      <section class="card">
        <div class="card-head"><span class="card-title">时长分布</span><span class="card-hint">首</span></div>
        <div class="card-body">
          <ColumnChart :items="agg.durations" :height="140" />
        </div>
      </section>
    </div>

    <div class="grid-2">
      <section class="card">
        <div class="card-head"><span class="card-title">专辑 Top 8</span></div>
        <div class="card-body">
          <BarList :items="agg.albumsTop" :show-rank="true" />
        </div>
      </section>
      <section class="card">
        <div class="card-head"><span class="card-title">艺术家 Top 8</span></div>
        <div class="card-body">
          <BarList :items="agg.artistsTop" :show-rank="true" />
        </div>
      </section>
    </div>

    <div class="grid-2">
      <section class="card">
        <div class="card-head"><span class="card-title">写入进度</span><span class="card-hint">近 60 天累计</span></div>
        <div class="card-body">
          <LineChart
            v-if="history.length"
            :series="[
              {
                name: '已写入',
                color: 'var(--c2)',
                points: history.map((row) => ({ label: row.date, value: row.written })),
              },
              {
                name: '进入复核',
                color: 'var(--c3)',
                points: history.map((row) => ({ label: row.date, value: row.reviewed })),
              },
            ]"
            :height="150"
          />
          <div v-else class="dim">还没有写入记录</div>
        </div>
      </section>

      <section class="card">
        <div class="card-head"><span class="card-title">待处理</span></div>
        <div class="card-body flush">
          <div v-for="todo in todos" :key="todo.label" class="todo-row" @click="router.push(todo.to)">
            <div>
              <div class="todo-label">{{ todo.label }}</div>
              <div class="cell-meta">{{ todo.hint }}</div>
            </div>
            <div class="todo-value num">{{ fmtNumber(todo.value) }}</div>
            <AppIcon name="CaretRight" :size="13" class="dim" />
          </div>
        </div>
      </section>
    </div>
  </template>

  <EmptyState
    v-else
    title="还没有可统计的曲目"
    hint="确认受管目录已挂载并完成一次扫描后再回来。"
  />
</template>

<style scoped>
.loading-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}

.spinner {
  width: 12px;
  height: 12px;
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

.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 14px;
}

.todo-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background 0.15s;
}

.todo-row:last-child {
  border-bottom: 0;
}

.todo-row:hover {
  background: var(--surface-2);
}

.todo-row > div:first-child {
  flex: 1;
  min-width: 0;
}

.todo-label {
  font-size: 13px;
  font-weight: 550;
}

.todo-value {
  font-size: 18px;
  font-weight: 650;
}

@media (max-width: 720px) {
  .loading-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}

.spinner {
  width: 12px;
  height: 12px;
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

.grid-2 {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
