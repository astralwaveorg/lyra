import { computed, ref } from 'vue'

export type Theme = 'dark' | 'light'

const KEY = 'lyra-theme'
const current = ref<Theme>('dark')

function apply(theme: Theme) {
  current.value = theme
  const root = document.documentElement
  root.dataset.theme = theme
  // Element Plus 的暗色变量挂在 `.dark` 上，两套主题必须同步切换。
  root.classList.toggle('dark', theme === 'dark')
  const meta = document.querySelector('meta[name="theme-color"]')
  meta?.setAttribute('content', theme === 'dark' ? '#0b0e13' : '#f4f6fb')
}

export function setupTheme() {
  let saved: string | null = null
  try {
    saved = localStorage.getItem(KEY)
  } catch {
    /* 隐私模式下 localStorage 不可用 */
  }
  const prefersLight =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: light)').matches
  apply(saved === 'light' || saved === 'dark' ? (saved as Theme) : prefersLight ? 'light' : 'dark')
}

export function useTheme() {
  return {
    theme: computed(() => current.value),
    isDark: computed(() => current.value === 'dark'),
    toggle() {
      const next: Theme = current.value === 'dark' ? 'light' : 'dark'
      apply(next)
      try {
        localStorage.setItem(KEY, next)
      } catch {
        /* 忽略 */
      }
    },
  }
}

/** 窄屏侧栏抽屉。桌面端恒展开，由 CSS 决定，不参与这里的开关。 */
export const sidebarOpen = ref(false)
