<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type {
  AIProtocol,
  AISettings,
  AITestResult,
  CoverPolicySettings,
  DedupePolicySettings,
  LyricsSettings,
  NetworkSettings,
  OrganizePolicySettings,
  ProxyFeature,
  RootDisplay,
  TagPolicy,
  TokenRow,
} from '@/api/types'
import { useMeta } from '@/stores/meta'
import { useSession } from '@/stores/session'
import { useTheme } from '@/stores/ui'
import { fmtAgo, fmtDate, fmtInterval } from '@/utils/format'
import { providerLabel } from '@/utils/labels'
import PageHead from '@/components/PageHead.vue'
import AppIcon from '@/components/AppIcon.vue'

const meta = useMeta()
const session = useSession()
const { isDark, toggle } = useTheme()

const tokens = ref<TokenRow[]>([])
const tokensLoading = ref(false)
const tokenLabel = ref('脚本')
const tokenTtl = ref(365)
const minted = ref('')
const minting = ref(false)

/** 登录会话是内部令牌，混在 API 令牌里既吵又容易误撤销当前会话。 */
const apiTokens = computed(() => tokens.value.filter((row) => !row.label.startsWith('session:')))
const sessionCount = computed(
  () => tokens.value.filter((row) => row.label.startsWith('session:')).length,
)

const tagPolicy = ref<TagPolicy | null>(null)
const tagLoading = ref(false)
const tagSaving = ref(false)

const ai = ref<AISettings | null>(null)
const aiLoading = ref(false)
const aiSaving = ref(false)
const aiTesting = ref(false)
const aiToken = ref('')
const clearAIToken = ref(false)
const aiTest = ref<AITestResult | null>(null)
const aiError = ref('')

const coverPolicy = ref<CoverPolicySettings | null>(null)
const organizePolicy = ref<OrganizePolicySettings | null>(null)
const dedupePolicy = ref<DedupePolicySettings | null>(null)
const lyricsSettings = ref<LyricsSettings | null>(null)
const acoustid = ref<{ enabled: boolean; has_key: boolean; masked: string; source: string } | null>(null)
const acoustidKey = ref('')
const coverSaving = ref(false)
const organizeSaving = ref(false)
const lyricsSaving = ref(false)
const acoustidSaving = ref(false)

const network = ref<NetworkSettings | null>(null)
const networkLoading = ref(false)
const networkSaving = ref(false)
const networkTesting = ref(false)
const networkError = ref('')
const networkTest = ref('')

/** 每个境外功能是否走代理。国内来源（QQ、网易云）永远直连，没有开关。 */
const PROXY_FEATURE_LABEL: Record<ProxyFeature, string> = {
  lrclib: '歌词来源 LRCLIB',
  musicbrainz: '标签 MusicBrainz',
  acoustid: '指纹 AcoustID',
  cover: '按链接下载封面',
}

const AI_PROTOCOL_LABEL: Record<AIProtocol, string> = {
  openai_chat: 'OpenAI Chat Completions',
  openai_responses: 'OpenAI Responses',
  anthropic: 'Anthropic Messages',
}

const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const changing = ref(false)

const health = computed(() => meta.health)
const summary = computed(() => meta.summary)

async function loadTagPolicy() {
  tagLoading.value = true
  try {
    tagPolicy.value = (await api.tagSettings()).policy
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '标签设置加载失败')
  } finally {
    tagLoading.value = false
  }
}

async function saveTagPolicy() {
  if (!tagPolicy.value) return
  const policy = tagPolicy.value
  if (policy.scan_interval_hours < 0.5 || policy.scan_interval_hours > 168) {
    ElMessage.warning('扫描间隔需在 0.5 到 168 小时之间')
    return
  }
  if (policy.max_auto_write_per_run < 0 || policy.max_auto_write_per_run > 10000) {
    ElMessage.warning('单轮写入上限需在 0 到 10000 之间')
    return
  }
  if (policy.max_duration_delta_seconds < 0 || policy.max_duration_delta_seconds > 120) {
    ElMessage.warning('时长容差需在 0 到 120 秒之间')
    return
  }
  tagSaving.value = true
  try {
    tagPolicy.value = (await api.updateTagSettings(policy)).policy
    ElMessage.success('标签设置已保存，监控进程会在一分钟内应用')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '保存失败')
  } finally {
    tagSaving.value = false
  }
}

async function loadAutomationSettings() {
  try {
    const [cover, organize, dedupe, lyrics, acid] = await Promise.all([
      api.coverSettings(),
      api.organizeSettings(),
      api.dedupeSettings(),
      api.lyricsSettings(),
      api.acoustidSettings(),
    ])
    coverPolicy.value = cover.policy
    organizePolicy.value = organize.policy
    dedupePolicy.value = dedupe.policy
    lyricsSettings.value = lyrics.settings
    acoustid.value = acid
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '自动化设置加载失败')
  }
}

