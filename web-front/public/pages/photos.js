/**
 * 媒体库：**照片 + 它配的那段视频**。
 *
 * 一屏网格，每张显示缩略图 + 标题 + 两个警示徽标（无视频 / 参考图变了）。
 * 那两个徽标不是装饰：它们各自对应一种「扫了不会有反应」的状态，而那是用户最可能
 * 来这一页查的事。
 *
 * ## 这一页对两种角色是不同的东西
 *
 * 网格本身共用（`/v1/photos` 服务端就按授权过滤，宾客拿到的行数天然是对的），
 * 差别只有两处，都在这个文件里显式分叉：
 *
 * | | 管理员 | 宾客 |
 * |---|---|---|
 * | 点一张进哪 | `DETAIL`（NAS 路径、自匹配、删除） | `VIEW`（大图、视频、存到手机） |
 * | 空了说什么 | 去「素材」页传 | 去找管理员要授权 |
 * | 徽标 | 全给 | 只给「无视频」 |
 *
 * 徽标那一条值得说明：「参考图变了」是一条**给策展人看**的运维状态（它意味着要重新
 * 入库），宾客对它无能为力，摆在他面前只会让他以为自己的照片坏了。「无视频」相反 ——
 * 它解释了"为什么我扫了没反应"，那是宾客真会问的问题。
 */
import * as api from '../api.js'
import { empty, failed, h, loading, thumb } from '../ui.js'
import { Page } from '../navpolicy.js'

export default {
  title: '媒体库',

  async mount(el, ctx) {
    let alive = true
    const isAdmin = ctx.isAdmin()
    el.appendChild(loading('正在取照片…'))

    const render = (list) => {
      if (!alive) return
      el.innerHTML = ''
      if (!list.length) {
        el.appendChild(isAdmin
          ? empty('媒体库是空的', '去「素材」页挑一张打印过的照片 + 一段视频，一次传完就是一组映射。')
          // 宾客这一句必须给出**他能做的下一步**，而"去传一张"不是（他没有那个页签，
          // 服务端也会 403）。他能做的只有一件事：找管理员。
          : empty('还没有照片给你', '管理员还没有把照片授权给你。找他要一下，之后刷新这一页就能看到。'))
        return
      }
      const grid = h('div', { class: 'grid' })
      for (const p of list) {
        const id = p.photoId ?? p.id
        const flags = []
        // 这两条与 Android 的同一批文案。它们回答「为什么扫了没反应」——
        // 而那是这一页存在的主要理由之一。
        if (p.hasVideo === false) flags.push(h('span', { class: 'badge', text: '无视频' }))
        // 「参考图变了」只给管理员：宾客对它无能为力（要重新入库），
        // 摆在他面前只会让他以为自己的照片坏了。
        if (isAdmin && p.refStale) flags.push(h('span', { class: 'badge warn', text: '参考图变了' }))
        grid.appendChild(h('button', {
          class: 'card', onclick: () => ctx.shell.push(isAdmin ? Page.DETAIL : Page.VIEW, { id }),
        },
          thumb(id, ctx.shell.libraryRev, p.title ?? '照片'),
          h('div', { class: 'card-b' },
            h('span', { class: 'card-t', text: p.title || '（未命名）' }),
            flags.length ? h('span', { class: 'flags' }, ...flags) : null)))
      }
      el.appendChild(grid)
      el.appendChild(h('p', { class: 'note', text: `共 ${list.length} 张` }))
    }

    const load = async () => {
      el.innerHTML = ''
      el.appendChild(loading('正在取照片…'))
      try {
        render(await api.photos())
      } catch (e) {
        if (!alive) return
        el.innerHTML = ''
        el.appendChild(failed(e.message, load))
      }
    }
    await load()

    return () => { alive = false }
  },
}
