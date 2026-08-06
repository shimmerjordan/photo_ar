/**
 * 陈旧前端探测。**这个文件是为一类"代码明明是对的，但用户拿到的是旧的"而存在的。**
 *
 * ## 它防的那件事长什么样
 *
 * 部署完新版之后，用户看到的仍然是旧行为 —— 而且**没有任何迹象**：容器日志里
 * 版本号是新的、边缘节点上的文件是新的、`curl` 拿到的也是新的。只有那一个浏览器
 * 里是旧的，而它不会说。
 *
 * 真实踩过的一次：Cloudflare 的 **Browser Cache TTL** 被设成了 4 小时，它会
 * **覆盖源站的 `Cache-Control`**。源站发的是 `no-cache`（每次问一句、没变就 304），
 * 到浏览器手上变成 `max-age=14400`（四小时内连问都不问）。于是新发的 `api.js` 和
 * `theme.css` 在四个小时里对已经打开过页面的人**完全不存在**，表现是接口报 400
 * （旧客户端打新服务端）和"改的样式没生效"。两轮排查全花在这上面。
 *
 * ## 判据
 *
 * - `BUILD`：**跟着 JS 包一起被缓存**的版本号。浏览器手上的包是旧的，这个值就是旧的。
 * - `/api/config` 的 `version`：服务端**当下**的版本。那条接口是 `no-store`，
 *   而且路径不带静态后缀，CDN 不会缓存它 —— 所以它一定是新的。
 *
 * 两个不相等 ⇒ 这个浏览器手上的 JS 比服务端旧。这个推断**不会假阳**：两个值都由
 * 同一个 `PHOTOAR_VERSION` 生成，同一次部署里必然相同。
 *
 * ## 为什么修复动作不是 `location.reload()`
 *
 * 普通 reload 对一个还在 `max-age` 有效期内的资源**不发任何请求**，刷十次也是旧的。
 * （用户能做的只有长按刷新键"硬刷新"，而那件事没人知道。）
 *
 * 所以这里的做法是：把**这一页实际加载过的**同源 js/css 逐个用
 * `fetch(url, {cache: 'reload'})` 拉一遍 —— 那个选项会绕过缓存去问源站，并且
 * **把新内容写回 HTTP 缓存**。写回之后再 reload，模块就是新的了。
 * 清单来自 `performance.getEntriesByType('resource')`，也就是浏览器自己记录的
 * "我加载了这些" —— 比在代码里手写一份模块清单准，而且不会随重构过期。
 *
 * ## 局限（必须说清楚）
 *
 * **加这个探测的那一版自己救不了自己。** 用户手上如果是"还没有这段代码"的旧包，
 * 这段代码就不会跑。它从下一次部署开始生效。这是这类自检的固有形状，不是缺陷 ——
 * 但也意味着**它不能替代把 CDN 配对**（见 `docs/deploy-details.md`）。
 */

/** 这一版的版本号。由 web-front 在发这个文件时替换掉，见 `server/index.js`。 */
export const BUILD = '__PHOTOAR_VERSION__'

/** 占位符没被替换 = 不是从 web-front 发出来的（比如直接开文件）。那就别判。 */
export const known = () => BUILD !== '__PHOTOAR' + '_VERSION__'

/**
 * 浏览器手上的包是不是比服务端旧。
 *
 * @param serverVersion `/api/config` 的 `version`
 * @returns 旧了就返回 `{build, server}`，否则 null
 */
export function staleAgainst(serverVersion) {
  const s = (serverVersion ?? '').trim()
  // 两边任何一个不知道就不判 —— 宁可漏报。误报的代价是给一个没问题的用户弹一个
  // "你的页面是旧的"，而他刷新之后还是那句，那比不提示更糟。
  if (!s || !known()) return null
  if (s === BUILD) return null
  return { build: BUILD, server: s }
}

/**
 * 强制把这一页加载过的同源 js/css 重新取一遍，然后刷新。
 *
 * @param reload 注入点，测试用
 * @param fetchFn 注入点，测试用
 */
export async function hardRefresh({ reload, fetchFn } = {}) {
  const f = fetchFn ?? globalThis.fetch
  const rl = reload ?? (() => location.reload())
  let urls = []
  try {
    urls = performance.getEntriesByType('resource')
      .map((e) => e.name)
      .filter((n) => n.startsWith(location.origin) && /\.(js|css)(\?|$)/.test(n))
  } catch { /* 没有 performance API 就只 reload，至少 HTML 是新的 */ }
  // 去重：同一个 URL 被记录多次（比如预加载 + 真正加载）时不必取两遍。
  await Promise.all([...new Set(urls)].map((u) =>
    f(u, { cache: 'reload' }).catch(() => {})))
  rl()
}
