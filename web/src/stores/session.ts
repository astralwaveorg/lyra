import { defineStore } from 'pinia'
import { api, setUnauthorizedHandler } from '@/api/client'

/**
 * 会话状态。
 *
 * 页面打开时先问一次 `/auth/me`：200 表示已有有效 Cookie（或服务未开启鉴权），
 * 401 才显示登录页。**不要靠「本地有没有 token」推断**——Cookie 是
 * HttpOnly 的，脚本根本读不到，只能由服务端回答。
 */
export const useSession = defineStore('session', {
  state: () => ({
    ready: false,
    authed: false,
    loginRequired: true,
    pending: false,
    username: '' as string,
    error: '' as string,
  }),
  actions: {
    async init() {
      setUnauthorizedHandler(() => {
        this.authed = false
        this.username = ''
        this.loginRequired = true
      })
      try {
        const me = await api.me()
        this.authed = true
        this.username = me.username || ''
        this.loginRequired = me.auth_required
      } catch {
        this.authed = false
        this.username = ''
        this.loginRequired = true
      } finally {
        this.ready = true
      }
    },
    async login(username: string, password: string) {
      this.pending = true
      this.error = ''
      try {
        await api.login(username, password)
        // 登录接口只说明「密码对」。真正确认会话可用要再走一次 /auth/me：
        // 反代或浏览器拒绝 Secure Cookie 时，登录会「成功」但下一次请求仍 401。
        const me = await api.me()
        this.authed = true
        this.username = me.username || ''
        this.loginRequired = me.auth_required
        return true
      } catch (error) {
        this.error = error instanceof Error ? error.message : '登录失败'
        return false
      } finally {
        this.pending = false
      }
    },
    async logout() {
      try {
        await api.logout()
      } finally {
        this.authed = false
        this.username = ''
        this.loginRequired = true
      }
    },
  },
})
