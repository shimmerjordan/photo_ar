package app.photoar.arview.ui

import app.photoar.arview.NoticeKind
import app.photoar.arview.SaveOutcome

/**
 * 提示文案。§13 的每一条都要有对应的人话，而不是把异常字符串直接甩到屏幕上。
 *
 * [detail] 只在能帮上忙的时候拼进去（比如「文件已不在」要给出 NAS 路径），
 * 网络栈的英文异常不往界面上放。
 */
object Notices {

    /**
     * 「保存到相册」的结果文案。纯函数，好让部分成功那几条分支能被测到。
     *
     * 部分成功必须**说清楚存了什么、什么没成**，不能一句「保存失败」了事：照片已经
     * 在相册里了，用户按第二次只会得到同名的第二份，而真正没成的是视频。
     */
    fun saveResult(outcome: SaveOutcome): String {
        val saved = buildList {
            if (outcome.imageName != null) add("照片")
            if (outcome.videoName != null) add("视频")
        }
        val ok = if (saved.isEmpty()) null else "已保存${saved.joinToString("和")}到相册"
        if (outcome.problems.isEmpty()) {
            // 一样都没存、也没有报错 = 这张照片没有视频，而照片那一步本该成功。
            // 走到这里说明有分支忘了记 problems，如实说而不是显示一句空话。
            return ok ?: "没有可保存的内容"
        }
        val bad = outcome.problems.joinToString("；")
        return if (ok == null) "保存失败：$bad" else "$ok；但$bad"
    }

    /** @return null 表示清掉当前提示。 */
    fun text(kind: NoticeKind, detail: String?): String? = when (kind) {
        NoticeKind.CLEARED -> null
        NoticeKind.AIM_AT_PHOTO -> "把照片放进取景框，保持 20-40cm"
        NoticeKind.NETWORK_SLOW -> "网络不稳，正在重新寻找可用连接…"
        // 服务端换成用户体系之后，这条最常见的原因是**登录过期**（管理员会话 12 小时、
        // 访客 30 天），不是"令牌填错了"。文案跟着改，而且状态机会同时把用户送回登录
        // 界面（ScanEffects.requestLogin）—— 提示与去处必须一起给。
        NoticeKind.UNAUTHORIZED -> "登录已失效，请重新登录"
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
        // 措辞不能像报错：识别是对的、框也在，只是视频还在路上，而且我们**还在等**。
        // 「加载中」这三个字是这句话的重点 —— 它要回答用户此刻唯一的问题：
        // 「是不是我没对准？」不是。所以别让人再去挪手机。
        NoticeKind.VIDEO_SLOW -> "视频加载慢，请稍等（照片已认出，不用再挪动）"
        // 「离线」这个词是刻意露给用户的。后半句取决于装的是哪一份库：服务端预建那份
        // （原图建的）跟联网命中一模一样，端上现建那份（640px 缩略图）低一档，贴合略偏
        // 时人得知道原因。所以整句由 `ScanRuntime` 填进 detail —— 只有它知道装的是谁。
        // 兜底那句按端上现建写：猜错方向必须是「别承诺质量」。
        NoticeKind.LOCAL_HIT ->
            detail?.takeIf { it.isNotBlank() } ?: "离线识别（本地缓存），贴合可能略有偏差"
        // 离线识别没消失，只是降了一档 —— 所以文案不能像报错。原因（版本不匹配）对用户
        // 没有意义，能做的事在服务端，所以只说「已切到备用方式」并把细节留给日志。
        NoticeKind.TARGETS_DB_FALLBACK -> "离线识别库版本不匹配，已改用端上现建的那份"
        // 和 VIDEO_UNPLAYABLE 分开的理由在 NoticeKind 那边写了：这条用户能自己
        // 解决，所以文案要给出办法，而不是只报告坏消息。
        NoticeKind.VIDEO_NOT_CACHED -> "这条视频还没缓存，联网后可播"
        // detail 是 FeaturePathPolicy.message() 出来的整句中文（"取不到端上模型，已改回
        // 上传整帧识别。功能不受影响，只是慢一点。"），直接用。兜底那句不该出现，
        // 但它比一个空提示好。
        NoticeKind.FEATURES_FALLBACK ->
            detail?.takeIf { it.isNotBlank() } ?: "端上提特征没走通，已改回上传整帧识别"
    }

    /** 提示要不要自动消失。瞬时状态会自己好，硬故障要留在屏幕上。 */
    fun transient(kind: NoticeKind): Boolean = when (kind) {
        NoticeKind.UNAUTHORIZED,
        NoticeKind.ASSET_MISSING,
        NoticeKind.VIDEO_UNPLAYABLE,
        NoticeKind.REF_STALE,
        // 「没缓存」在这次扫描里不会自己变好，消失了只会让人对着空白照片等下去
        NoticeKind.VIDEO_NOT_CACHED,
        -> false
        // LOCAL_HIT 走这边：它只是说明这次是怎么认出来的，看一眼就够了
        else -> true
    }
}
