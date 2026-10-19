import { describe, expect, it } from 'vitest'
import { alignLyrics, lyricBodies, parseLrc } from '../lyrics'

describe('parseLrc', () => {
  it('解析时间轴与正文', () => {
    const lines = parseLrc('[00:12.34]第一句\n[00:15.00]第二句')
    expect(lines).toHaveLength(2)
    expect(lines[0]).toMatchObject({ ms: 12340, text: '第一句' })
    expect(lines[0].stamp).toBe('00:12.34')
  })

  it('一行多个时间戳展开成多行', () => {
    const lines = parseLrc('[00:01.00][00:31.00]副歌')
    expect(lines.map((line) => line.text)).toEqual(['副歌', '副歌'])
  })

  it('丢掉元信息行', () => {
    const lines = parseLrc('[ti:歌名]\n[ar:歌手]\n[00:01.00]正文')
    expect(lines).toHaveLength(1)
    expect(lines[0].text).toBe('正文')
  })

  it('去掉逐字时间标签', () => {
    const lines = parseLrc('[00:01.00]<00:01.00>逐<00:01.50>字')
    expect(lines[0].text).toBe('逐字')
  })

  it('没有时间轴的纯文本也保留', () => {
    const lines = parseLrc('第一句\n第二句')
    expect(lines).toHaveLength(2)
    expect(lines[0].ms).toBeNull()
  })
})

describe('lyricBodies', () => {
  it('只保留非空正文', () => {
    expect(lyricBodies(parseLrc('[00:01.00]A\n\n[00:02.00]B'))).toEqual(['A', 'B'])
  })
})

describe('alignLyrics', () => {
  it('相同的行标为 same', () => {
    const rows = alignLyrics(['a', 'b'], ['a', 'b'])
    expect(rows.every((row) => row.kind === 'same')).toBe(true)
  })

  it('多出来的一行标为 add', () => {
    const rows = alignLyrics(['a'], ['a', 'b'])
    expect(rows.map((row) => row.kind)).toEqual(['same', 'add'])
  })

  it('缺失的一行标为 del', () => {
    const rows = alignLyrics(['a', 'b'], ['a'])
    expect(rows.map((row) => row.kind)).toEqual(['same', 'del'])
  })

  it('忽略空白差异', () => {
    const rows = alignLyrics(['a b'], ['ab'])
    expect(rows[0].kind).toBe('same')
  })
})
