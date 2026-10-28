<script setup lang="ts">
import { computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { routes } from '@/router'
import { useSession } from '@/stores/session'
import { useMeta } from '@/stores/meta'
import { sidebarOpen, useTheme } from '@/stores/ui'
import AppIcon from '@/components/AppIcon.vue'
import LoginView from '@/views/LoginView.vue'

const session = useSession()
const meta = useMeta()
const route = useRoute()
const router = useRouter()
const { isDark, toggle } = useTheme()

type NavItem = { path: string; title: string; icon: string; badge?: number; soon?: boolean }

const groups = computed(() => {
  const order = ['曲库', '维护', '系统']
  const buckets = new Map<string, NavItem[]>()
  for (const item of routes) {
    const metaInfo = item.meta as
      | { title?: string; group?: string; icon?: string; soon?: boolean }
      | undefined
    if (!item.name || !metaInfo?.title) continue
    const group = metaInfo.group ?? '其它'
    if (!buckets.has(group)) buckets.set(group, [])
    buckets.get(group)!.push({
      path: item.path,
      title: metaInfo.title,
      icon: metaInfo.icon ?? 'Document',
      soon: metaInfo.soon,
      badge: item.name === 'todo' && meta.todoTotal ? meta.todoTotal : undefined,
    })
  }
  return order
    .filter((name) => buckets.has(name))
    .map((name) => ({ name, items: buckets.get(name)! }))
})

const pageTitle = computed(() => (route.meta.title as string | undefined) ?? '')

function go(path: string) {
  sidebarOpen.value = false
  if (route.path !== path) void router.push(path)
}

async function signOut() {
  await session.logout()
}

onMounted(async () => {
  await session.init()
  if (session.authed) {
    await meta.refresh().catch(() => undefined)
    meta.startPolling()
  }
})

watch(
  () => session.authed,
  async (authed) => {
    if (authed) {
      await meta.refresh().catch(() => undefined)
      meta.startPolling()
    } else {
      meta.stopPolling()
    }
  },
)

onUnmounted(() => meta.stopPolling())
</script>

<template>
  <div v-if="!session.ready" class="boot-screen">
    <div class="boot-mark"><AppIcon name="Headset" :size="22" /></div>
    <span class="dim">正在连接 Lyra</span>
  </div>

  <LoginView v-else-if="!session.authed" />

  <div v-else class="shell">
    <div v-if="sidebarOpen" class="scrim" @click="sidebarOpen = false" />

    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <router-link to="/overview" class="brand" @click="sidebarOpen = false">
        <div class="brand-mark"><AppIcon name="Headset" :size="18" /></div>
        <div>
          <div class="brand-name">LYRA</div>
          <div class="brand-sub">Music Library</div>
        </div>
      </router-link>
      <el-button
        class="close-btn close-drawer"
        text
        circle
        @click="sidebarOpen = false"
      >
        <AppIcon name="Close" />
      </el-button>

      <template v-for="group in groups" :key="group.name">
        <div class="nav-group">{{ group.name }}</div>
        <div
          v-for="item in group.items"
          :key="item.path"
          class="nav-item"
          :class="{ active: route.path === item.path }"
          @click="go(item.path)"
        >
          <AppIcon :name="item.icon" :size="16" />
          <span>{{ item.title }}</span>
          <span v-if="item.soon" class="soon">未开发</span>
          <span v-if="item.badge" class="badge num">{{ item.badge > 99 ? '99+' : item.badge }}</span>
        </div>
      </template>

      <div class="sidebar-foot">
        <div class="row">
          <span class="status-dot" :class="meta.watchHealthy ? '' : 'warn'" />
          <span>{{ meta.watchHealthy ? '监控运行中' : '监控无心跳' }}</span>
        </div>
        <div class="dim">版本 {{ meta.version || '—' }}</div>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <el-button class="menu-btn" text circle @click="sidebarOpen = true">
          <AppIcon name="Menu" :size="18" />
        </el-button>
        <router-link to="/overview" class="title-link" title="回到概览">
          <h1 class="topbar-title">{{ pageTitle }}</h1>
          <span id="page-count" class="topbar-count" />
        </router-link>
        <span class="topbar-spacer" />
        <div id="page-actions" class="topbar-actions" />

        <el-tooltip
          :content="meta.writeEnabled ? '写入已开启，可以改写文件' : '只读模式，写入开关关闭'"
          placement="bottom"
        >
          <span class="status-pill topbar-pill">
            <span class="status-dot" :class="meta.writeEnabled ? '' : 'warn'" />
            {{ meta.writeEnabled ? '可写' : '只读' }}
          </span>
        </el-tooltip>

        <el-tooltip
          v-if="meta.busy"
          content="有任务正在运行"
          placement="bottom"
        >
          <span class="status-pill topbar-pill">
            <span class="status-dot warn" />
            任务运行中
          </span>
        </el-tooltip>

        <el-button text circle :title="isDark ? '切换到浅色' : '切换到深色'" @click="toggle">
          <AppIcon :name="isDark ? 'Sunny' : 'Moon'" :size="17" />
        </el-button>

        <el-dropdown trigger="click" @command="(cmd: string) => (cmd === 'out' ? signOut() : go('/settings'))">
          <span class="account">
            <span class="avatar"><AppIcon name="User" :size="14" /></span>
            <span class="account-name">{{ session.username || '已登录' }}</span>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="settings">设置</el-dropdown-item>
              <el-dropdown-item command="out" divided>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </header>

      <main class="page">
        <router-view v-slot="{ Component }">
          <component :is="Component" />
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 8px 16px;
}

.close-drawer {
  position: absolute;
  top: 14px;
  right: 10px;
}

.boot-screen {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
}

.boot-mark {
  width: 44px;
  height: 44px;
  border-radius: 13px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
  border: 1px solid var(--accent-line);
}

.menu-btn {
  display: none;
}

.row {
  display: flex;
  align-items: center;
  gap: 7px;
}

.account {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 8px 4px 4px;
  border-radius: 999px;
  border: 1px solid transparent;
  color: var(--text-2);
  font-size: 12.5px;
  outline: none;
}

.account:hover {
  background: var(--surface-2);
  border-color: var(--border);
}

.avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--surface-3);
  color: var(--text-2);
}

.close-btn {
  display: none;
}

@media (max-width: 959px) {
  .menu-btn,
  .close-btn {
    display: inline-flex;
  }

  .account-name {
    display: none;
  }
}

@media (max-width: 560px) {
  .topbar-title {
    font-size: 14px;
  }
}
</style>
