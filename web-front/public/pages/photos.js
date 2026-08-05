/**
 * 照片库。**Android `PhotosScreen` 的对译。**
 *
 * 一屏网格，每张显示缩略图 + 标题 + 两个警示徽标（无视频 / 参考图变了）。
 * 那两个徽标不是装饰：它们各自对应一种「扫了不会有反应」的状态，而那是用户最可能
 * 来这一页查的事。
 */
import * as api from '../api.js'
import { empty, failed, h, loading, thumb } from '../ui.js'
import { Page } from '../navpolicy.js'

export default {
  title: '照片库',

  async mount(el, ctx) {
    let alive = true
    el.appendChild(loading('正在取照片…'))

    const render = (list) => {
      if (!alive) return
      el.innerHTML = ''
      if (!list.length) {
        el.appendChild(empty(
          '照片库是空的',
          '去「素材」页挑一张打印过的照片 + 一段视频，一次传完就是一组映射。',
        ))
        return
      }
      const grid = h('div', { class: 'grid' })
      for (const p of list) {
        const id = p.photoId ?? p.id
        const flags = []
        // 这两条与 Android 的同一批文案。它们回答「为什么扫了没反应」——
        // 而那是这一页存在的主要理由之一。
        if (p.hasVideo === false) flags.push(h('span', { class: 'badge', text: '无视频' }))
        if (p.refStale) flags.push(h('span', { class: 'badge warn', text: '参考图变了' }))
        grid.appendChild(h('button', {
          class: 'card', onclick: () => ctx.shell.push(Page.DETAIL, { id }),
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
