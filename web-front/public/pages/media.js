/**
 * 素材：挑一张照片 + 一段视频，一次传完就是一组映射。**Android `MediaScreen` 的对译。**
 *
 * ## 为什么这一页存在（管理台已经能批量导入了）
 *
 * Android 那边的原话：**婚礼当天刚拍的素材在手机里，而管理台跑在 NAS 上看不到它们。**
 * 管理台的批量导入处理的是**已经在 NAS 上**的文件。这一页处理的是"还在手机里"的。
 *
 * ## 五步，每一步都可能提前结束
 *
 * ```
 * 1. 算 sha256           ← 本地，不上传
 * 2. uploadCheck         ← 服务端已经有这个文件？有就跳过上传（§29：上传前就告诉他重复了）
 * 3. upload              ← 带进度。50MB 的视频没有进度条等于卡死
 * 4. createPhoto         ← 跑特征提取，可能几十秒
 * 5. attachVideo         ← 配上视频
 * ```
 *
 * **第 4 步失败但第 5 步该继续的情况是真的**：照片入库成功而视频没配上时，那张照片
 * 扫到了不会播任何东西 —— 所以那一条必须显式说出来，而不是笼统报"失败"。
 *
 * ## 上传前不做"能不能传"的预判
 *
 * Android 那边判「media 通道是不是隧道」（隧道有 100MB 上限）。网页没有通道概念，
 * 而"当前请求是否经过隧道"在浏览器里没有可靠信号。猜错的两种后果都糟：该禁的没禁
 * （白等一分钟看到 413），不该禁的禁了（局域网下明明能传却没入口）。所以这里**不预判**，
 * 只把上限写在旁边，并让 413 的原文如实显示出来。
 */
import * as api from '../api.js'
import { PRINT_SIZES } from '../printsize.js'
import { bytes, button, h, section, toast } from '../ui.js'

/**
 * 本地算 sha256。服务端的判重与 `uploadCheck` 都按这个值。
 *
 * **`crypto.subtle` 只在安全上下文里存在**，走纯 http（且不是 localhost）时它是
 * `undefined`。而这一步只是"上传前先问问服务端有没有"的优化 —— 拿不到就返回 null，
 * 让调用方跳过判重直接传。不这么写的话，整个上传会挂在
 * `Cannot read properties of undefined (reading 'digest')` 上，而那句话跟"你在用 http"
 * 一点关系都看不出来。
 *
 * @returns 十六进制小写，或 null（这个环境算不了）
 */
