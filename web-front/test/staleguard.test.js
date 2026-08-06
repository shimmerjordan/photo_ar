/**
 * 陈旧前端探测。**为一次真实的、两轮都没查出来的事故写的。**
 *
 * 事故形状：Cloudflare 的 Browser Cache TTL 覆盖了源站的 `Cache-Control: no-cache`，
 * 发下去的是 `max-age=14400`。于是新版的 `api.js` / `theme.css` 在四小时里对已经打开过
 * 页面的人不存在 —— 而容器日志、边缘节点、`curl` 三处看到的全是新版。表现是接口 400
 * 和"样式没生效"，两个都指向别处。
 *
 * 这里钉的是判据的**边界**：什么时候该报、什么时候**绝不能**报。误报比漏报糟得多 ——
 * 一个没问题的用户被告知"你的页面是旧的"，而他刷新之后还是那句。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { BUILD, hardRefresh, known, staleAgainst } from '../public/staleguard.js'

describe('staleAgainst：什么时候判为旧', () => {
  test('源码里的占位符没被替换时，一律不判', () => {
    // 直接从仓库读这个文件（没经过 web-front 的替换），BUILD 就是占位符。
    // 这时任何服务端版本都不该报 —— 本地 `node server/index.js` 之外还有
    // "直接开文件看看"这种用法，给它弹一个假警报是纯粹的噪声。
    assert.equal(known(), false)
    assert.equal(staleAgainst('0.1.2'), null)
    assert.equal(staleAgainst('sha-abcdef0'), null)
  })

  test('服务端版本拿不到时不判', () => {
    // `/api/config` 挂了、或者老服务端不返回 version。漏报，不误报。
    assert.equal(staleAgainst(undefined), null)
    assert.equal(staleAgainst(''), null)
    assert.equal(staleAgainst('   '), null)
  })

  test('BUILD 是占位符这件事本身要能被看出来', () => {
    // 这条钉的是"占位符长什么样"——server/index.js 里的 replaceAll 用的是同一个串，
    // 两边分叉的话替换会静默失败，而那时 known() 永远 false、探测器永远不工作。
    assert.equal(BUILD, '__PHOTOAR_VERSION__')
  })
})

describe('staleAgainst：替换过之后的判定', () => {
  // 模拟 web-front 替换之后的模块。用同一份源码做，免得测试里的判定逻辑
  // 和真正跑的那份分叉。
  const load = async (version) => {
    const src = (await import('node:fs/promises')).readFile
    const code = (await src(new URL('../public/staleguard.js', import.meta.url), 'utf8'))
      .replaceAll('__PHOTOAR_VERSION__', version)
    return import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
  }

  test('版本相同 = 不旧', async () => {
    const m = await load('sha-2bb5095')
    assert.equal(m.staleAgainst('sha-2bb5095'), null)
  })

  test('版本不同 = 旧，并且两个版本号都带出来', async () => {
    const m = await load('sha-2bb5095')
    // 报错文案要印出这两个值 —— 那是这句话唯一能被验证的部分。
    assert.deepEqual(m.staleAgainst('sha-9999999'), {
      build: 'sha-2bb5095', server: 'sha-9999999',
    })
  })

  test('服务端版本两边空白不算差异', async () => {
    const m = await load('0.1.1')
    assert.equal(m.staleAgainst('  0.1.1  '), null)
  })
})

describe('hardRefresh：为什么不能只 reload', () => {
  const withEnv = async (fn) => {
    const g = globalThis
    const old = { performance: g.performance, location: g.location }
    const entries = [
      { name: 'https://x.test/app.js' },
      { name: 'https://x.test/api.js' },
      { name: 'https://x.test/api.js' },        // 重复：预加载 + 真正加载
      { name: 'https://x.test/theme.css' },
      { name: 'https://x.test/vendor/opencv.wasm' },  // 不是 js/css，不该取
      { name: 'https://other.test/a.js' },      // 跨源，不该取
      { name: 'https://x.test/api/lib' },       // 没有后缀，不该取
    ]
    g.performance = { getEntriesByType: () => entries }
    g.location = { origin: 'https://x.test' }
    try { return await fn() } finally { Object.assign(g, old) }
  }

  test('只取同源的 js/css，去重，且必须 cache:reload', async () => {
    await withEnv(async () => {
      const got = []
      let reloaded = 0
      await hardRefresh({
        reload: () => { reloaded++ },
        fetchFn: async (u, o) => { got.push([u, o?.cache]); return {} },
      })
      const urls = got.map((g) => g[0]).sort()
      assert.deepEqual(urls, [
        'https://x.test/api.js', 'https://x.test/app.js', 'https://x.test/theme.css',
      ])
      // **`cache: 'reload'` 是这整个函数的全部意义**：普通 reload 对还在 max-age
      // 有效期内的资源一个请求都不发，刷十次也是旧的。这个选项绕过缓存去问源站，
      // 并把新内容写回 HTTP 缓存 —— 写回之后再 reload，模块才是新的。
      assert.ok(got.every((g) => g[1] === 'reload'), '每一个都要 cache:reload')
      assert.equal(reloaded, 1, '取完之后要刷新一次')
    })
  })

  test('某个资源取失败也要继续刷新', async () => {
    await withEnv(async () => {
      let reloaded = 0
      await hardRefresh({
        reload: () => { reloaded++ },
        fetchFn: async (u) => { if (u.endsWith('api.js')) throw new Error('网断了'); return {} },
      })
      // 取不到就取不到 —— 刷新之后至少 HTML 是新的，而且下一次进来还会再判一遍。
      // 卡在这里不刷新的话，用户面对的是一个按了没反应的按钮。
      assert.equal(reloaded, 1)
    })
  })
})
