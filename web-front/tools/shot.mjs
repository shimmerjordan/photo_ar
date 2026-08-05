#!/usr/bin/env node
/**
 * 给页面拍照。开发时用眼睛看的那一步。
 *
 * ## 为什么需要它
 *
 * 这套界面的失败模式**全都是看得见但测不出来的**：木框的圆角错半格、点阵字落在半像素上
 * 变糊、桃色面板里落了一段深色底才该有的浅字、页签顶出来那 4px 把顶边压掉了。
 * 一条 `assert` 也逮不着，而它们全都是一眼就能看出来的。
 *
 * ## 为什么不用 puppeteer
 *
 * 与 `test/harness.js` 同一个理由：那会带进几百 MB 的 node_modules 和一份自己下载的
 * Chromium，而我们要看的正是**本机这个 Chrome** 渲染出来的样子。这里只用到 CDP 的四个
 * 方法，而 Node 22 自带了 WebSocket —— 依赖为零。
 *
 * ## 会话
 *
 * 登录之后的页面要 cookie。`--cookie` 走 `Network.setCookie` 注入，凭证只在命令行里、
 * 不落任何文件（这个仓库要进 git）。
 *
 * 用法：
 *   node tools/shot.mjs --base http://127.0.0.1:48099 --cookie "session=…" \
 *        --out /tmp/shots '#/photos' '#/settings' …
 *   不给路径时拍默认那一组（登录门 + 全部页面）。
 */
import { spawn } from 'node:child_process'
import { mkdir, writeFile, rm } from 'node:fs/promises'
import { mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

/** 默认拍这些。名字就是文件名。 */
const DEFAULT = [
  ['gate', ''],
  ['scan', '#/scan'],
  ['photos', '#/photos'],
  ['media', '#/media'],
  ['admin', '#/admin'],
  ['settings', '#/settings'],
  ['history', '#/history'],
  ['cache', '#/cache'],
]

// 参数：`--名 值` 成对吃掉，剩下的是要拍的 hash。
//
// 不用"前一个是不是 --开头"来判断值：`--out /tmp/x gate` 里的 `gate` 前一项是 `/tmp/x`，
// 不以 -- 开头，于是那种写法会把 `--out` 的值当成路径。踩过。
const argv = process.argv.slice(2)
const opts = {}
const paths = []
for (let i = 0; i < argv.length; i++) {
  if (argv[i].startsWith('--')) opts[argv[i].slice(2)] = argv[++i]
  else paths.push(argv[i])
}
const flag = (name, dflt) => opts[name] ?? dflt
const BASE = String(flag('base', 'http://127.0.0.1:48099')).replace(/\/+$/, '')
const COOKIE = flag('cookie', '')
const OUT = flag('out', '/tmp/photoar-shots')
const WIDTH = Number(flag('width', 390))
const HEIGHT = Number(flag('height', 844))
const DPR = Number(flag('dpr', 2))
const WAIT = Number(flag('wait', 3500))

/** 一个最小的 CDP 客户端。 */
class CDP {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data)
      const p = this.pending.get(m.id)
      if (p) {
        this.pending.delete(m.id)
        m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result)
      }
    })
  }

  send(method, params = {}) {
    const id = ++this.id
    this.ws.send(JSON.stringify({ id, method, params }))
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`${method} 超时`))
      }, 30_000)
    })
  }
}

