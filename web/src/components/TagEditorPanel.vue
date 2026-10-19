<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { FileMeta, TagSuggest } from '@/api/types'
import { fmtDuration, fmtBytes } from '@/utils/format'
import { tagFieldLabel } from '@/utils/labels'
import AppIcon from './AppIcon.vue'

const props = defineProps<{ root: string; path: string; meta: FileMeta | null }>()
const emit = defineEmits<{ (event: 'saved'): void }>()

type Row = { key: string; value: string }

const rows = ref<Row[]>([])
const original = ref<Record<string, string[]>>({})

/** 流派建议来自曲库实际用到的写法，避免同一风格出现中英两套标签。 */
const FALLBACK_GENRES = [
  'Pop', 'Rock', 'Mandopop', 'Cantopop', 'Ballad', 'Folk',
  'Electronic', 'Jazz', 'Classical', 'Soundtrack',
]
let genreCache: string[] | null = null
const genreOptions = ref<string[]>([...FALLBACK_GENRES])
const genreInput = ref('')

const genreRow = computed(() =>
  rows.value.find((row) => row.key.trim().toUpperCase() === 'GENRE'),
)
const genres = computed(() =>
  (genreRow.value?.value ?? '')
    .split(';')
    .map((item) => item.trim())
    .filter(Boolean),
)

function addGenre(value?: string) {
  const text = (value ?? genreInput.value).trim()
  genreInput.value = ''
  if (!text) return
  // 已存在就忽略（大小写不敏感），避免同一风格出现两条
  if (genres.value.some((item) => item.toLowerCase() === text.toLowerCase())) return
  setGenres([...genres.value, text])
}

function removeGenre(value: string) {
  setGenres(genres.value.filter((item) => item !== value))
}

/** 只展示曲库里真的在用、且当前文件还没有的流派。 */
const genreSuggestions = computed(() => {
  const current = new Set(genres.value.map((item) => item.toLowerCase()))
  return genreOptions.value
    .filter((item) => !current.has(item.toLowerCase()))
    .slice(0, 8)
})

function setGenres(next: string[]) {
  const value = next.join('; ')
  if (genreRow.value) {
    genreRow.value.value = value
    return
  }
  if (value) rows.value.push({ key: 'GENRE', value })
}

/** 流派单独一行编辑，普通字段表里不再重复展示 GENRE。 */
const editableRows = computed(() =>
  rows.value.filter((row) => row.key.trim().toUpperCase() !== 'GENRE'),
)

onMounted(async () => {
  if (genreCache) {
    genreOptions.value = genreCache
    return
  }
  try {
    const stats = await api.stats()
    const counts = new Map<string, number>()
    for (const root of stats.roots) {
      for (const item of root.genres ?? []) {
        // 统计里的「其他 N 项」是合并后的长尾标签，不是真实流派
        if (item.is_other) continue
        counts.set(item.name, (counts.get(item.name) ?? 0) + item.count)
      }
    }
    const known = [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([name]) => name)
    genreCache = [...new Set([...known, ...FALLBACK_GENRES])].slice(0, 40)
    genreOptions.value = genreCache
  } catch {
    genreOptions.value = [...FALLBACK_GENRES]
  }
})
const saving = ref(false)
const loadedFor = ref('')

const link = ref('')
const lookupLoading = ref(false)
const lookupError = ref('')
const suggestion = ref<TagSuggest | null>(null)
const picking = ref<Record<string, boolean>>({})

const dirty = computed(() => {
  const current: Record<string, string[]> = {}
  for (const row of rows.value) {
    const key = row.key.trim().toUpperCase()
    if (!key) continue
    const values = row.value
      .split(';')
      .map((value) => value.trim())
      .filter(Boolean)
    if (values.length) current[key] = values
  }
  const before = original.value
  const keys = new Set([...Object.keys(current), ...Object.keys(before)])
  for (const key of keys) {
    const a = current[key] ?? []
    const b = before[key] ?? []
    if (a.length !== b.length || a.some((value, index) => value !== b[index])) return true
  }
  return false
})

function fill(meta: FileMeta | null) {
  if (!meta) {
    rows.value = []
    original.value = {}
    loadedFor.value = ''
    return
  }
  original.value = Object.fromEntries(
    Object.entries(meta.all_tags ?? {}).map(([key, values]) => [key, [...values]]),
  )
  rows.value = Object.entries(meta.all_tags ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, values]) => ({ key, value: values.join('; ') }))
}

watch(
  () => [props.meta, props.path] as const,
  ([meta, path]) => {
    if (path !== loadedFor.value) {
      loadedFor.value = path
      suggestion.value = null
      lookupError.value = ''
      link.value = ''
    }
    fill(meta)
  },
  { immediate: true },
)

