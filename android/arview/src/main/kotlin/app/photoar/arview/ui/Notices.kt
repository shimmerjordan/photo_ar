package app.photoar.arview.ui

import app.photoar.arview.NoticeKind

/**
 * 提示文案。§13 的每一条都要有对应的人话，而不是把异常字符串直接甩到屏幕上。
 *
 * [detail] 只在能帮上忙的时候拼进去（比如「文件已不在」要给出 NAS 路径），
 * 网络栈的英文异常不往界面上放。
 */
object Notices {

    /** @return null 表示清掉当前提示。 */
    fun text(kind: NoticeKind, detail: String?): String? = when (kind) {
        NoticeKind.CLEARED -> null
        NoticeKind.AIM_AT_PHOTO -> "把照片放进取景框，保持 20-40cm"
        NoticeKind.NETWORK_SLOW -> "网络不稳，正在重新寻找可用连接…"
        NoticeKind.UNAUTHORIZED -> "访问令牌不对，去设置里改一下"
        NoticeKind.TARGET_LOAD_FAILED -> "这张照片的识别数据加载失败，稍后再试"
        NoticeKind.IMGDB_FALLBACK -> "正在用缩略图跟踪，贴合可能略有偏差"
        NoticeKind.TARGET_NOT_FOUND -> "认出来了，但没在画面里找到，再对准一下"
        NoticeKind.ASSET_MISSING ->
            if (detail.isNullOrBlank()) "关联的视频已不在 NAS 上"
            else "关联的视频已不在 NAS 上：$detail"
        NoticeKind.REF_STALE -> "参考图有更新，识别可能不准，建议重新入库"
        NoticeKind.NO_SEEK -> "这个视频不支持拖动进度"
        NoticeKind.VIDEO_UNPLAYABLE -> "视频播不了，照片还认得住"
        NoticeKind.TRACKING_LOST -> "照片离开画面，已暂停"
    }

    /** 提示要不要自动消失。瞬时状态会自己好，硬故障要留在屏幕上。 */
    fun transient(kind: NoticeKind): Boolean = when (kind) {
        NoticeKind.UNAUTHORIZED,
        NoticeKind.ASSET_MISSING,
        NoticeKind.VIDEO_UNPLAYABLE,
        NoticeKind.REF_STALE,
        -> false
        else -> true
    }
}
