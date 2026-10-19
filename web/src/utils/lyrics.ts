export type LyricLine = { ms: number | null; stamp: string; text: string }

const TIME_TAG = /\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g
const WORD_TAG = /<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>/g
const META_TAG = /^\[(ti|ar|al|by|offset|re|ve|length|au|tool):/i

function stampOf(ms: number): string {
  const total = Math.max(0, Math.round(ms))
  const m = Math.floor(total / 60000)
  const s = Math.floor((total % 60000) / 1000)
  const cs = Math.floor((total % 1000) / 10)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(cs).padStart(2, '0')}`
}

/** 从原始 LRC 文本解析出行。解析只是为了预览，不参与任何写入判断。 */
export function parseLrc(text: string): LyricLine[] {
  const rows: { ms: number | null; text: string }[] = []
  for (const raw of (text || '').split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || META_TAG.test(line)) continue
    TIME_TAG.lastIndex = 0
    const times: number[] = []
    let match: RegExpExecArray | null
    while ((match = TIME_TAG.exec(line)) !== null) {
      const m = Number(match[1] ?? 0)
      const s = Number(match[2] ?? 0)
      const frac = match[3] ? Number(match[3].padEnd(3, '0')) : 0
      times.push(m * 60000 + s * 1000 + frac)
    }
    const body = line.replace(TIME_TAG, '').replace(WORD_TAG, '').trim()
    if (!times.length) {
      rows.push({ ms: null, text: body || line })
      continue
    }
    for (const ms of times) rows.push({ ms, text: body })
  }
  return rows
    .filter((row) => row.text.length > 0 || row.ms !== null)
    .map((row) => ({ ms: row.ms, stamp: row.ms === null ? '' : stampOf(row.ms), text: row.text }))
}

/** 只保留歌词正文，用于比对与重合度估算。 */
export function lyricBodies(lines: LyricLine[]): string[] {
  return lines.map((line) => line.text.trim()).filter((text) => text.length > 0)
}

/**
 * 两份歌词的对齐。
 *
 * 用最长公共子序列而不是逐行比较：两侧行数往往不同（一份多出副歌重复段），
 * 逐行比较会让后面所有行都显示成「已修改」，读起来全是噪音。
 * 行数上限 800，超出直接截断——这是给人看的对照，不是通用 diff 工具。
 */
export function alignLyrics(
  left: string[],
  right: string[],
): { left: string; right: string; kind: 'same' | 'add' | 'del' | 'edit' }[] {
  const a = left.slice(0, 800)
  const b = right.slice(0, 800)
  const n = a.length
  const m = b.length
  // LCS 长度表。800×800 的 Int32 表约 2.5MB，一次性用完即丢，可以接受。
  const table = new Int32Array((n + 1) * (m + 1))
  const at = (i: number, j: number) => i * (m + 1) + j
  const norm = (s: string) => s.replace(/\s+/g, '')
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[at(i, j)] =
        norm(a[i]) === norm(b[j])
          ? table[at(i + 1, j + 1)] + 1
          : Math.max(table[at(i + 1, j)], table[at(i, j + 1)])
    }
  }
  const rows: { left: string; right: string; kind: 'same' | 'add' | 'del' | 'edit' }[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (norm(a[i]) === norm(b[j])) {
      rows.push({ left: a[i], right: b[j], kind: 'same' })
      i += 1
      j += 1
    } else if (table[at(i + 1, j)] >= table[at(i, j + 1)]) {
      rows.push({ left: a[i], right: '', kind: 'del' })
      i += 1
    } else {
      rows.push({ left: '', right: b[j], kind: 'add' })
      j += 1
    }
  }
  while (i < n) rows.push({ left: a[i++], right: '', kind: 'del' })
  while (j < m) rows.push({ left: '', right: b[j++], kind: 'add' })
  return rows
}
