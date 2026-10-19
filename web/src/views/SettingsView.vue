<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { TagPolicy, TokenRow } from '@/api/types'
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
    ElMessage.warning('给令牌起个名字，方便以后认出来')
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
  await Promise.all([loadTokens(), loadTagPolicy()])
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
      <span class="card-title">标签刮削</span>
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
          自动扫描间隔会同步显示在「标签刮削」页的扫描状态里
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
    <div class="card-head"><span class="card-title">修改密码</span></div>
    <div class="card-body form">
      <el-input v-model="currentPassword" type="password" show-password placeholder="当前密码" autocomplete="current-password" />
      <el-input v-model="newPassword" type="password" show-password placeholder="新密码，至少 10 位" autocomplete="new-password" />
      <el-input v-model="confirmPassword" type="password" show-password placeholder="再输一次新密码" autocomplete="new-password" />
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
          <div v-for="root in summary?.roots ?? []" :key="root.name" class="root">
            <el-tag size="small" type="info">{{ root.name }}</el-tag>
            <span class="mono cell-meta">{{ root.path }}</span>
          </div>
          <span v-if="!summary?.roots.length" class="dim">没有配置受管目录</span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
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
  gap: 5px;
  min-width: 0;
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
