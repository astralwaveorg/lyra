import { describe, expect, it } from 'vitest'
import { fmtBytes, fmtDelta, fmtDuration, fmtPct, fmtSpan, splitPath } from '../format'

describe('fmtBytes', () => {
  it('小于 1KB 用字节', () => {
    expect(fmtBytes(512)).toBe('512 B')
  })
  it('按 1024 进制进位', () => {
    expect(fmtBytes(1536)).toBe('1.5 KB')
    expect(fmtBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })
  it('缺失值不显示成 0', () => {
    expect(fmtBytes(null)).toBe('—')
    expect(fmtBytes(undefined)).toBe('—')
  })
})

describe('fmtDuration', () => {
  it('一小时以内用 m:ss', () => {
    expect(fmtDuration(227)).toBe('3:47')
  })
  it('超过一小时补小时位', () => {
    expect(fmtDuration(3735)).toBe('1:02:15')
  })
  it('0 与空值视为没有时长', () => {
    expect(fmtDuration(0)).toBe('—')
    expect(fmtDuration(null)).toBe('—')
  })
})

describe('fmtSpan', () => {
  it('大数用天与小时', () => {
    expect(fmtSpan(3 * 86400 + 3600)).toBe('3 天 1 小时')
  })
  it('不足一天用小时与分钟', () => {
    expect(fmtSpan(3600 * 12 + 60 * 34)).toBe('12 小时 34 分')
  })
})

describe('fmtPct', () => {
  it('保留一位小数且不显示多余的 0', () => {
    expect(fmtPct(90.25)).toBe('90.3%')
    expect(fmtPct(100)).toBe('100%')
  })
})

describe('splitPath', () => {
  it('拆出目录与文件名', () => {
    expect(splitPath('A/B/C.flac')).toEqual({ dir: 'A/B', name: 'C.flac' })
  })
  it('根目录下的文件没有目录部分', () => {
    expect(splitPath('C.flac')).toEqual({ dir: '', name: 'C.flac' })
  })
})

describe('fmtDelta', () => {
  it('小于一秒标注为 <1s', () => {
    expect(fmtDelta(0.4)).toBe('<1s')
  })
  it('超过一分钟换算成分秒', () => {
    expect(fmtDelta(75)).toBe('1m15s')
  })
})
