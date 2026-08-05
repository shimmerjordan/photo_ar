/**
 * 识别历史。**Android `HistoryScreen` 的对译。admin only。**
 *
 * ## 为什么它是「扫不出来时第一个该看的地方」
 *
 * 每一条记录带 `reason` / `inliers` / `runnerUp`。而 §34.3 记了这三列为什么必须有：
 * 941 条真机帧里 897 条内点 160~229 却判未命中，光凭内点数**分不出**挡住它们的是
 * `weak`（取景问题，下一帧就好）还是 `ambiguous`（库里有近重复，每一帧都这样）——
 * 而那两件事的修法毫不相干。
 *
 * 所以 `ambiguous` 在这里单独标红：其余未命中是"这一帧没拍好"，它是"不处理的话
 * 每一帧都这样"。
 */
import * as api from '../api.js'
import { empty, failed, h, loading, when } from '../ui.js'

/** 未命中原因 → 该做什么。**每一条都要能照着做** —— 这是 App 那边反复踩出来的教训。 */
const REASON = {
  ok: { label: '命中', hint: '' },
  weak: { label: 'weak', hint: '内点不够（这一帧没拍好，下一帧通常就好）' },
  ambiguous: { label: 'ambiguous', hint: '库里有近重复，两张互相挤掉了 —— 要删掉其中一张', bad: true },
  forbidden: { label: 'forbidden', hint: '认出来了但这个用户没被授权' },
  empty: { label: 'empty', hint: '候选集是空的（库里没有可比的照片）' },
  orphan: { label: 'orphan', hint: '库里有、catalog 里没有', bad: true },
}

export default {
  title: '识别历史',

  async mount(el, ctx) {
    let alive = true

    const load = async () => {
      el.innerHTML = ''
      el.appendChild(loading('正在取记录…'))
      let items
      try {
        items = await api.history(200)
      } catch (e) {
        if (!alive) return
        el.innerHTML = ''
        el.appendChild(failed(e.message, load))
        return
      }
      if (!alive) return
      el.innerHTML = ''

      if (!items.length) {
        el.appendChild(empty('还没有识别记录', '扫一次照片就会在这里留一条，命中和未命中都记。'))
        return
      }

      const list = h('div', { class: 'list' })
      for (const it of items) {
        const matched = Boolean(it.matched ?? (it.photoId && it.reason === 'ok'))
        const r = REASON[it.reason] ?? { label: it.reason ?? '—', hint: '' }
        const bits = []
        if (it.inliers !== undefined) bits.push(`inliers ${it.inliers}`)
        if (it.runnerUp) bits.push(`第二名 ${it.runnerUp}`)
        if (it.latencyMs !== undefined) bits.push(`${it.latencyMs}ms`)
        if (it.via) bits.push(it.via)

        list.appendChild(h('div', { class: `item${r.bad ? ' bad' : ''}` },
          h('div', { class: 'item-h' },
            h('span', { class: matched ? 'ok' : 'miss', text: matched ? '命中' : '未命中' }),
            h('span', { class: 'when', text: when(it.at ?? it.createdAt) })),
          h('div', { class: 'item-b', text: it.title || it.photoId?.slice(0, 8) || r.label }),
          h('div', { class: 'item-m mono', text: bits.join(' · ') }),
          r.hint ? h('div', { class: 'item-hint', text: r.hint }) : null))
      }
      el.appendChild(list)
      el.appendChild(h('p', { class: 'note', text: `最近 ${items.length} 条` }))
    }

    await load()
    return () => { alive = false }
  },
}