async function saveCoverPolicy() {
  if (!coverPolicy.value) return
  coverSaving.value = true
  try {
    coverPolicy.value = (await api.updateCoverSettings(coverPolicy.value)).policy
    ElMessage.success('封面设置已保存，监控进程会在一分钟内应用')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '保存失败')
  } finally {
    coverSaving.value = false
  }
}

async function saveOrganizePolicy() {
  if (!organizePolicy.value) return
  organizeSaving.value = true
  try {
    organizePolicy.value = (await api.updateOrganizeSettings(organizePolicy.value)).policy
    ElMessage.success('归位设置已保存，监控进程会在一分钟内应用')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '保存失败')
  } finally {
    organizeSaving.value = false
  }
}

async function saveDedupePolicy() {
  if (!dedupePolicy.value) return
  organizeSaving.value = true
  try {
    dedupePolicy.value = (await api.updateDedupeSettings(dedupePolicy.value)).policy
    ElMessage.success('重复检测设置已保存，监控进程会在一分钟内应用')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '保存失败')
  } finally {
    organizeSaving.value = false
  }
}

async function saveLyricsSettings() {
  if (!lyricsSettings.value) return
  lyricsSaving.value = true
  try {
    lyricsSettings.value = (await api.updateLyricsSettings(lyricsSettings.value)).settings
    ElMessage.success('歌词输出设置已保存')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '保存失败')
  } finally {
    lyricsSaving.value = false
  }
}

async function saveAcoustid() {
  acoustidSaving.value = true
  try {
    const payload: { api_key?: string; enabled?: boolean } = {
      enabled: acoustid.value?.enabled ?? false,
    }
    if (acoustidKey.value.trim()) payload.api_key = acoustidKey.value.trim()
    await api.updateAcoustidSettings(payload)
    acoustid.value = await api.acoustidSettings()
    acoustidKey.value = ''
    ElMessage.success('指纹设置已保存')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '保存失败')
  } finally {
    acoustidSaving.value = false
  }
}

async function loadAISettings() {
  aiLoading.value = true
  aiError.value = ''
  try {
    ai.value = await api.aiSettings()
    aiToken.value = ''
    clearAIToken.value = false
    aiTest.value = null
  } catch (err) {
    aiError.value = err instanceof Error ? err.message : '大模型设置加载失败'
  } finally {
    aiLoading.value = false
  }
}

function aiPayload() {
  if (!ai.value) return null
  const payload = {
    enabled: ai.value.enabled,
    protocol: ai.value.protocol,
    base_url: ai.value.base_url.trim(),
    model: ai.value.model.trim(),
    timeout_seconds: ai.value.timeout_seconds,
    max_requests_per_run: ai.value.max_requests_per_run,
    auto_clean_duplicates: ai.value.auto_clean_duplicates,
    pre_review: ai.value.pre_review,
    pre_review_confidence: ai.value.pre_review_confidence,
    auto_accept: ai.value.auto_accept,
    verify_tags: ai.value.verify_tags,
    use_proxy: ai.value.use_proxy,
  }
  if (clearAIToken.value) return { ...payload, clear_api_key: true }
  if (aiToken.value.trim()) return { ...payload, api_key: aiToken.value.trim() }
  return payload
}

async function saveAISettings() {
  if (!ai.value) return
  const payload = aiPayload()
  if (!payload) return
  if (!payload.base_url.startsWith('http://') && !payload.base_url.startsWith('https://')) {
    ElMessage.warning('API 地址需要以 http:// 或 https:// 开头')
    return
  }
  if (!payload.model) {
    ElMessage.warning('请填写模型 ID')
    return
  }
  if (payload.timeout_seconds < 5 || payload.timeout_seconds > 300) {
    ElMessage.warning('超时时间需要在 5 到 300 秒之间')
    return
  }
  aiSaving.value = true
  aiError.value = ''
  try {
    const saved = await api.updateAISettings(payload)
    ai.value = saved
    aiToken.value = ''
    clearAIToken.value = false
    ElMessage.success('大模型设置已保存')
  } catch (err) {
    aiError.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    aiSaving.value = false
  }
}

async function testAISettings() {
  if (!ai.value) return
  const payload = aiPayload()
  if (!payload) return
  aiTesting.value = true
  aiTest.value = null
  aiError.value = ''
  try {
    aiTest.value = await api.testAISettings(payload)
    ElMessage.success('连接成功')
  } catch (err) {
    aiError.value = err instanceof Error ? err.message : '连接失败'
  } finally {
    aiTesting.value = false
  }
}

async function loadNetworkSettings() {
  networkLoading.value = true
  networkError.value = ''
  try {
    network.value = await api.networkSettings()
  } catch (err) {
    networkError.value = err instanceof Error ? err.message : '代理设置加载失败'
  } finally {
    networkLoading.value = false
  }
}

async function saveNetworkSettings() {
  if (!network.value) return
  networkSaving.value = true
  networkError.value = ''
  try {
    network.value = await api.updateNetworkSettings({
      proxy: network.value.proxy.trim(),
      use_proxy: { ...network.value.use_proxy },
    })
    ElMessage.success('代理设置已保存')
  } catch (err) {
    networkError.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    networkSaving.value = false
  }
}

