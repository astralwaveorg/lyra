import { createRouter, createWebHistory } from 'vue-router'

/**
 * 路由即页面，也是侧栏的唯一来源。
 *
 * 分组按「你在这里做什么」划分（重构后的信息架构）：
 *   曲库 —— 看内容与操作（概览、音乐、文件）
 *   维护 —— 修内容（待办、自动化、整理、回收站、活动）
 *   系统 —— 关于这个工具本身（设置）
 *
 * 操作与审查的关系：音乐页是对象驱动的编辑面，待办页是队列驱动的
 * 审查面；两边点开任何一首歌都落到同一个详情抽屉。
 */
export const routes = [
  { path: '/', redirect: '/overview' },
  {
    path: '/overview',
    name: 'overview',
    component: () => import('@/views/OverviewView.vue'),
    meta: { title: '概览', group: '曲库', icon: 'DataAnalysis' },
  },
  {
    path: '/music',
    name: 'music',
    component: () => import('@/views/LibraryView.vue'),
    meta: { title: '音乐', group: '曲库', icon: 'Headset' },
  },
  {
    path: '/files',
    name: 'files',
    component: () => import('@/views/FilesView.vue'),
    meta: { title: '文件', group: '曲库', icon: 'FolderOpened' },
  },
  {
    path: '/todo',
    name: 'todo',
    component: () => import('@/views/TodoView.vue'),
    meta: { title: '待办', group: '维护', icon: 'Bell' },
  },
  {
    path: '/automation',
    name: 'automation',
    component: () => import('@/views/AutomationView.vue'),
    meta: { title: '自动化', group: '维护', icon: 'MagicStick' },
  },
  {
    path: '/organize',
    name: 'organize',
    component: () => import('@/views/OrganizeView.vue'),
    meta: { title: '曲库整理', group: '维护', icon: 'Sort' },
  },
  {
    path: '/trash',
    name: 'trash',
    component: () => import('@/views/TrashView.vue'),
    meta: { title: '回收站', group: '维护', icon: 'Delete' },
  },
  {
    path: '/activity',
    name: 'activity',
    component: () => import('@/views/LogsView.vue'),
    meta: { title: '活动', group: '维护', icon: 'Document' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置', group: '系统', icon: 'Setting' },
  },
  {
    path: '/about',
    name: 'about',
    component: () => import('@/views/AboutView.vue'),
    meta: { title: '关于', group: '系统', icon: 'InfoFilled' },
  },
  // ---- 旧地址重定向（客户端路由，保留外部书签可用） ----
  { path: '/library', redirect: '/music' },
  {
    // /review?tab=safe|pending|ignored 深链要把页签带过去，不能整体丢
    path: '/review',
    redirect: (to: { query: Record<string, unknown> }) => {
      const tab = typeof to.query.tab === 'string' ? to.query.tab : ''
      const kindTab = ['safe', 'pending', 'ignored'].includes(tab) ? `&lyrics_tab=${tab}` : ''
      return `/todo?kind=lyrics${kindTab}`
    },
  },
  { path: '/scrape', redirect: '/automation' },
  { path: '/tags', redirect: '/todo?kind=tags' },
  { path: '/logs', redirect: '/activity' },
  // 旧地址：歌词整理已并入「待办 · 歌词 · 独立LRC」
  { path: '/orphans', redirect: '/todo?kind=lyrics&lyrics_tab=orphans' },
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, saved) {
    return saved ?? { top: 0 }
  },
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? ''
  document.title = title ? `${title} · Lyra` : 'Lyra'
})
