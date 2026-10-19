import { onMounted, onUnmounted, ref } from 'vue'

/** 响应式断点：与 CSS 里的断点保持一致（见 styles/base.css）。 */
export function useMedia(query: string) {
  const matches = ref(false)
  let mql: MediaQueryList | null = null
  const update = () => {
    matches.value = mql?.matches ?? false
  }

  onMounted(() => {
    if (typeof window.matchMedia !== 'function') return
    mql = window.matchMedia(query)
    update()
    mql.addEventListener('change', update)
  })

  onUnmounted(() => {
    mql?.removeEventListener('change', update)
  })

  return matches
}

/** 窄屏：表格转卡片。 */
export function useNarrow() {
  return useMedia('(max-width: 720px)')
}