async function sha256Hex(file) {
  if (!globalThis.crypto?.subtle) return null
  const buf = await file.arrayBuffer()
  const d = await crypto.subtle.digest('SHA-256', buf)
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

export default {
  title: '素材',

  async mount(el, ctx) {
    let alive = true
    let busy = false
    let photoFile = null
    let videoFile = null
    let sizeKey = 'unknown'

    const log = h('div', { class: 'steps' })
    const say = (text, kind = '') => {
      if (!alive) return
      log.appendChild(h('p', { class: `step ${kind}`, text }))
      log.scrollTop = log.scrollHeight
    }

    el.appendChild(h('p', { class: 'p', text: '挑一张照片和配它的那段视频，一次传完就是一组映射。' }))
    el.appendChild(h('p', { class: 'p dim', text: '管理台的批量导入处理的是已经在 NAS 上的文件；这一页处理的是还在手机里的。' }))

    // ── 选文件 ──────────────────────────────────────────────────────
    const photoIn = h('input', { type: 'file', accept: 'image/*', id: 'f-photo' })
    const videoIn = h('input', { type: 'file', accept: 'video/*', id: 'f-video' })
    const photoName = h('span', { class: 'fname', text: '未选择' })
    const videoName = h('span', { class: 'fname', text: '未选择' })
    photoIn.addEventListener('change', () => {
      photoFile = photoIn.files?.[0] ?? null
      photoName.textContent = photoFile ? `${photoFile.name}（${bytes(photoFile.size)}）` : '未选择'
    })
    videoIn.addEventListener('change', () => {
      videoFile = videoIn.files?.[0] ?? null
      videoName.textContent = videoFile ? `${videoFile.name}（${bytes(videoFile.size)}）` : '未选择'
    })

    el.appendChild(section('选素材',
      // 「选择文件」那块木牌是可见的触发点，真 input 被 CSS 收起来了（理由见 theme.css）。
      // `for=` 让点木牌等于点 input —— 不需要 JS，也不破坏键盘与读屏。
      h('label', { class: 'file', for: 'f-photo' },
        h('span', { text: '照片（打印出来的那张）' }), photoIn,
        h('span', { class: 'pick', text: '选择照片' }), photoName),
      h('label', { class: 'file', for: 'f-video' },
        h('span', { text: '视频' }), videoIn,
        h('span', { class: 'pick', text: '选择视频' }), videoName),
      h('p', { class: 'p dim', text: api.UPLOAD_LIMIT_NOTE })))

    // ── 标题与打印宽度 ──────────────────────────────────────────────
    const titleIn = h('input', { type: 'text', placeholder: '留空则用文件名', id: 'f-title' })
    const chips = h('div', { class: 'chips' })
    const hintEl = h('p', { class: 'p dim' })
    const paintChips = () => {
      chips.innerHTML = ''
      for (const s of PRINT_SIZES) {
        const b = h('button', {
          type: 'button', class: `chip${s.key === sizeKey ? ' on' : ''}`, text: s.label,
          onclick: () => { sizeKey = s.key; paintChips() },
        })
        chips.appendChild(b)
      }
      hintEl.textContent = PRINT_SIZES.find((s) => s.key === sizeKey)?.hint ?? ''
    }
    paintChips()

    el.appendChild(section('照片印出来有多宽？',
      chips, hintEl,
      h('p', { class: 'p dim', text: '只是记下来，识别和贴合都不看它 —— 网页版按照片四个角贴，不需要真实尺寸。留「不知道」完全没问题。' }),
      h('label', { class: 'field', for: 'f-title' }, h('span', { text: '标题（可留空）' }), titleIn)))

    // ── 执行 ────────────────────────────────────────────────────────
    const progress = h('div', { class: 'bar2' }, h('i'))
    const progressText = h('p', { class: 'p mono' })
    const setProgress = (loaded, total, label) => {
      const pct = total ? Math.min(1, loaded / total) : 0
      progress.firstElementChild.style.transform = `scaleX(${pct})`
      progressText.textContent = `${label}  ${bytes(loaded)} / ${bytes(total)}`
    }

    const go = h('div', { class: 'actions' })
    const runBtn = button('传上去并建立映射', async () => {
      if (busy) return
      if (!photoFile) return toast('先挑一张照片')
      busy = true
      runBtn.disabled = true
      log.innerHTML = ''
      const t0 = Date.now()
      // 处理期间每秒报一次「已经 N 秒」。Android 那边有同一条 —— 特征提取要几十秒，
      // 而一个不动的"处理中"与卡死无从区分。
      const tick = setInterval(() => {
        if (!alive) return
        const s = Math.round((Date.now() - t0) / 1000)
        progressText.textContent = `处理中… 已经 ${s} 秒（照片大的话要久一些，别离开这一页）`
      }, 1000)

      try {
        const upOne = async (file, what) => {
          const sha = await sha256Hex(file)
          if (!sha) {
            // 说清楚跳过的是**上传前**的判重：服务端入库时仍会按 sha256 去重，
            // 不会真的落出两份。
            say('这个页面不是安全上下文（http），算不了 sha256，判重只能按文件名', 'warn')
          }
          let known = null
          say(`检查${what}是不是已经传过…`)
          try {
            known = await api.uploadCheck(file.name, sha, file.size)
          } catch (e) {
            // check 失败不该阻止上传 —— 但**必须说出来**。这个 catch 曾经是空的，
            // 于是接口签名对不上（GET 打 POST 路由）这件事被它整个吞掉，
            // 判重从来没工作过而界面上一切正常。
            say(`判重没做成（${e.message}），直接传`, 'warn')
          }

          // 内容已经在库里 —— 名字可能完全不同（相册第二次导出同一张照片就是这样）。
          // 这一条比按名字有用得多，所以先看。`missing` 的资产不能复用：库里有记录但
          // NAS 上文件没了，拿它去入库会在特征提取那一步失败。
          const hit = (known?.matches ?? []).find((m) => m.path && !m.missing)
          if (hit) {
            say(`${what}服务端已经有了（${hit.path.split('/').pop()}），跳过上传`, 'ok')
            return { nasPath: hit.path, sha256: sha }
          }
          // 同名同内容：文件就在落地目录里，直接用。
          if (known?.nameTaken && known.sameContent && known.existingPath) {
            say(`${what}已经在落地目录里了，跳过上传`, 'ok')
            return { nasPath: known.existingPath, sha256: sha }
          }
          // 同名不同内容：换服务端给的建议名。不换的话服务端会先把整个文件收下来、
          // 落临时文件比完哈希才 409 —— 几十 MB 白传。
          let name = file.name
          if (known?.nameTaken && known.suggestedName) {
            name = known.suggestedName
            say(`落地目录里已有同名但不同内容的文件，${what}改名传成 ${name}`, 'warn')
          }

          say(`上传${what}…`)
          const r = await api.upload(file, {
            name,
            onProgress: ({ loaded, total }) => setProgress(loaded, total, `上传${what}`),
          })
          return { nasPath: r?.path ?? r?.nasPath, sha256: sha }
        }

        const photoUp = await upOne(photoFile, '照片')
        const videoUp = videoFile ? await upOne(videoFile, '视频') : null

        say('入库并建立映射…（要跑特征提取，可能几十秒）')
        const widthMm = PRINT_SIZES.find((s) => s.key === sizeKey)?.widthMm ?? 0
        const payload = {
          refPath: photoUp.nasPath,
          title: titleIn.value.trim() || undefined,
          printWidthMm: widthMm > 0 ? widthMm : undefined,
          videoPath: videoUp?.nasPath,
        }

        let created
        try {
          created = await api.createPhoto(payload)
        } catch (e) {
          // 409 already_ingested：这张照片已经入过库了。**这不是失败** ——
          // 服务端会带上 photoId，用户接下来该做的是给它配视频，而不是重传。
          if (e.code === 'already_ingested' && e.body?.photoId) {
            say(`这张照片已经入过库了（${e.body.photoId.slice(0, 8)}）。`, 'warn')
            if (videoUp) {
              say('给已有的那条配上视频…')
              await api.attachVideo(e.body.photoId, { videoPath: videoUp.nasPath })
              say('视频已配上。', 'ok')
              ctx.shell.libraryChanged()
            }
            return
          }
          throw e
        }

        const pid = created?.photoId ?? created?.id
        if (videoUp && pid && !created?.hasVideo) {
          // createPhoto 没能一次带上视频时补一刀。**这一步不能省**：照片入库成功而
          // 视频没配上时，扫到它不会播任何东西，而界面上看起来"成功了"。
          say('配视频…')
          await api.attachVideo(pid, { videoPath: videoUp.nasPath })
        }

        const q = created?.qualityScore
        if (videoUp) {
          say(`成了：照片已入库${q !== undefined ? `（质量分 ${q}）` : ''}，视频已配上。`, 'ok')
        } else {
          say(`照片已入库${q !== undefined ? `（质量分 ${q}）` : ''}，但**还没配视频** —— ` +
            '扫到它不会播任何东西。回来这一页挑一段视频再传一次就能补上。', 'warn')
        }
        ctx.shell.libraryChanged()
      } catch (e) {
        // 失败留在页面上（不用 toast）。413 的原文要原样显示 —— 那是唯一能告诉用户
        // "换个网络"的线索。
        say(`没成：${e.message}${e.status ? `（HTTP ${e.status}）` : ''}`, 'bad')
      } finally {
        clearInterval(tick)
        if (alive) {
          busy = false
          runBtn.disabled = false
          progressText.textContent = ''
          progress.firstElementChild.style.transform = 'scaleX(0)'
        }
      }
    })
    go.appendChild(runBtn)
    el.appendChild(section('开始', go, progress, progressText, log))

    return () => { alive = false }
  },
}
