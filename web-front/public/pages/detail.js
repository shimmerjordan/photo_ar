/**
 * 照片详情。**Android `PhotoDetailScreen` 的对译。**
 *
 * 三段：参考图预览、尺寸与质量、NAS 上的文件。加上两个动作（试播、删除）。
 *
 * ## 那三条警示文案是这一页的主要价值
 *
 * Android 那边一字一句写清了每种坏状态该去做什么，因为它们在界面上长得都一样
 * （「扫了没反应」），而修法毫不相干：
 *
 * - 参考图在 NAS 上找不到 → 去把文件放回原处
 * - 还没关联视频 → 识别出来也没东西可播
 * - 关联的视频文件不见了 → 同上，但要找的是另一个文件
 *
 */
import * as api from '../api.js'
import { Page } from '../navpolicy.js'
import { bytes, button, confirmDanger, duration, failed, framed, h, loading, row, section, toast, when } from '../ui.js'

export default {
  title: '照片详情',

  async mount(el, ctx) {
    let alive = true
    const id = ctx.params.id
    if (!id) {
      el.appendChild(h('p', { class: 'state', text: '缺少照片 id' }))
      return () => { alive = false }
    }

    const load = async () => {
      el.innerHTML = ''
      el.appendChild(loading('正在取详情…'))
      let d
      try {
        d = await api.photoDetail(id)
      } catch (e) {
        if (!alive) return
        el.innerHTML = ''
        el.appendChild(failed(e.message, load))
        return
      }
      if (!alive) return
      el.innerHTML = ''

      const warns = []
      if (d.refMissing) {
        warns.push('参考图在 NAS 上找不到了。这张照片扫不出来，去把文件放回原处。')
      }
      if (!d.hasVideo && !d.videoPath) {
        warns.push('还没关联视频。识别出来也没东西可播。')
      } else if (d.videoMissing) {
        warns.push('关联的视频文件不见了。')
      }
      for (const w of warns) el.appendChild(h('p', { class: 'warnbox', text: w }))

      el.appendChild(framed(h('img', {
        class: 'ref', alt: d.title ?? '参考图',
        src: `/v1/photo/${id}/thumb?rev=${ctx.shell.libraryRev}`,
      })))
      el.appendChild(h('h1', { class: 'ttl', text: d.title || '（未命名）' }))

      el.appendChild(section('尺寸与指标',
        // 0 表示"未知"，服务端与 Android 都以 0 为未知（见 §13）。显示成"未填"
        // 而不是 0mm —— 后者看起来像一个真的测量值。
        row('打印宽度', d.printWidthM > 0 ? `${Math.round(d.printWidthM * 1000)} mm` : '未填（扫的时候要轻轻晃一下手机）'),
        row('自匹配', String(d.selfScore ?? '—'), { mono: true }),
        row('入库时间', when(d.createdAt))))

      el.appendChild(section('NAS 上的文件',
        row('参考图', d.refPath ?? '—', { mono: true, bad: Boolean(d.refMissing) }),
        row('视频', d.videoPath ?? '未关联', { mono: true, bad: Boolean(d.videoMissing) }),
        d.durationMs ? row('时长', duration(d.durationMs)) : null))

      const actions = h('div', { class: 'actions' })
      if (d.hasVideo !== false || d.videoPath) {
        actions.appendChild(button('试播', () => ctx.shell.push(Page.PLAY, { id }), { iconName: 'play' }))
      }
      actions.appendChild(button('删除这张', async () => {
        // 服务端的删除是**墓碑**而不是真删（slot 下标就是 desc.bin 的偏移，摘一项会让
        // photo_id ↔ slot 整体平移 → 命中之后播别人的视频）。但对用户是不可撤销的，
        // 所以必须问一次。
        if (!confirmDanger(`删除「${d.title || id.slice(0, 8)}」？不可撤销。`)) return
        try {
          await api.deletePhoto(id)
          toast('已删除')
          ctx.shell.libraryChanged()
          ctx.shell.pop()
        } catch (e) {
          // 失败留在页面上，不用 toast —— 那会自己消失，而这是用户唯一的线索。
          el.appendChild(h('p', { class: 'warnbox', text: `删除没成：${e.message}` }))
        }
      }, { kind: 'danger', iconName: 'trash' }))
      el.appendChild(actions)
    }

    await load()
    return () => { alive = false }
  },
}