async function main() {
  await mkdir(OUT, { recursive: true })
  const profile = await mkdtemp(join(tmpdir(), 'photoar-shot-'))
  const chrome = spawn(process.env.CHROME ?? 'google-chrome', [
    '--headless=new',
    '--remote-debugging-port=0',
    `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    // 自签证书（手机自测那套）也要能拍。这里只连 127.0.0.1，中间没有网络可被中间人。
    '--ignore-certificate-errors',
    // 无头环境没有真 GPU，而这个页面要 WebGL。
    '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader',
    // 相机：给一个假的，好让扫描页走到"开起来了"那条路而不是错误页。
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
    `--window-size=${WIDTH},${HEIGHT}`,
    'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] })

  const wsUrl = await new Promise((resolve, reject) => {
    let buf = ''
    const t = setTimeout(() => reject(new Error(`Chrome 没报出调试端口：${buf.slice(-500)}`)), 20_000)
    chrome.stderr.on('data', (d) => {
      buf += d
      const m = buf.match(/ws:\/\/[^\s]+/)
      if (m) { clearTimeout(t); resolve(m[0]) }
    })
    chrome.on('error', reject)
  })

  const ws = new WebSocket(wsUrl)
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j) })
  const browser = new CDP(ws)

  const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await browser.send('Target.attachToTarget', { targetId, flatten: true })
  // flatten 模式下每条消息要带 sessionId。包一层，省得每处都写。
  const cdp = {
    send: (method, params) => {
      const id = ++browser.id
      ws.send(JSON.stringify({ id, method, params, sessionId }))
      return new Promise((resolve, reject) => {
        browser.pending.set(id, { resolve, reject })
        setTimeout(() => {
          if (browser.pending.delete(id)) reject(new Error(`${method} 超时`))
        }, 60_000)
      })
    },
  }

  await cdp.send('Page.enable')
  await cdp.send('Runtime.enable')
  await cdp.send('Network.enable')
  // 手机视口。**deviceScaleFactor 必须给** —— 像素画在 DPR 1 和 DPR 3 下是两种东西，
  // 而这个界面只在手机上用。
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: WIDTH, height: HEIGHT, deviceScaleFactor: DPR, mobile: true,
  })
  if (COOKIE) {
    const [name, ...rest] = COOKIE.split('=')
    const url = new URL(BASE)
    await cdp.send('Network.setCookie', {
      name: name.trim(), value: rest.join('=').trim(),
      domain: url.hostname, path: '/', httpOnly: true, secure: url.protocol === 'https:',
    })
  }

  // 控制台里的错误一并收上来：截图看不出 "某个模块 404 了"，而那正是最常见的坏法。
  const problems = []
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data)
    if (m.method === 'Runtime.exceptionThrown') {
      problems.push(`异常 ${m.params.exceptionDetails?.exception?.description ?? m.params.exceptionDetails?.text}`)
    }
    if (m.method === 'Network.loadingFailed') problems.push(`加载失败 ${m.params.errorText}`)
  })

  const shots = paths.length ? paths.map((p) => [p.replace(/\W+/g, '_') || 'root', p]) : DEFAULT
  const report = []
  for (const [name, hash] of shots) {
    problems.length = 0
    await cdp.send('Page.navigate', { url: `${BASE}/${hash}` })
    await new Promise((r) => setTimeout(r, WAIT))
    // hash 变更不会触发导航，得自己改。
    if (hash) {
      await cdp.send('Runtime.evaluate', { expression: `location.hash = ${JSON.stringify(hash)}` })
      await new Promise((r) => setTimeout(r, 1500))
    }
    const { result } = await cdp.send('Runtime.evaluate', {
      expression: `JSON.stringify({
        title: document.getElementById('title')?.textContent ?? '',
        gate: !document.getElementById('gate')?.hidden,
        tabs: document.getElementById('tabbar')?.children.length ?? 0,
        boot: document.getElementById('boot')?.textContent ?? '',
        font: document.fonts.check('12px FusionPixel'),
      })`,
      returnByValue: true,
    })
    const { data } = await cdp.send('Page.captureScreenshot', { format: 'png' })
    const file = join(OUT, `${name}.png`)
    await writeFile(file, Buffer.from(data, 'base64'))
    const state = JSON.parse(result.value)
    report.push({ name, file, ...state, problems: [...new Set(problems)] })
    console.log(`${name.padEnd(10)} ${file}  ${JSON.stringify(state)}` +
      (problems.length ? `\n  问题：${[...new Set(problems)].join(' | ')}` : ''))
  }

  ws.close()
  chrome.kill('SIGKILL')
  await rm(profile, { recursive: true, force: true }).catch(() => {})
  return report.some((r) => r.problems.length) ? 1 : 0
}

main().then((c) => process.exit(c), (e) => { console.error(e); process.exit(1) })
