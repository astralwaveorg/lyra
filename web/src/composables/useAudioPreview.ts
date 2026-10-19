import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'

/**
 * 全局只保留一个试听播放器。
 *
 * 为什么放在模块级：曲目可能在多个地方出现（推荐采纳、待审查、抽屉），
 * 每处各建一个 <audio> 会出现两条同时播放。共用一个元素、共享状态，
 * 点到哪首就切哪首。
 */
let audio: HTMLAudioElement | null = null

const playingKey = ref<string | null>(null)
const pendingKey = ref<string | null>(null)

function key(root: string, path: string): string {
  return `${root}/${path}`
}

function ensure(): HTMLAudioElement {
  if (audio) return audio
  const element = new Audio()
  element.preload = 'none'
  element.addEventListener('playing', () => {
    pendingKey.value = null
  })
  element.addEventListener('pause', () => {
    playingKey.value = null
    pendingKey.value = null
  })
  element.addEventListener('ended', () => {
    playingKey.value = null
    pendingKey.value = null
  })
  element.addEventListener('error', () => {
    playingKey.value = null
    pendingKey.value = null
    ElMessage.warning('试听失败：这个文件无法播放')
  })
  audio = element
  return element
}

export function useAudioPreview() {
  function isPlaying(root: string, path: string): boolean {
    return playingKey.value === key(root, path)
  }

  function isPending(root: string, path: string): boolean {
    return pendingKey.value === key(root, path)
  }

  function toggle(root: string, path: string): void {
    const element = ensure()
    const target = key(root, path)
    if (playingKey.value === target) {
      element.pause()
      return
    }
    if (pendingKey.value === target) return
    element.pause()
    playingKey.value = null
    pendingKey.value = target
    element.src = api.mediaUrl(root, path)
    element
      .play()
      .then(() => {
        playingKey.value = target
        pendingKey.value = null
      })
      .catch(() => {
        pendingKey.value = null
      })
  }

  function stop(): void {
    audio?.pause()
  }

  return {
    playing: computed(() => playingKey.value),
    pending: computed(() => pendingKey.value),
    toggle,
    stop,
    isPlaying,
    isPending,
  }
}