async function testNetworkProxy() {
  networkTesting.value = true
  networkTest.value = ''
  try {
    const result = await api.testNetworkSettings()
    networkTest.value = result.message
    if (result.reachable === false) {
      ElMessage.warning(result.message)
    } else {
      ElMessage.success(result.message)
    }
  } catch (err) {
    networkTest.value = err instanceof Error ? err.message : '测试失败'
    ElMessage.error(networkTest.value)
  } finally {
    networkTesting.value = false
  }
}

/**
 * 受管根的显示配置：显示名 + 宿主机路径。
 *
 * 显示名留空 = 用宿主机目录名（如 `Library`）。宿主机路径是用户能
 * 打开的位置（容器里挂载点是 `/library`，磁盘上是
 * `/vol2/1000/Music/Library`）——两者不一致时，按容器内路径取名字
 * 会得到「library」，与磁盘上的真实目录对不上。
 */
const rootDisplays = ref<RootDisplay[]>([])
const rootLabelsDirty = ref(false)
const rootLabelsSaving = ref(false)

async function loadRootLabels() {
  try {
    const data = await api.rootLabels()
    rootDisplays.value = data.roots ?? []
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '曲库设置加载失败')
  }
  rootLabelsDirty.value = false
}

/** 新增曲库（运行时根）：路径必须在容器内已存在。 */
const newRoot = ref({ name: '', path: '', host_path: '', display_name: '', role: 'library' })
const addingRoot = ref(false)

async function addRoot() {
  if (!newRoot.value.name.trim() || !newRoot.value.path.trim()) {
    ElMessage.warning('填写曲库名称与路径')
    return
  }
  addingRoot.value = true
  try {
    await api.addRoot({ ...newRoot.value })
    newRoot.value = { name: '', path: '', host_path: '', display_name: '', role: 'library' }
    await loadRootLabels()
    await meta.refresh()
    ElMessage.success('曲库已添加，重启监控进程后开始监听')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '添加曲库失败')
  } finally {
    addingRoot.value = false
  }
}

async function saveRootLabels() {
  rootLabelsSaving.value = true
  try {
    await api.updateRootDisplay(
      rootDisplays.value.map((root) => ({
        name: root.name,
        display_name: root.display_name ?? '',
        host_path: root.host_path ?? '',
      })),
    )
    await meta.refresh()
    await loadRootLabels()
    ElMessage.success('曲库显示设置已保存')
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '曲库显示设置保存失败')
  } finally {
    rootLabelsSaving.value = false
  }
}

async function loadTokens() {
  tokensLoading.value = true
  try {
    tokens.value = (await api.tokens()).items
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '令牌列表加载失败')
  } finally {
    tokensLoading.value = false
  }
}

async function mint() {
  if (!tokenLabel.value.trim()) {
    ElMessage.warning('请填写令牌用途，便于日后识别')
    return
  }
  minting.value = true
  try {
    const result = await api.mintToken(tokenLabel.value.trim(), tokenTtl.value)
    minted.value = result.token
    await loadTokens()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '签发失败')
  } finally {
    minting.value = false
  }
}