function addRow() {
  rows.value.unshift({ key: '', value: '' })
}

function removeRow(row: Row) {
  const index = rows.value.indexOf(row)
  if (index >= 0) rows.value.splice(index, 1)
}

async function save() {
  const tags: Record<string, string[]> = {}
  const remove: string[] = []
  const seen = new Set<string>()

  for (const row of rows.value) {
    const key = row.key.trim().toUpperCase()
    if (!key) continue
    if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(key)) {
      ElMessage.error(`标签名不合法：${key}，只允许字母、数字、下划线`)
      return
    }
    seen.add(key)
    const values = row.value
      .split(';')
      .map((value) => value.trim())
      .filter(Boolean)
    const before = original.value[key] ?? []
    const changed = values.length !== before.length || values.some((value, index) => value !== before[index])
    if (!changed) continue
    if (values.length) tags[key] = values
    else remove.push(key)
  }

  for (const key of Object.keys(original.value)) {
    if (!seen.has(key)) remove.push(key)
  }

  if (!Object.keys(tags).length && !remove.length) {
    ElMessage.info('没有需要写入的修改')
    return
  }

  saving.value = true
  try {
    const result = await api.writeTags({ root: props.root, path: props.path, tags, remove })
    ElMessage.success(
      `已写入 ${result.applied.length} 项标签${result.removed.length ? `，删除 ${result.removed.length} 项` : ''}`,
    )
    emit('saved')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '写入失败')
  } finally {
    saving.value = false
  }
}

async function lookup() {
  const url = link.value.trim()
  if (!url) return
  lookupLoading.value = true
  lookupError.value = ''
  suggestion.value = null
  try {
    suggestion.value = await api.tagLink(props.root, props.path, url)
    const picks: Record<string, boolean> = {}
    for (const candidate of suggestion.value.candidates) {
      for (const row of candidate.rows ?? []) {
        picks[`${candidate.provider_id}:${row.key}`] = row.action !== 'same'
      }
    }
    picking.value = picks
  } catch (err) {
    lookupError.value = err instanceof Error ? err.message : '查询失败'
  } finally {
    lookupLoading.value = false
  }
}

function applyPicks() {
  if (!suggestion.value) return
  let applied = 0
  for (const candidate of suggestion.value.candidates) {
    for (const row of candidate.rows ?? []) {
      if (!picking.value[`${candidate.provider_id}:${row.key}`]) continue
      const remote = (row.suggested ?? []).join('; ').trim()
      if (!remote || row.action === 'same') continue
      const index = rows.value.findIndex((item) => item.key.toUpperCase() === row.key.toUpperCase())
      if (index >= 0) rows.value[index].value = remote
      else rows.value.push({ key: row.key, value: remote })
      applied += 1
    }
  }
  ElMessage.success(applied ? `已填入 ${applied} 个字段，确认后写入` : '没有可填入的字段')
}

function actionLabel(action: string) {
  return action === 'conflict' ? '不一致' : action === 'fill' ? '可补' : '一致'
}
</script>

