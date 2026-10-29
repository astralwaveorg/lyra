import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { Health, Summary, TodoCounts, WatchStatus } from '@/api/types'

/**
 * 同一时刻只允许一次刷新在飞。
 *
 * 实际首屏会同时触发三处：App 挂载、登录状态变化、当前页面自己的初始化。
 * 不合并就是三份 summary+health+watch 请求，而且返回顺序不确定，
 * 页面会闪一下旧值。合并后所有调用方共享同一个 Promise。
 */
let inflight: Promise<void> | null = null

const EMPTY_TODO: TodoCounts = {
  lyrics: 0, tags: 0, covers: 0, organize: 0, orphans: 0, failed: 0,
}

/** 全局状态摘要：侧栏徽标、顶栏状态灯、写开关都读这里。 */
export const useMeta = defineStore('meta', {
  state: () => ({
    summary: null as Summary | null,
    health: null as Health | null,
    watch: null as WatchStatus | null,
    todoCounts: { ...EMPTY_TODO } as TodoCounts,
    loading: false,
    loadedAt: 0,
    timer: 0 as unknown as ReturnType<typeof setInterval> | 0,
  }),
  getters: {
    needsReview: (state) =>
      state.summary?.counts.review_tracks ?? state.summary?.counts.needs_review ?? 0,
    stagingPending: (state) => state.summary?.counts.staged_pending ?? 0,
    /** 六类待办总数：侧栏「待办」徽标与概览卡共用。 */
    todoTotal: (state) =>
      Object.values(state.todoCounts ?? EMPTY_TODO).reduce((sum, n) => sum + n, 0),
    issueTotal: (state) =>
      Object.values(state.summary?.issues ?? {}).reduce((sum, n) => sum + n, 0),
    writeEnabled: (state) => state.summary?.write_enabled ?? state.health?.write_enabled ?? false,
    version: (state) => state.summary?.version ?? state.health?.version ?? '',
    busy: (state) => state.summary?.busy ?? state.health?.busy ?? false,
    watchHealthy: (state) => state.watch?.healthy ?? false,
    watchAge: (state) => state.watch?.heartbeat_age_seconds ?? null,
  },
  actions: {
    async refresh() {
      if (inflight) return inflight
      inflight = this._refresh().finally(() => {
        inflight = null
      })
      return inflight
    },
    async _refresh() {
      this.loading = true
      try {
        const [summary, health, watch, todo] = await Promise.all([
          api.summary(),
          api.health(),
          api.watch().catch(() => null),
          // 待办计数失败不拖垮整个摘要（旧后端没有该端点时降级为 0）
          api.todo('', 1, 1).then((r) => r.counts).catch(() => null),
        ])
        this.summary = summary
        this.health = health
        this.watch = watch
        if (todo) this.todoCounts = todo
        this.loadedAt = Date.now()
      } finally {
        this.loading = false
      }
    },
    startPolling(intervalMs = 45000) {
      this.stopPolling()
      this.timer = setInterval(() => {
        // 页面在后台时不轮询：NAS 上这点负载不算什么，但省下来没坏处。
        if (document.visibilityState === 'visible') {
          void this.refresh().catch(() => undefined)
        }
      }, intervalMs)
    },
    stopPolling() {
      if (this.timer) {
        clearInterval(this.timer as ReturnType<typeof setInterval>)
        this.timer = 0 as unknown as ReturnType<typeof setInterval>
      }
    },
  },
})
