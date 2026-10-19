<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSession } from '@/stores/session'
import AppIcon from '@/components/AppIcon.vue'

const session = useSession()
const username = ref('')
const password = ref('')
const version = ref('')
const passwordRef = ref<HTMLInputElement>()

async function submit() {
  if (!username.value || !password.value || session.pending) return
  const ok = await session.login(username.value, password.value)
  if (ok) password.value = ''
}

onMounted(async () => {
  try {
    version.value = (await api.health()).version
  } catch {
    /* 健康检查失败也要能显示登录框：否则连不上时页面一片空白 */
  }
})
</script>

<template>
  <div class="login">
    <div class="card login-card">
      <div class="login-head">
        <div class="brand-mark"><AppIcon name="Headset" :size="18" /></div>
        <div>
          <div class="brand-name">LYRA</div>
          <div class="brand-sub">Music Library</div>
        </div>
      </div>

      <form class="login-form" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">用户名</span>
          <el-input v-model="username" autocomplete="username" placeholder="admin" size="large" />
        </label>
        <label class="field">
          <span class="field-label">密码</span>
          <el-input
            ref="passwordRef"
            v-model="password"
            type="password"
            autocomplete="current-password"
            show-password
            size="large"
            @keyup.enter="submit"
          />
        </label>

        <el-alert v-if="session.error" :title="session.error" type="error" :closable="false" show-icon />

        <el-button type="primary" size="large" native-type="submit" :loading="session.pending">
          登录
        </el-button>
      </form>

      <div class="login-foot dim">版本 {{ version || '—' }}</div>
    </div>
  </div>
</template>

<style scoped>
.login {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}

.login-card {
  width: 100%;
  max-width: 360px;
  padding: 26px 26px 20px;
  box-shadow: var(--shadow-lg);
}

.login-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 22px;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-label {
  font-size: 12px;
  color: var(--text-3);
}

.login-form :deep(.el-button) {
  margin-top: 4px;
  width: 100%;
}

.login-foot {
  margin-top: 18px;
  font-size: 11.5px;
  text-align: center;
}
</style>