async function revoke(row: TokenRow) {
  try {
    await ElMessageBox.confirm(`撤销后，用「${row.label}」的脚本会立刻失效。`, '撤销令牌', {
      confirmButtonText: '撤销',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.revokeToken(row.id)
    ElMessage.success('已撤销')
    await loadTokens()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '撤销失败')
  }
}

async function changePassword() {
  if (newPassword.value.length < 10) {
    ElMessage.warning('新密码至少 10 个字符')
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  changing.value = true
  try {
    await api.changePassword({
      username: session.username || 'admin',
      current_password: currentPassword.value,
      new_password: newPassword.value,
    })
    ElMessage.success('密码已更新')
    currentPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : '修改失败')
  } finally {
    changing.value = false
  }
}

async function copyToken() {
  try {
    await navigator.clipboard.writeText(minted.value)
    ElMessage.success('已复制')
  } catch {
    /* 非 HTTPS 环境下剪贴板不可用，让用户手动选中 */
  }
}

onMounted(async () => {
  if (!meta.summary) await meta.refresh().catch(() => undefined)
  loadRootLabels()
  await Promise.all([
    loadTokens(),
    loadTagPolicy(),
    loadAISettings(),
    loadAutomationSettings(),
    loadNetworkSettings(),
  ])
})
</script>

<template>
  <PageHead />

  <div class="grid">
    <section class="card">
      <div class="card-head"><span class="card-title">账户</span></div>
      <div class="card-body col">
        <div class="row">
          <div>
            <div class="label">登录状态</div>
            <div class="cell-meta">登录状态由服务端签发，7 天有效</div>
          </div>
          <el-tag type="success" size="small">已登录</el-tag>
        </div>
        <el-button @click="session.logout()">退出登录</el-button>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><span class="card-title">外观</span></div>
      <div class="card-body row">
        <div>
          <div class="label">主题</div>
          <div class="cell-meta">跟随系统，也可以固定为深色或浅色</div>
        </div>
        <el-button @click="toggle">
          <AppIcon :name="isDark ? 'Sunny' : 'Moon'" :size="14" style="margin-right: 5px" />
          {{ isDark ? '切换到浅色' : '切换到深色' }}
        </el-button>
      </div>
    </section>
  </div>

  <section class="card">
    <div class="card-head">
      <span class="card-title">标签扫描</span>
      <span class="card-hint">保存后监控进程会在一分钟内应用</span>
    </div>
    <div v-if="tagPolicy" class="card-body col">
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">自动扫描</div>
          <div class="cell-meta">监控进程按间隔扫描全库，发现缺口后自动补齐</div>
        </div>
        <el-switch v-model="tagPolicy.auto_scan" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">扫描间隔</div>
          <div class="cell-meta">
            最短 30 分钟，最长 7 天；每 {{ fmtInterval(tagPolicy.scan_interval_hours) }}自动扫描
          </div>
        </div>
        <div class="setting-control">
          <el-input-number
            v-model="tagPolicy.scan_interval_hours"
            :min="0.5"
            :max="168"
            :step="0.5"
            :disabled="!tagPolicy.auto_scan"
            controls-position="right"
          />
          <span class="cell-meta">小时</span>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">自动写入</div>
          <div class="cell-meta">只写补缺失和按目录纠正的发行字段，普通冲突仍进复核</div>
        </div>
        <el-switch v-model="tagPolicy.auto_write" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">指纹校验</div>
          <div class="cell-meta">缺 MBID 或有歧义时用 AcoustID 确认录音</div>
        </div>
        <el-switch v-model="tagPolicy.fingerprint" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">单轮写入上限</div>
          <div class="cell-meta">一次扫描最多自动写入多少首，0 表示不限制</div>
        </div>
        <el-input-number
          v-model="tagPolicy.max_auto_write_per_run"
          :min="0"
          :max="10000"
          :step="50"
          controls-position="right"
        />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">时长容差</div>
          <div class="cell-meta">候选与本地时长相差超过此值时不自动写入</div>
        </div>
        <div class="setting-control">
          <el-input-number
            v-model="tagPolicy.max_duration_delta_seconds"
            :min="0"
            :max="120"
            :step="0.5"
            controls-position="right"
          />
          <span class="cell-meta">秒</span>
        </div>
      </div>
      <div class="setting-actions">
        <el-button type="primary" :loading="tagSaving" @click="saveTagPolicy">
          保存标签设置
        </el-button>
        <span class="cell-meta">
          自动扫描间隔会同步显示在「待办 · 标签」页的扫描状态里
        </span>
      </div>
    </div>
    <el-skeleton v-else-if="tagLoading" :loading="true" animated :rows="4" class="settings-skeleton" />
    <el-alert
      v-else
      type="error"
      :closable="false"
      show-icon
      title="标签设置加载失败，请刷新页面重试"
      class="settings-skeleton"
    />
  </section>

  <section class="card">
    <div class="card-head">
      <span class="card-title">封面与归位</span>
      <span class="card-hint">入库流水线的两个自动化开关；移动文件可逐条撤销</span>
    </div>
    <div v-if="coverPolicy && organizePolicy" class="card-body col">
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">同目录图片自动嵌入</div>
          <div class="cell-meta">检测到 cover.jpg 等规范命名图片时，自动嵌入同目录缺封面的文件</div>
        </div>
        <el-switch v-model="coverPolicy.auto_embed_sibling" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">网络封面自动嵌入</div>
          <div class="cell-meta">
            关闭时只把封面候选放进待办，确认后写入；打开则直接嵌入（按专辑一次下载）
          </div>
        </div>
        <el-switch v-model="coverPolicy.auto_write" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">封面质量门</div>
          <div class="cell-meta">宽×高不小于该值才采用，不足自动换下一来源</div>
        </div>
        <el-input-number
          v-model="coverPolicy.min_min_pixels"
          :min="0"
          :max="10000000"
          :step="10000"
          controls-position="right"
        />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">自动归位</div>
          <div class="cell-meta">
            「新文件自动归位」只对入库文件生效；「定期整理全库」每 6 小时
            把全库（含存量）按模板归位。冲突一律转待办，所有移动可撤销。
          </div>
        </div>
        <el-select v-model="organizePolicy.mode" style="width: 210px">
          <el-option label="不动文件（只出建议）" value="off" />
          <el-option label="新文件自动归位" value="new_files" />
          <el-option label="收件箱流转" value="inbox" />
          <el-option label="定期整理全库（含存量）" value="full" />
        </el-select>
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">目录模板</div>
          <div class="cell-meta">
            可用字段：artist、albumartist、album、title、track_no、disc_no、year、ext
          </div>
        </div>
        <el-input
          v-model="organizePolicy.template"
          style="max-width: 340px"
          placeholder="{artist}/{album}/{track_no} - {title}"
        />
      </div>
      <div class="setting-actions">
        <el-button type="primary" :loading="coverSaving || organizeSaving" @click="saveCoverPolicy(); saveOrganizePolicy()">
          保存封面与归位设置
        </el-button>
        <span class="cell-meta">归位记录在整理日志，可逐条撤销</span>
      </div>
    </div>
    <el-skeleton v-else :loading="true" animated :rows="4" class="settings-skeleton" />
  </section>

  <section class="card">
    <div class="card-head">
      <span class="card-title">重复文件</span>
      <span class="card-hint">完全一样的副本自动清理，其余留人工判断</span>
    </div>
    <div v-if="dedupePolicy" class="card-body col">
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">自动清理完全相同的副本</div>
          <div class="cell-meta">
            音频数据、时长、标题、艺人、专辑全部相同才自动处理：保留一份，
            其余进回收站（可还原）。同一首歌的不同专辑或不同版本不动。
          </div>
        </div>
        <el-switch v-model="dedupePolicy.auto_clean_exact" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">检测间隔</div>
          <div class="cell-meta">扫描全库找出重复文件的时间间隔</div>
        </div>
        <el-input-number
          v-model="dedupePolicy.scan_interval_hours"
          :min="1"
          :max="720"
          size="small"
          style="width: 130px"
        />
        <span class="cell-meta">小时</span>
      </div>
      <div class="setting-actions">
        <el-button type="primary" :loading="organizeSaving" @click="saveDedupePolicy">
          保存重复检测设置
        </el-button>
        <span class="cell-meta">被清理的副本在「回收站」里可以还原</span>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <span class="card-title">歌词输出</span>
      <span class="card-hint">翻译歌词与逐字歌词的写入文件后的形态</span>
    </div>
    <div v-if="lyricsSettings" class="card-body col">
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">翻译歌词</div>
          <div class="cell-meta">
            双语文体：翻译行紧跟原文行写入 .lrc 与内嵌，本地播放器兼容最好；
            翻译数据默认一直在抓取，只是之前从未落盘
          </div>
        </div>
        <el-select v-model="lyricsSettings.translation" style="width: 210px">
          <el-option label="双语文体（推荐）" value="bilingual" />
          <el-option label="不写入翻译" value="off" />
          <el-option label="仅暂存（不写入文件）" value="field" />
        </el-select>
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">保留逐字歌词原文</div>
          <div class="cell-meta">QQ逐字词（QRC）另存为同名 .qrc 文件；默认关闭</div>
        </div>
        <el-switch v-model="lyricsSettings.keep_word_level" />
      </div>
      <div class="setting-actions">
        <el-button type="primary" :loading="lyricsSaving" @click="saveLyricsSettings">
          保存歌词输出设置
        </el-button>
      </div>
    </div>
    <el-skeleton v-else :loading="true" animated :rows="3" class="settings-skeleton" />
  </section>

  <section class="card">
    <div class="card-head">
      <span class="card-title">声学指纹</span>
      <span class="card-hint">
        AcoustID：标签缺失或有歧义时，用音频本身确认「这是哪首歌」——身份识别的最后兜底
      </span>
    </div>
    <div v-if="acoustid" class="card-body col">
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">启用指纹兜底</div>
          <div class="cell-meta">
            需要 fpcalc（Chromaprint）与 API 密钥。
            <span v-if="acoustid.source === 'env'">key 来自环境变量，此处只读</span>
            <span v-else-if="acoustid.has_key">已保存：{{ acoustid.masked }}</span>
          </div>
        </div>
        <el-switch v-model="acoustid.enabled" />
      </div>
      <div v-if="acoustid.source !== 'env'" class="setting-row">
        <div class="setting-text">
          <div class="label">API 密钥</div>
          <div class="cell-meta">在 acoustid.org 免费申请；只保存在服务端数据目录</div>
        </div>
        <el-input
          v-model="acoustidKey"
          type="password"
          show-password
          clearable
          :placeholder="acoustid.has_key ? '留空表示不修改' : '粘贴 AcoustID API 密钥'"
          style="max-width: 360px"
        />
      </div>
      <div class="setting-actions">
        <el-button
          type="primary"
          :loading="acoustidSaving"
          :disabled="acoustid.source === 'env'"
          @click="saveAcoustid"
        >
          保存指纹设置
        </el-button>
      </div>
    </div>
    <el-skeleton v-else :loading="true" animated :rows="3" class="settings-skeleton" />
  </section>

  <section class="card">
    <div class="card-head">
      <span class="card-title">大模型</span>
      <span class="card-hint">用于歌词语义复核；Token 只保存在服务端数据目录</span>
    </div>
    <div v-if="ai" class="card-body col">
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">启用大模型</div>
          <div class="cell-meta">只用于「两份歌词是否同一首、留哪一份」这类语义判断</div>
        </div>
        <el-switch v-model="ai.enabled" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">协议</div>
          <div class="cell-meta">决定请求体和返回解析方式；兼容 OpenAI 与 Anthropic</div>
        </div>
        <el-select v-model="ai.protocol" style="width: 260px">
          <el-option
            v-for="item in ai.protocols"
            :key="item"
            :value="item"
            :label="AI_PROTOCOL_LABEL[item]"
          />
        </el-select>
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">API 地址</div>
          <div class="cell-meta">可填 base URL（如 https://api.openai.com/v1）或完整 endpoint</div>
        </div>
        <el-input
          v-model="ai.base_url"
          placeholder="https://api.openai.com/v1"
          style="max-width: 420px"
        />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">模型 ID</div>
          <div class="cell-meta">例如 gpt-4o-mini、claude-sonnet-4-5 或兼容服务的模型名</div>
        </div>
        <el-input v-model="ai.model" placeholder="gpt-4o-mini" style="max-width: 420px" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">API 令牌</div>
          <div class="cell-meta">
            <span v-if="ai.api_key_source === 'env'">来自环境变量 LYRA_AI_API_KEY，设置页不能覆盖</span>
            <span v-else-if="ai.api_key_source === 'settings'">已保存在服务端 ai-token 文件，留空表示不修改</span>
            <span v-else-if="ai.api_key_source === 'config'">来自配置文件，留空表示不修改</span>
            <span v-else>还没有配置 Token</span>
          </div>
        </div>
        <div class="setting-control token-control">
          <el-input
            v-model="aiToken"
            type="password"
            show-password
            clearable
            :disabled="clearAIToken || ai.api_key_source === 'env'"
            :placeholder="ai.has_api_key ? '留空表示不修改' : '粘贴 API 令牌'"
            style="max-width: 360px"
          />
          <el-checkbox
            v-if="ai.has_api_key && ai.api_key_source !== 'env'"
            v-model="clearAIToken"
          >
            清除已保存的 Token
          </el-checkbox>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">超时</div>
          <div class="cell-meta">单次模型请求的最长等待时间</div>
        </div>
        <div class="setting-control">
          <el-input-number
            v-model="ai.timeout_seconds"
            :min="5"
            :max="300"
            :step="5"
            controls-position="right"
          />
          <span class="cell-meta">秒</span>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">单轮请求上限</div>
          <div class="cell-meta">一次操作最多调用模型多少次，0 表示不限制</div>
        </div>
        <el-input-number
          v-model="ai.max_requests_per_run"
          :min="0"
          :max="100"
          :step="1"
          controls-position="right"
        />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">使用网络代理</div>
          <div class="cell-meta">
            关闭后大模型请求直连，不受「网络代理」设置影响。国内接口直连通常更稳；
            境外接口若需要代理再打开。
          </div>
        </div>
        <el-switch v-model="ai.use_proxy" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">智能预审</div>
          <div class="cell-meta">
            对落入待审查的候选调用模型判定「是否同一首歌」，判定结论显示在待办里，
            高置信时自动采用（留痕、可撤销）。可大幅减少逐首人工确认。
          </div>
        </div>
        <el-switch v-model="ai.pre_review" />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">自动采用</div>
          <div class="cell-meta">
            模型判定为同一首歌且置信度达标时自动写入歌词（写入留痕、可撤销）。
            关闭后模型只给建议，仍需人工确认。
          </div>
        </div>
        <el-switch v-model="ai.auto_accept" />
      </div>
      <div v-if="ai.pre_review" class="setting-row">
        <div class="setting-text">
          <div class="label">自动采用阈值</div>
          <div class="cell-meta">模型置信度达到该值且程序闸门（时长/版本）也通过时才自动写入</div>
        </div>
        <el-input-number
          v-model="ai.pre_review_confidence"
          :min="0.5"
          :max="1"
          :step="0.05"
          controls-position="right"
        />
      </div>
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">标签候选核验</div>
          <div class="cell-meta">标签搜索到的候选经模型核验后按「已核验」处理</div>
        </div>
        <el-switch v-model="ai.verify_tags" />
      </div>
      <div class="setting-actions">
        <el-button type="primary" :loading="aiSaving" @click="saveAISettings">
          保存大模型设置
        </el-button>
        <el-button :loading="aiTesting" @click="testAISettings">
          测试连接
        </el-button>
        <span class="cell-meta">测试只发一条最小请求，不写文件</span>
      </div>
      <el-alert
        v-if="aiError"
        :title="aiError"
        type="error"
        :closable="false"
        show-icon
      />
      <el-alert
        v-if="aiTest"
        :title="`连接成功 · ${aiTest.model} · ${aiTest.latency_ms}ms · ${aiTest.use_proxy ? '经代理' : '直连'}`"
        type="success"
        :closable="false"
        show-icon
      >
        <div class="cell-meta mono">{{ aiTest.endpoint }}</div>
        <div v-if="aiTest.sample" class="cell-meta">返回：{{ aiTest.sample }}</div>
      </el-alert>
    </div>
    <el-skeleton v-else-if="aiLoading" :loading="true" animated :rows="5" class="settings-skeleton" />
    <el-alert
      v-else
      type="error"
      :closable="false"
      show-icon
      title="大模型设置加载失败，请刷新页面重试"
      class="settings-skeleton"
    />
  </section>

  <section class="card">
    <div class="card-head">
      <span class="card-title">网络代理</span>
      <span class="card-hint">按功能决定哪些功能走代理；国内来源始终直连</span>
    </div>
    <div v-if="network" class="card-body col">
      <div class="setting-row">
        <div class="setting-text">
          <div class="label">代理地址</div>
          <div class="cell-meta">
            支持 HTTP 与 SOCKS5，例如 http://10.0.1.8:7890 或 socks5://10.0.1.8:7890。
            留空时沿用系统环境变量，未配置则直连。
          </div>
        </div>
        <el-input
          v-model="network.proxy"
          placeholder="http://10.0.1.8:7890"
          clearable
          style="max-width: 420px"
        />
      </div>
      <el-alert
        v-if="network.source === 'env' && network.environment_proxy"
        :title="`当前沿用环境变量代理：${network.environment_proxy}`"
        type="info"
        :closable="false"
        show-icon
      />
      <el-alert
        v-if="network.source === 'config'"
        title="当前使用配置文件里的代理，保存后会覆盖它"
        type="info"
        :closable="false"
        show-icon
      />
      <el-alert
        v-if="network.proxy_status === 'unreachable'"
        title="代理当前不可达，经代理的功能会失败。可先关闭对应功能的开关。"
        type="error"
        :closable="false"
        show-icon
      />
      <div class="setting-row" v-for="feature in network.features" :key="feature">
        <div class="setting-text">
          <div class="label">{{ PROXY_FEATURE_LABEL[feature] }}</div>
          <div class="cell-meta">这个功能是否经代理访问</div>
        </div>
        <el-switch v-model="network.use_proxy[feature]" />
      </div>
      <div class="cell-meta">
        QQ音乐、网易云音乐等国内来源始终直连，不受这里影响；大模型的开关在大模型设置里。
      </div>
      <div class="setting-actions">
        <el-button type="primary" :loading="networkSaving" @click="saveNetworkSettings">
          保存代理设置
        </el-button>
        <el-button :loading="networkTesting" @click="testNetworkProxy">测试代理</el-button>
        <span class="cell-meta">保存后新的请求立即使用，监控进程约 30 秒内应用</span>
      </div>
      <el-alert
        v-if="networkTest"
        :title="networkTest"
        type="info"
        :closable="false"
        show-icon
      />
      <el-alert
        v-if="networkError"
        :title="networkError"
        type="error"
        :closable="false"
        show-icon
      />
    </div>
    <el-skeleton v-else-if="networkLoading" :loading="true" animated :rows="3" class="settings-skeleton" />
    <el-alert
      v-else
      type="error"
      :closable="false"
      show-icon
      title="代理设置加载失败，请刷新页面重试"
      class="settings-skeleton"
    />
  </section>

  <section class="card">
    <div class="card-head"><span class="card-title">更新密码</span></div>
    <div class="card-body form">
      <el-input v-model="currentPassword" type="password" show-password placeholder="当前密码" autocomplete="current-password" />
      <el-input v-model="newPassword" type="password" show-password placeholder="新密码，至少 10 个字符" autocomplete="new-password" />
      <el-input v-model="confirmPassword" type="password" show-password placeholder="再次输入新密码" autocomplete="new-password" />
      <el-button type="primary" :loading="changing" :disabled="!currentPassword || !newPassword" @click="changePassword">
        更新密码
      </el-button>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <span class="card-title">API 令牌</span>
      <span class="card-hint">给脚本与移动端用</span>
      <div class="card-actions">
        <el-button size="small" :loading="tokensLoading" @click="loadTokens">刷新</el-button>
      </div>
    </div>
    <div class="card-body col">
      <div class="mint">
        <el-input v-model="tokenLabel" placeholder="用途，例如「同步脚本」" style="max-width: 220px" />
        <el-select v-model="tokenTtl" style="width: 130px">
          <el-option :value="30" label="30 天" />
          <el-option :value="180" label="180 天" />
          <el-option :value="365" label="一年" />
        </el-select>
        <el-button type="primary" :loading="minting" @click="mint">签发</el-button>
      </div>

      <el-alert
        v-if="minted"
        type="success"
        :closable="false"
        title="令牌只显示这一次，请立刻保存"
        class="minted"
      >
        <div class="token-box">
          <code>{{ minted }}</code>
          <el-button size="small" @click="copyToken">复制</el-button>
        </div>
      </el-alert>

      <div v-if="apiTokens.length" class="token-list">
        <div v-for="row in apiTokens" :key="row.id" class="token-row">
          <span class="token-label">{{ row.label }}</span>
          <span class="cell-meta">创建于 {{ fmtDate(row.created_at) }}</span>
          <span class="cell-meta">最近使用 {{ fmtAgo(row.last_used_at) }}</span>
          <span class="cell-meta">{{ row.expires_at ? `到期 ${fmtDate(row.expires_at)}` : '长期有效' }}</span>
          <el-button size="small" text type="danger" @click="revoke(row)">撤销</el-button>
        </div>
      </div>
      <div v-else class="dim">还没有签发过令牌</div>
      <div v-if="sessionCount" class="cell-meta">
        另有 {{ sessionCount }} 条登录会话，登录时自动签发，到期自动失效
      </div>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><span class="card-title">运行信息</span></div>
    <div class="card-body info">
      <div class="info-row"><span class="label">版本</span><span class="num">{{ health?.version || '—' }}</span></div>
      <div class="info-row"><span class="label">数据库结构</span><span class="num">v{{ health?.schema_version ?? '—' }}</span></div>
      <div class="info-row">
        <span class="label">写入开关</span>
        <el-tag size="small" :type="meta.writeEnabled ? 'success' : 'warning'">
          {{ meta.writeEnabled ? '已开启' : '关闭' }}
        </el-tag>
      </div>
      <div class="info-row">
        <span class="label">歌词来源</span>
        <span>{{ (summary?.lyrics_by_provider ? Object.keys(summary.lyrics_by_provider) : []).map(providerLabel).join(' · ') || '未统计' }}</span>
      </div>
      <div class="info-row">
        <span class="label">受管目录</span>
        <div class="roots">
          <span v-if="!summary?.roots.length" class="dim">没有配置受管目录</span>
          <div v-for="root in rootDisplays" :key="root.name" class="root">
            <el-tag size="small" type="info">{{ root.name }}</el-tag>
            <el-input
              v-model="root.display_name"
              class="root-label-input"
              size="small"
              :placeholder="`显示名（默认 ${root.label}）`"
              maxlength="60"
              clearable
              @input="rootLabelsDirty = true"
            />
            <el-input
              v-model="root.host_path"
              class="root-path-input mono"
              size="small"
              placeholder="/vol2/1000/Music/Library"
              @input="rootLabelsDirty = true"
            />
            <span class="mono cell-meta">容器内 {{ root.path }}</span>
          </div>
          <div v-if="rootDisplays.length" class="row">
            <el-button
              size="small"
              type="primary"
              :disabled="!rootLabelsDirty"
              :loading="rootLabelsSaving"
              @click="saveRootLabels"
            >保存曲库设置</el-button>
            <span class="dim">
              显示名留空则用宿主机目录名；宿主机路径是你在文件管理器里能看到的位置
            </span>
          </div>
          <div class="root-add">
            <div class="label">新增曲库</div>
            <div class="cell-meta">
              路径需在容器内已存在：指向已有挂载点的子目录可以直接添加；
              指向宿主机上的新目录要先在 compose 里挂载并重启。
            </div>
            <div class="root-add-row">
              <el-input
                v-model="newRoot.name"
                size="small"
                placeholder="名称（如 inbox）"
                style="max-width: 150px"
              />
              <el-select v-model="newRoot.role" size="small" style="max-width: 110px">
                <el-option label="曲库" value="library" />
                <el-option label="收件箱" value="inbox" />
              </el-select>
              <el-input
                v-model="newRoot.path"
                size="small"
                class="mono"
                placeholder="/library/Inbox"
                style="max-width: 220px"
              />
              <el-input
                v-model="newRoot.host_path"
                size="small"
                class="mono"
                placeholder="宿主机路径（可选）"
                style="max-width: 240px"
              />
              <el-input
                v-model="newRoot.display_name"
                size="small"
                placeholder="显示名（可选）"
                style="max-width: 160px"
              />
              <el-button size="small" type="primary" :loading="addingRoot" @click="addRoot">
                添加
              </el-button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.token-control {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr));
  gap: 14px;
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  width: 100%;
  padding: 9px 0;
  border-bottom: 1px solid var(--border);
}

