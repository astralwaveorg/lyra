#!/usr/bin/env node
/**
 * 用无头 Chrome 给界面截图（零依赖，直接走 CDP）。
 *
 *   node tools/shot.mjs <origin> <path> <out.png> [options]
 *
 * 选项：
 *   --theme dark|light   写入 localStorage 后刷新（默认 dark）
 *   --width 1440         视口宽度
 *   --height 1100        视口高度
 *   --wait 2200          加载完成后再等多久（毫秒），等接口回来
 *   --full               截整页（默认只截视口）
 *   --cookie name=value  先写一条 Cookie（用于需要登录的页面）
 *   --eval "表达式"      截图前先求值并打印结果（调试界面状态用，支持 await）
 *
 * 为什么不用 Playwright：这里只需要「打开页面、设一个 localStorage、截图」，
 * 引入一整套浏览器下载不值得。CDP 是 Chrome 自带的能力，脚本 60 行就够。
 */

import { spawn } from 'node:child_process'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

const args = process.argv.slice(2)
const [origin, path, out] = args
if (!origin || !path || !out) {
  console.error('用法: node tools/shot.mjs <origin> <path> <out.png> [--theme dark|light]')
  process.exit(2)
}

function option(name, fallback) {
  const index = args.indexOf(name)
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback
}
const theme = option('--theme', 'dark')
const width = Number(option('--width', '1440'))
const height = Number(option('--height', '1100'))
const waitMs = Number(option('--wait', '2200'))
const full = args.includes('--full')
const cookie = option('--cookie', '')
const script = option('--eval', '')

// 页面自己的 JS 异常不会让截图失败，只会让界面变成空白——顺手收集起来打印。
const problems = []

const profile = mkdtempSync(join(tmpdir(), 'lyra-shot-'))
const port = 9500 + Math.floor(Math.random() * 300)
const chrome = spawn(
  CHROME,
  [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--hide-scrollbars',
    // 无头环境下没有「用户手势」，不加这个参数时 audio.play() 会被自动播放策略拒绝，
    // 看起来像「按钮没反应」。真实浏览器里点一下是允许播放的。
    '--autoplay-policy=no-user-gesture-required',
    '--force-color-profile=srgb',
    // 无头模式下若没有新帧产生，截图可能拿到**上一帧**：
    // 实测页面早就渲染完了，截出来却还是骨架屏。这两个开关强制
    // 合成器在截图前把所有阶段跑完。
    '--run-all-compositor-stages-before-draw',
    '--disable-new-content-rendering-timeout',
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${port}`,
    'about:blank',
  ],
  { stdio: 'ignore' },
)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function browserSocket() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/version`)
      if (response.ok) return (await response.json()).webSocketDebuggerUrl
    } catch {
      /* 还没起来 */
    }
    await sleep(120)
  }
  throw new Error('Chrome 调试端口没有就绪')
}

class Cdp {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    this.listeners = new Map()
    ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id)
        this.pending.delete(message.id)
        if (message.error) reject(new Error(JSON.stringify(message.error)))
        else resolve(message.result)
        return
      }
      const key = `${message.sessionId ?? ''}:${message.method}`
      for (const listener of this.listeners.get(key) ?? []) listener(message.params)
    })
  }

  send(method, params = {}, sessionId) {
    this.id += 1
    const id = this.id
    const payload = { id, method, params }
    if (sessionId) payload.sessionId = sessionId
    this.ws.send(JSON.stringify(payload))
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }))
  }

  once(method, sessionId) {
    return new Promise((resolve) => {
      const key = `${sessionId ?? ''}:${method}`
      const list = this.listeners.get(key) ?? []
      const handler = (params) => {
        this.listeners.set(key, (this.listeners.get(key) ?? []).filter((fn) => fn !== handler))
        resolve(params)
      }
      this.listeners.set(key, [...list, handler])
    })
  }
}

const socketUrl = await browserSocket()
const ws = new WebSocket(socketUrl)
await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, { once: true })
  ws.addEventListener('error', reject, { once: true })
})
const cdp = new Cdp(ws)

const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' })
const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true })

await cdp.send('Page.enable', {}, sessionId)
await cdp.send('Network.enable', {}, sessionId)
if (cookie) {
  const [name, ...rest] = cookie.split('=')
  const host = new URL(origin).hostname
  await cdp.send(
    'Network.setCookie',
    { name, value: rest.join('='), domain: host, path: '/', httpOnly: true, sameSite: 'Lax' },
    sessionId,
  )
}
await cdp.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false }, sessionId)

// 先把 localStorage 写好再进目标页，避免先渲染成浅色再闪一下。
await cdp.send('Runtime.enable', {}, sessionId)
cdp.listeners.set(`${sessionId}:Runtime.exceptionThrown`, [
  (payload) => problems.push(String(payload.exceptionDetails?.exception?.description ?? '').slice(0, 400)),
])

const loaded = cdp.once('Page.loadEventFired', sessionId)
await cdp.send('Page.navigate', { url: `${origin}/` }, sessionId)
await loaded
await cdp.send(
  'Runtime.evaluate',
  { expression: `localStorage.setItem('lyra-theme', ${JSON.stringify(theme)})` },
  sessionId,
)

const target = cdp.once('Page.loadEventFired', sessionId)
await cdp.send('Page.navigate', { url: `${origin}${path}` }, sessionId)
await target
await sleep(waitMs)

if (script) {
  const result = await cdp.send(
    'Runtime.evaluate',
    { expression: script, returnByValue: true, awaitPromise: true },
    sessionId,
  )
  console.log(JSON.stringify(result.result?.value ?? result.result ?? null, null, 2))
}
if (problems.length) console.log(`页面异常：\n${problems.join('\n')}`)

// 再推一帧：resize + 两次 rAF，确保最后的状态已经被合成。
await cdp.send(
  'Runtime.evaluate',
  {
    expression:
      "new Promise((r) => { window.dispatchEvent(new Event('resize')); requestAnimationFrame(() => requestAnimationFrame(() => r(true))); })",
    awaitPromise: true,
  },
  sessionId,
)

const shot = await cdp.send(
  'Page.captureScreenshot',
  { format: 'png', captureBeyondViewport: full, fromSurface: true },
  sessionId,
)
writeFileSync(out, Buffer.from(shot.data, 'base64'))

ws.close()
chrome.kill()
console.log(out)
process.exit(0)
