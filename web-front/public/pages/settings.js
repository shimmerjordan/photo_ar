/**
 * 设置：账号、识别参数、诊断、关于。**Android `SettingsScreen` 的对译（减掉通道那一半）。**
 *
 * ## Android 的「通道」整节在这里不存在
 *
 * 那边有多端点探活（LAN / Tailscale / Cloudflare）、每条通道声明「适合 api 还是 media」、
 * 以及「现在走的是哪条」。网页版没有对应物：页面就是服务器发出来的，请求全走同源相对
 * 路径 —— 没有可选的通道，也就没有可配的东西。
 *
 * 这不是"少做了"：那一整套的存在理由是 App 装在手机上、要自己找服务端。网页反过来，
 * 是服务端把自己发给了手机。
 *
 * ## 保留的那三样都有对应物
 *
 * - **账号**：谁登录着、什么角色、什么时候过期、登出。
 * - **识别参数**：服务端的热配置（`recog.min_inliers` 等）。**只读** —— 改它要去管理台，
 *   而那边有完整的校验。这里显示是为了让"为什么这张扫不出来"能对上一个具体的数。
 * - **诊断**：那块页面内日志的开关。Android 那边是"再点 N 下进入调试模式"，
 *   网页这边同样藏在连点里（底部信息三连击），这里给一个明面上的开关。
 */
import * as api from '../api.js'
import { thresholds } from '../recognize/consts.js'
import { isDiagEnabled, setDiagEnabled } from '../diag.js'
import { button, h, row, section, toast, when } from '../ui.js'

export default {
  title: '设置',

  async mount(el, ctx) {
    let alive = true
    const me = ctx.me()

    el.appendChild(section('账号',
      row('名字', me?.name ?? '—'),
      row('角色', me?.role === 'admin' ? '管理员' : '访客'),
      // grantAll 的人看得到全库。这一条对访客很重要 —— 它解释了"为什么我能看到这些"。
      me?.grantAll ? row('授权范围', '全部照片') : null,
      me?.expiresAt ? row('有效期至', when(me.expiresAt)) : null,
      h('div', { class: 'actions' },
        button('退出登录', async () => {
          try {
            await api.logout()
          } catch { /* 就算服务端那边失败，本地也该回到未登录 */ }
          // 整页重载而不是自己清状态：登出要作废的东西散在好几处（cookie、库包、
          // Worker、相机）。重载是唯一能保证不漏的做法，而它只发生一次。
          location.replace(location.pathname)
        }, { kind: 'danger' }))))

    // 有效期快到时提醒。Android 那边同样有这一条 —— 访客 30 天、管理员 12 小时，
    // 而"扫到一半掉线"是最难解释的失败。
    if (me?.expiresAt) {
      const leftMs = (me.expiresAt < 1e12 ? me.expiresAt * 1000 : me.expiresAt) - Date.now()
      if (leftMs > 0 && leftMs < 60 * 60 * 1000) {
        el.appendChild(h('p', { class: 'warnbox', text: '登录即将过期，建议现在就重新登录一次。' }))
      }
    }

    el.appendChild(section('识别参数（服务端热配置，只读）',
      row('内点门槛', String(thresholds.minInliers), { mono: true }),
      row('第一名 / 第二名比值', String(thresholds.ratio), { mono: true }),
      row('候选数 Top-K', String(thresholds.topK), { mono: true }),
      h('p', { class: 'p dim', text: '改它去管理台 → 识别设置。这里显示是为了让「为什么这张扫不出来」能对上一个具体的数。' })))

    const diagRow = h('div', { class: 'actions' })
    const paintDiag = () => {
      diagRow.innerHTML = ''
      const on = isDiagEnabled()
      diagRow.appendChild(button(on ? '关掉诊断日志' : '打开诊断日志', () => {
        setDiagEnabled(!on)
        paintDiag()
      }, { kind: 'ghost' }))
    }
    paintDiag()
    el.appendChild(section('诊断',
      h('p', { class: 'p', text: '页面顶部会出现一块滚动日志，带「复制」按钮。' }),
      h('p', { class: 'p dim', text: '扫不出来、或者认出来了但视频没播时，那块日志能分开五种互不相干的原因。也可以连点底部信息三次开关它。' }),
      diagRow))

    const about = section('关于')
    el.appendChild(about)
    about.body.appendChild(row('识别后端', '取中…'))
    // ping 是免鉴权的轻请求，用来显示后端与降级状态。`backendDegraded` 那一条很重要：
    // XFeat 模型缺失时服务会静默回退 ORB，而"换了特征却毫无变化"只有这里看得出来。
    try {
      const p = await api.ping()
      if (!alive) return
      about.body.lastChild.remove()
      about.body.appendChild(row('识别后端', p.backend ?? '—'))
      if (p.backendDegraded) {
        about.body.appendChild(h('p', { class: 'warnbox', text: '服务端报 backendDegraded：配置要的后端起不来，已回退。识别行为与预期不同。' }))
      }
      if (p.photos !== undefined) about.body.appendChild(row('库内照片', String(p.photos), { mono: true }))
    } catch {
      if (!alive) return
      about.body.lastChild.remove()
      about.body.appendChild(row('识别后端', '连不上服务端'))
    }
    about.body.appendChild(h('p', { class: 'p dim' },
      h('a', { href: 'https://github.com/shimmerjordan/photo_ar', target: '_blank', rel: 'noopener', text: '开源地址' })))

    return () => { alive = false }
  },
}
