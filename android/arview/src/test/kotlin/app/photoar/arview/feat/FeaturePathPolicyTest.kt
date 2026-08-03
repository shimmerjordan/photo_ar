package app.photoar.arview.feat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 开关与失败回退。
 *
 * 端上推理本身在这里跑不起来，所以「什么时候用哪条路、失败几次之后放弃、提示报几遍」
 * 必须全在这个纯类里 —— 这是这条路上唯一验得到的行为。三种失败（模型下不来 /
 * ONNX 加载失败 / 推理抛异常）都要静默回退到传 JPEG 并留一条 notice，不能让扫描坏掉。
 */
class FeaturePathPolicyTest {

    @Test
    fun `默认走现状那条路`() {
        // 端上推理没在真机上验过，默认打开等于把一条没验过的路径设成所有人的默认行为。
        assertEquals(RecognizePath.JPEG, FeaturePathPolicy().path)
        assertFalse(FeaturePathPolicy().preferFeatures)
    }

    @Test
    fun `开了就走特征那条路`() {
        assertEquals(RecognizePath.FEATURES, FeaturePathPolicy(true).path)
    }

    // ---- 三种失败都回退 ----

    @Test
    fun `模型下不来就回退，一次就够`() {
        val p = FeaturePathPolicy(true)
        assertTrue("第一次失败要报一条提示", p.onFailure(FeatureFailure.MODEL_UNAVAILABLE))
        assertEquals(RecognizePath.JPEG, p.path)
        assertTrue(p.fellBack)
    }

    @Test
    fun `ONNX 加载失败也是一次就够`() {
        val p = FeaturePathPolicy(true)
        assertTrue(p.onFailure(FeatureFailure.LOAD_FAILED))
        assertEquals(RecognizePath.JPEG, p.path)
    }

    @Test
    fun `服务端拒收也是一次就够`() {
        // 最常见的原因是服务端跑的是 ORB 后端。每 400ms 重试一次只会刷日志，
        // 而且每次都白付一次端上推理。
        val p = FeaturePathPolicy(true)
        assertTrue(p.onFailure(FeatureFailure.SERVER_REJECTED))
        assertEquals(RecognizePath.JPEG, p.path)
    }

    @Test
    fun `推理异常给三次机会`() {
        // 单帧的解码失败、瞬时内存压力是真实存在的，一次异常就永久关掉这条路会让
        // 用户看到「今天怎么突然变慢了」且下次重启才恢复。
        val p = FeaturePathPolicy(true)
        assertFalse(p.onFailure(FeatureFailure.INFER_FAILED))
        assertEquals(RecognizePath.FEATURES, p.path)
        assertFalse(p.onFailure(FeatureFailure.INFER_FAILED))
        assertEquals(RecognizePath.FEATURES, p.path)
        assertTrue("第 3 次才放弃", p.onFailure(FeatureFailure.INFER_FAILED))
        assertEquals(RecognizePath.JPEG, p.path)
        assertEquals(3, FeaturePathPolicy.INFER_STRIKES)
    }

    @Test
    fun `成功一次把推理的连续失败计数清零`() {
        val p = FeaturePathPolicy(true)
        p.onFailure(FeatureFailure.INFER_FAILED)
        p.onFailure(FeatureFailure.INFER_FAILED)
        p.onSuccess()
        assertFalse("清零之后又得重新数三次", p.onFailure(FeatureFailure.INFER_FAILED))
        assertEquals(RecognizePath.FEATURES, p.path)
    }

    @Test
    fun `致命失败不吃计数，混在一起也是一次就放弃`() {
        val p = FeaturePathPolicy(true)
        p.onFailure(FeatureFailure.INFER_FAILED)
        assertTrue(p.onFailure(FeatureFailure.LOAD_FAILED))
        assertEquals(RecognizePath.JPEG, p.path)
    }

    // ---- 提示只报一次 ----

    @Test
    fun `回退之后再失败不再报提示`() {
        // 扫描时每 400ms 一帧，逐次报就是一屏刷不完的重复提示 —— 而用户需要知道的
        // 只有「这次走的是慢的那条路」。
        val p = FeaturePathPolicy(true)
        assertTrue(p.onFailure(FeatureFailure.MODEL_UNAVAILABLE))
        repeat(20) { assertFalse(p.onFailure(FeatureFailure.MODEL_UNAVAILABLE)) }
        repeat(20) { assertFalse(p.onFailure(FeatureFailure.INFER_FAILED)) }
    }

    @Test
    fun `开关本来是关的时候失败也不报提示`() {
        // 这条路根本没在跑，报一条「已改回上传整帧」只会让人困惑。
        val p = FeaturePathPolicy(false)
        p.onFailure(FeatureFailure.MODEL_UNAVAILABLE)
        assertFalse("preferFeatures 是 false 就不算「回退」", p.fellBack)
        assertEquals(RecognizePath.JPEG, p.path)
    }

    // ---- 用户偏好不被悄悄改掉 ----

    @Test
    fun `回退不改用户的持久化偏好`() {
        // 顺手写成 false 的话，服务端修好之后用户得自己想起来再打开一次 ——
        // 而他根本不知道 App 悄悄替他关掉过。
        val p = FeaturePathPolicy(true)
        p.onFailure(FeatureFailure.SERVER_REJECTED)
        assertTrue("用户的偏好还是「开」", p.preferFeatures)
        assertTrue(p.disabledThisSession)
    }

    @Test
    fun `用户重新打开开关等于「再试一次」`() {
        val p = FeaturePathPolicy(true)
        p.onFailure(FeatureFailure.SERVER_REJECTED)
        assertEquals(RecognizePath.JPEG, p.path)

        p.setPreference(true)
        assertEquals("显式打开就该再试一次", RecognizePath.FEATURES, p.path)
        assertFalse(p.disabledThisSession)
        assertNull(p.lastFailure)
        // 而且提示的「只报一次」也跟着重置
        assertTrue(p.onFailure(FeatureFailure.SERVER_REJECTED))
    }

    @Test
    fun `关掉开关就是关掉`() {
        val p = FeaturePathPolicy(true)
        p.setPreference(false)
        assertEquals(RecognizePath.JPEG, p.path)
        assertFalse(p.preferFeatures)
        assertFalse("没开就谈不上回退", p.fellBack)
    }

    // ---- 文案 ----

    @Test
    fun `每种失败都有自己的文案且都不像报错`() {
        for (kind in FeatureFailure.entries) {
            val p = FeaturePathPolicy(true)
            p.onFailure(kind)
            if (kind == FeatureFailure.INFER_FAILED) {
                repeat(FeaturePathPolicy.INFER_STRIKES) { p.onFailure(kind) }
            }
            val text = p.message()
            assertTrue("$kind 没有文案", text.isNotBlank())
            assertTrue("$kind 的文案要说清功能没丢", text.contains("功能不受影响"))
            assertTrue("$kind 的文案要说清改走哪条路", text.contains("上传整帧"))
        }
    }

    @Test
    fun `最近一次失败原因记得住`() {
        val p = FeaturePathPolicy(true)
        assertNull(p.lastFailure)
        p.onFailure(FeatureFailure.LOAD_FAILED)
        assertEquals(FeatureFailure.LOAD_FAILED, p.lastFailure)
    }

    @Test
    fun `哪几种失败是一次定生死`() {
        assertTrue(FeatureFailure.MODEL_UNAVAILABLE.fatal)
        assertTrue(FeatureFailure.LOAD_FAILED.fatal)
        assertTrue(FeatureFailure.SERVER_REJECTED.fatal)
        assertFalse(FeatureFailure.INFER_FAILED.fatal)
    }
}
