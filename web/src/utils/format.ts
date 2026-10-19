/** 展示用的格式化。全部返回字符串，不做单位隐藏。 */

export function fmtBytes(bytes: number | null | undefined, digits = 1): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(value >= 100 ? 0 : digits)} ${units[index]}`
}

/** 单曲时长：`3:47` / `1:02:15`。 */
export function fmtDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return '—'
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
}

/** 曲库总时长：`12 小时 34 分`。数字大时读起来比秒数有意义。 */
export function fmtSpan(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return '—'
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.round((total % 3600) / 60)
  if (h >= 24) {
    const d = Math.floor(h / 24)
    return `${d} 天 ${h % 24} 小时`
  }
  if (h > 0) return `${h} 小时 ${m} 分`
  return `${m} 分钟`
}

export function fmtPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const rounded = Number(value.toFixed(digits))
  return `${rounded}%`
}

export function fmtNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString('zh-CN')
}

export function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return '—'
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`
  return fmtDate(ts)
}

export function fmtDate(ts: number | null | undefined): string {
  if (!ts) return '—'
  const date = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`
}

export function fmtClock(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return ''
  const total = Math.max(0, Math.round(ms / 1000))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/** `A/B/C.flac` → `{ dir: 'A/B', name: 'C.flac' }`。 */
export function splitPath(path: string): { dir: string; name: string } {
  const index = path.lastIndexOf('/')
  return index < 0
    ? { dir: '', name: path }
    : { dir: path.slice(0, index), name: path.slice(index + 1) }
}

export function fmtScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return '—'
  return score.toFixed(2)
}

export function fmtDelta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const value = Math.abs(seconds)
  if (value < 1) return '<1s'
  if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)}s`
  return `${Math.floor(value / 60)}m${Math.round(value % 60)}s`
}

/** 扫描间隔：小于一天用小时，整天的倍数用天。 */
export function fmtInterval(hours: number | null | undefined): string {
  if (hours === null || hours === undefined || !Number.isFinite(hours) || hours <= 0) {
    return '—'
  }
  if (hours < 1) return `${Math.round(hours * 60)} 分钟`
  // 一天和一天半仍按小时显示，保持“每 24 小时”这种习惯说法；
  // 两天以上再折算成天，避免出现“每 168 小时”。
  if (hours >= 48) {
    const days = hours / 24
    return `${Number.isInteger(days) ? days : days.toFixed(1)} 天`
  }
  return `${Number.isInteger(hours) ? hours : hours.toFixed(1)} 小时`
}