<template>
  <div class="tags-panel">
    <div class="panel-head">
      <span class="panel-title">标签字段</span>
      <span class="cell-meta">多个值用分号分隔</span>
      <div class="panel-actions">
        <el-button size="small" @click="addRow"><AppIcon name="Plus" :size="12" />添加字段</el-button>
        <el-button size="small" type="primary" :loading="saving" :disabled="!dirty" @click="save">
          写入标签
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="meta && !meta.extension_matches_container"
      type="warning"
      :closable="false"
      show-icon
      :title="`文件扩展名 ${meta.extension} 与实际容器 ${meta.media_format} 不一致`"
      class="gap"
    />

    <div class="genre">
      <div class="panel-head">
        <span class="panel-title">流派</span>
        <span class="cell-meta">可多个；已有的不会重复添加</span>
      </div>
      <div v-if="genres.length" class="genre-tags">
        <el-tag
          v-for="item in genres"
          :key="item"
          closable
          size="small"
          @close="removeGenre(item)"
        >
          {{ item }}
        </el-tag>
      </div>
      <div class="genre-add">
        <el-input
          v-model="genreInput"
          size="small"
          placeholder="输入流派，回车添加"
          @keyup.enter="addGenre()"
        />
        <el-button size="small" @click="addGenre()">添加</el-button>
      </div>
      <div v-if="genreSuggestions.length" class="genre-suggest">
        <button
          v-for="item in genreSuggestions"
          :key="item"
          type="button"
          class="suggest"
          @click="addGenre(item)"
        >
          {{ item }}
        </button>
      </div>
    </div>

    <div class="rows">
      <div v-for="(row, index) in editableRows" :key="index" class="row">
        <el-input v-model="row.key" size="small" placeholder="字段名" class="key" />
        <el-input v-model="row.value" size="small" placeholder="值" class="value" />
        <el-tooltip content="删除该字段">
          <el-button size="small" text @click="removeRow(row)">
            <AppIcon name="Close" :size="13" />
          </el-button>
        </el-tooltip>
      </div>
      <div v-if="!editableRows.length" class="dim">没有其它标签</div>
    </div>

    <div class="lookup">
      <div class="panel-head">
        <span class="panel-title">从 MusicBrainz 补全</span>
        <span class="cell-meta">粘贴发行或录音链接</span>
      </div>
      <div class="lookup-row">
        <el-input
          v-model="link"
          placeholder="https://musicbrainz.org/release/..."
          clearable
          @keyup.enter="lookup"
        />
        <el-button :loading="lookupLoading" @click="lookup">查询</el-button>
      </div>
      <el-alert v-if="lookupError" :title="lookupError" type="error" :closable="false" show-icon />

      <template v-if="suggestion">
        <div v-for="candidate in suggestion.candidates" :key="candidate.provider_id" class="candidate">
          <div class="candidate-head">
            <span class="cell-meta">
              {{ candidate.source_kind }}
              <template v-if="candidate.duration_delta !== null">
                · 时长差 {{ Math.round(candidate.duration_delta) }} 秒
              </template>
            </span>
          </div>
          <div class="field-rows">
            <label v-for="row in candidate.rows" :key="row.key" class="field-row">
              <el-checkbox
                v-model="picking[`${candidate.provider_id}:${row.key}`]"
                :disabled="row.action === 'same'"
                size="small"
              />
              <span class="field-name">{{ tagFieldLabel(row.key) }}</span>
              <span class="field-local ellipsis" :class="{ changed: row.action !== 'same' }">
                {{ (row.current ?? []).join(' / ') || '空' }}
              </span>
              <AppIcon name="ArrowRight" :size="11" class="dim" />
              <span class="field-remote ellipsis">{{ (row.suggested ?? []).join(' / ') || '空' }}</span>
              <el-tag size="small" :type="row.action === 'conflict' ? 'warning' : row.action === 'fill' ? 'primary' : 'info'">
                {{ actionLabel(row.action) }}
              </el-tag>
            </label>
          </div>
        </div>
        <el-button size="small" type="primary" plain @click="applyPicks">填入勾选项</el-button>
      </template>

      <div v-else-if="!lookupLoading && !lookupError" class="lookup-hint dim">
        用链接指定条目，比自动匹配更准。
      </div>
    </div>

    <div v-if="meta" class="foot-meta">
      {{ meta.media_format.toUpperCase() }} · {{ fmtDuration(meta.duration) }} · {{ fmtBytes(meta.size) }}
      <span v-if="meta.embedded_state === 'synced'"> · 内嵌歌词有时间轴</span>
    </div>
  </div>
</template>

<style scoped>
.tags-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-title {
  font-size: 12px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--text-3);
}

.panel-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}

.genre {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.genre-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.genre-add {
  display: flex;
  gap: 6px;
}

.genre-add :deep(.el-input) {
  flex: 1;
}

.genre-suggest {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.suggest {
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text-3);
  border-radius: 999px;
  padding: 1px 9px;
  font-size: 11.5px;
  cursor: pointer;
  font-family: inherit;
}

.suggest:hover {
  border-color: var(--accent-line);
  color: var(--accent-strong);
  background: var(--accent-soft);
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 5px;
  max-height: 40vh;
  overflow: auto;
}

.row {
  display: flex;
  gap: 6px;
  align-items: center;
}

.key {
  width: 190px;
  flex: none;
}

.key :deep(.el-input__inner) {
  font-family: var(--mono);
  font-size: 12px;
}

.lookup {
  border-top: 1px solid var(--border);
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.lookup-row {
  display: flex;
  gap: 8px;
}

.lookup-row :deep(.el-input) {
  flex: 1;
}

.candidate {
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 8px 10px;
  background: var(--surface-2);
}

.candidate-head {
  margin-bottom: 6px;
}

.field-rows {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.field-row {
  display: grid;
  grid-template-columns: 18px 82px minmax(0, 1fr) 12px minmax(0, 1fr) auto;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.field-name {
  color: var(--text-3);
}

.field-local,
.field-remote {
  min-width: 0;
}

.field-local.changed {
  color: var(--warn);
}

.gap {
  margin-top: 2px;
}

.foot-meta {
  font-size: 11.5px;
  color: var(--text-3);
  border-top: 1px solid var(--border);
  padding-top: 8px;
}

.small {
  font-size: 12px;
}

.lookup-hint {
  font-size: 12px;
  padding: 10px 0;
}
</style>