.setting-row:last-of-type {
  border-bottom: 0;
}

.setting-text {
  min-width: 0;
}

.setting-control {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.setting-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  width: 100%;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.settings-skeleton {
  padding: 16px;
}

@media (max-width: 760px) {
  .setting-row {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }
  .setting-control {
    width: 100%;
  }
}

.col {
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: flex-start;
}

.col > .row {
  width: 100%;
}

.label {
  font-size: 12px;
  color: var(--text-3);
}

.form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
  align-items: start;
}

.form :deep(.el-button) {
  justify-self: start;
}

.mint {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}

.minted {
  width: 100%;
}

.token-box {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}

.token-box code {
  flex: 1;
  min-width: 0;
  font-family: var(--mono);
  font-size: 12px;
  word-break: break-all;
  padding: 6px 8px;
  background: var(--surface-2);
  border-radius: var(--r-sm);
  border: 1px solid var(--border);
}

.token-list {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.token-row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  flex-wrap: wrap;
}

.token-row:last-child {
  border-bottom: 0;
}

.token-label {
  font-weight: 550;
  color: var(--text);
  min-width: 90px;
}

.info {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.info-row {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  font-size: 13px;
}

.info-row .label {
  width: 72px;
  flex: none;
  padding-top: 2px;
}

.roots {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  min-width: 0;
}

.root-label-input {
  width: 200px;
}

.root-path-input {
  width: 300px;
}

.root-add {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 6px;
  padding-top: 10px;
  border-top: 1px solid var(--lyra-border, rgba(128, 128, 128, 0.2));
}

.root-add-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.root {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.root .mono {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
