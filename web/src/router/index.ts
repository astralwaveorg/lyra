import { createRouter, createWebHistory } from 'vue-router'

/**
 * 路由即页面，也是侧栏的唯一来源。
 *
 * 分组按「你在这里做什么」划分：
 *   曲库 —— 看内容（概览、音乐、文件）
 *   维护 —— 修内容（审查、刮削、整理、回收与日志）
 *   系统 —— 关于这个工具本身（设置）
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
    path: '/library',
    name: 'library',
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
    path: '/review',
    name: 'review',
    component: () => import('@/views/ReviewView.vue'),
    meta: { title: '歌词审查', group: '维护', icon: 'CircleCheck' },
  },
  {
    path: '/scrape',
    name: 'scrape',
    component: () => import('@/views/ScrapeView.vue'),
    meta: { title: '歌词刮削', group: '维护', icon: 'MagicStick' },
  },
  {
    path: '/tags',
    name: 'tags',
    component: () => import('@/views/TagsScrapeView.vue'),
    meta: { title: '标签刮削', group: '维护', icon: 'PriceTag' },
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
    path: '/logs',
    name: 'logs',
    component: () => import('@/views/LogsView.vue'),
    meta: { title: '日志', group: '维护', icon: 'Document' },
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
  // 旧地址：歌词整理已并入「歌词审查 · 独立LRC」
  { path: '/orphans', redirect: '/review?tab=orphans' },
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
