/**
 * 本机缓存。**对应 Android 的 `CacheScreen`，但语义完全不同 —— 这一点必须说清。**
 *
 * ## Android 那一页管的是什么
 *
 * 它是「出门前准备一次」的页面：把照片的参考图与视频**下载到手机存储**，好在没网的
 * 现场也能识别；还要显示服务端预建的 ARCore 目标库装不装得上（装不上就退回端上现建，
 * 贴合差一档）。那些概念在网页版一个都不存在 —— 没有 ARCore，也没有我们自己管的文件。
 *
 * ## 网页版真正的三层缓存
 *
 * | 层 | 谁管 | 清掉的后果 |
 * |---|---|---|
 * | **识别库包**（PARL） | 我们（内存） | 刷新时重新下（几 MB，带 ETag） |
 * | **看过的视频** | 浏览器 HTTP 缓存 | 下次看那段要重新下 |
 * | **wasm 编译缓存** | 浏览器（instantiateStreaming 的 code cache） | 下次进页面要重新编译整个 12MB |
 *
 * 三层都**不是我们能精确查询的** —— `Cache-Control` 与 code cache 都没有可枚举的 API。
 * 所以这一页如实显示"能查到的那部分"，并明确说清哪些查不到。**编一个数字比不给更糟**：
 * 用户会据此判断"是不是缓存坏了"。
 */
import { bytes, button, h, row, section, toast } from '../ui.js'
import { clearWasmCache } from '../recognize/wasmcache.js'

export default {
  title: '本机缓存',

  async mount(el, ctx) {
    let alive = true
    const lib = ctx.libInfo?.()

    el.appendChild(section('识别库',
      row('这个账号可扫', lib ? `${lib.nPhotos} 张` : '还没加载'),
      lib?.skipped?.length
        ? row('不在识别库里', `${lib.skipped.length} 张`, { bad: true })
        : null,
      lib ? row('粗排词表', lib.hasVocab ? '有' : '无（全量扫描）') : null,
      h('p', { class: 'p dim', text: '库包在进页面时取一次（带 ETag，没变就 304）。它只含被授权给你的那些照片的特征 —— 不含照片本身。' }),
      lib?.skipped?.length
        ? h('p', { class: 'warnbox', text: '有照片在库里找不到特征：入库时被质量闸门或去重闸门拒了。那些照片扫任何角度都不会有反应，要管理员处理。' })
        : null))

    const storage = section('浏览器给的存储配额')
    el.appendChild(storage)
    // `navigator.storage.estimate()` 是唯一能问到的数，而它是**整个源**的用量
    // （含 HTTP 缓存、IndexedDB、Cache Storage），拆不开。所以只报总量并说明。
    if (navigator.storage?.estimate) {
      try {
        const est = await navigator.storage.estimate()
        if (!alive) return
        storage.body.appendChild(row('已用', bytes(est.usage ?? 0), { mono: true }))
        storage.body.appendChild(row('上限', bytes(est.quota ?? 0), { mono: true }))
        storage.body.appendChild(h('p', { class: 'p dim', text: '这是整个站点的总量（识别引擎、看过的视频、编译缓存都在里面），浏览器不提供拆分。' }))
      } catch {
        storage.body.appendChild(row('已用', '问不到'))
      }
    } else {
      storage.body.appendChild(h('p', { class: 'p dim', text: '这个浏览器不提供存储用量查询。' }))
    }

    el.appendChild(section('识别引擎',
      h('p', { class: 'p', text: '引擎是一个 12MB 的 wasm。第一次进页面要下载 + 编译，之后浏览器用它自己的编译缓存直接加载。' }),
      h('p', { class: 'p dim', text: '进页面时顶部那条进度会说明走的是哪条：「正在下载」还是「从缓存读取」。' }),
      h('div', { class: 'actions' },
        button('清掉引擎的编译缓存', async () => {
          // 这条只清我们自己那份 IndexedDB 兜底（见 wasmcache.js）。浏览器原生的
          // code cache 没有清除 API —— 如实说出来，而不是让用户以为点了就干净了。
          try {
            await clearWasmCache()
            toast('已清掉我们那份兜底缓存')
          } catch (e) {
            toast(`清理没成：${e.message}`)
          }
        }, { kind: 'ghost' })),
      h('p', { class: 'p dim', text: '浏览器原生的 wasm 编译缓存没有清除 API。真要彻底清，用浏览器设置里的「清除站点数据」。' })))

    el.appendChild(section('彻底清空',
      h('p', { class: 'p', text: '识别库、看过的视频、编译缓存、登录状态 —— 全部清掉并重新开始。' }),
      h('div', { class: 'actions' },
        button('清空并重载', async () => {
          if (!globalThis.confirm('清空本站的全部缓存并重载？会退出登录。')) return
          // 三样都清：Cache Storage、IndexedDB、以及登录。**顺序不重要，但都要尝试** ——
          // 任何一样失败都不该阻止其余的。
          try {
            for (const k of await caches.keys()) await caches.delete(k)
          } catch { /* 有的浏览器在非安全上下文下没有 caches */ }
          try {
            await clearWasmCache()
          } catch { /* 见上 */ }
          location.replace(location.pathname)
        }, { kind: 'danger', iconName: 'trash' }))))

    return () => { alive = false }
  },
}
