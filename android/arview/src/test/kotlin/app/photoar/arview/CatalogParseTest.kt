package app.photoar.arview

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * 外壳侧解析的测试。样例 JSON 全部照 `server/app.py` 与 `server/fsbrowser.py`
 * **实际构造的形状**抄下来（不是照 spec 抄）—— Phase 2 的教训是这两者会分叉，
 * 而分叉的代价是真机上才发现。
 */
class CatalogParseTest {

    // ---- /v1/photos ----

    @Test
    fun `photos 解出全部字段`() {
        val list = CatalogParse.photos(
            """
            {"photos":[
              {"photoId":"p1","title":"外婆生日","printWidthM":0.152,"qualityScore":88,
               "refAspect":1.5,"refThumbUrl":"/v1/photo/p1/thumb","hasVideo":true,
               "refStale":false,"createdAt":1730000000000}
            ],"total":1}
            """.trimIndent(),
        )
        assertEquals(1, list.size)
        val p = list[0]
        assertEquals("p1", p.photoId)
        assertEquals("外婆生日", p.title)
        assertEquals(0.152f, p.printWidthM, 1e-6f)
        assertEquals(88, p.qualityScore)
        assertEquals(1.5f, p.refAspect!!, 1e-6f)
        assertEquals("/v1/photo/p1/thumb", p.refThumbUrl)
        assertTrue(p.hasVideo)
        assertFalse(p.refStale)
        assertEquals(1730000000000L, p.createdAt)
    }

    @Test
    fun `photos 的 title 与 refAspect 是 JSON null 时给 null 而不是字符串 null`() {
        // Android 的 optString(name, null) 对 JSON null 会返回字符串 "null"，
        // Maven 版 org.json 不会 —— 两边行为不同，所以解析一律走 isNull()。
        val p = CatalogParse.photos(
            """{"photos":[{"photoId":"p1","title":null,"refAspect":null}]}""",
        ).single()
        assertNull(p.title)
        assertNull(p.refAspect)
    }

    @Test
    fun `photos 里 refAspect 为 0 或负数当没有`() {
        val zero = CatalogParse.photos("""{"photos":[{"photoId":"a","refAspect":0}]}""").single()
        val neg = CatalogParse.photos("""{"photos":[{"photoId":"b","refAspect":-1.5}]}""").single()
        assertNull(zero.refAspect)
        assertNull(neg.refAspect)
    }

    @Test
    fun `photos 缺 refThumbUrl 时按约定拼出来`() {
        val p = CatalogParse.photos("""{"photos":[{"photoId":"p9"}]}""").single()
        assertEquals("/v1/photo/p9/thumb", p.refThumbUrl)
    }

    @Test
    fun `photos 少了 photoId 直接报错`() {
        try {
            CatalogParse.photos("""{"photos":[{"title":"没有 id"}]}""")
            fail("应该抛 ApiParseException")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("photoId"))
        }
    }

    @Test
    fun `photos 数组里混进非对象元素时跳过而不是整体失败`() {
        val list = CatalogParse.photos("""{"photos":["坏元素",{"photoId":"p1"},42]}""")
        assertEquals(listOf("p1"), list.map { it.photoId })
    }

    @Test
    fun `photos 空数组是合法的空库`() {
        assertTrue(CatalogParse.photos("""{"photos":[],"total":0}""").isEmpty())
    }

    @Test
    fun `photos 没有 photos 数组时报错`() {
        try {
            CatalogParse.photos("""{"total":0}""")
            fail("应该抛 ApiParseException")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("photos"))
        }
    }

    @Test
    fun `响应不是 JSON 时报错带上片段`() {
        try {
            CatalogParse.photos("<html>502 Bad Gateway</html>")
            fail("应该抛 ApiParseException")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("502"))
        }
    }

    // ---- /v1/photo/{id} ----

    @Test
    fun `photoDetail 解出全部字段`() {
        val d = CatalogParse.photoDetail(
            """
            {"photoId":"p1","title":"生日","printWidthM":0.1524,"qualityScore":91,
             "selfScore":140,"refAspect":0.75,"refPath":"/share/photo/a.jpg",
             "refMissing":false,"refStale":true,"videoPath":"/share/video/a.mp4",
             "videoMissing":false,"imgdbBytes":4380,
             "createdAt":1730000000000,"updatedAt":1730000009999}
            """.trimIndent(),
        )
        assertEquals("p1", d.photoId)
        assertEquals(140, d.selfScore)
        assertEquals("/share/photo/a.jpg", d.refPath)
        assertEquals("/share/video/a.mp4", d.videoPath)
        assertEquals(false, d.videoMissing)
        assertTrue(d.hasVideo)
        assertTrue(d.refStale)
        assertEquals(4380L, d.imgdbBytes)
        assertEquals(1730000009999L, d.updatedAt)
    }

    @Test
    fun `photoDetail 区分「没关联视频」与「视频丢了」`() {
        // 服务端：没有 video 资产时 videoMissing 是 null，有资产才给 true/false。
        // 界面上「还没配视频」要提示去关联，「视频丢了」要提示去修路径 —— 两者
        // 都塌成 false 的话第二种会被当成正常。
        val none = CatalogParse.photoDetail("""{"photoId":"p1","videoPath":null,"videoMissing":null}""")
        assertNull(none.videoMissing)
        assertFalse(none.hasVideo)

        val lost = CatalogParse.photoDetail(
            """{"photoId":"p1","videoPath":"/share/v.mp4","videoMissing":true}""",
        )
        assertEquals(true, lost.videoMissing)
        assertTrue(lost.hasVideo)
    }

    @Test
    fun `photoDetail 少了 photoId 报错`() {
        try {
            CatalogParse.photoDetail("""{"title":"x"}""")
            fail("应该抛 ApiParseException")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("photoId"))
        }
    }

    // ---- /v1/fs/list ----

    @Test
    fun `fsList 根目录列表 path 为 null 且条目自带 path`() {
        val l = CatalogParse.fsList(
            """
            {"path":null,"parent":null,"entries":[
              {"name":"照片","path":"/share/photo","isDir":true,"isRoot":true},
              {"name":"视频","path":"/share/video","isDir":true,"isRoot":true}
            ]}
            """.trimIndent(),
        )
        assertTrue(l.atRoots)
        assertNull(l.parent)
        assertEquals(listOf("/share/photo", "/share/video"), l.entries.map { it.path })
        assertTrue(l.entries.all { it.isRoot && it.isDir })
    }

    @Test
    fun `fsList 子条目没有 path 时由客户端拼出来`() {
        // 服务端对子条目只给 name（fsbrowser.list_dir）。这是最容易踩的不对称点：
        // 少拼这一步，点进二级目录就会带着相对名去请求，服务端直接 403。
        val l = CatalogParse.fsList(
            """
            {"path":"/share/photo/2024","parent":"/share/photo","entries":[
              {"name":"三月","isDir":true},
              {"name":"a.jpg","isDir":false,"kind":"image","bytes":2048000,"mtime":1730000000000},
              {"name":"b.mp4","isDir":false,"kind":"video","bytes":9000000,"mtime":1730000000001},
              {"name":"note.txt","isDir":false,"kind":null,"bytes":12,"mtime":1730000000002}
            ]}
            """.trimIndent(),
        )
        assertFalse(l.atRoots)
        assertEquals("/share/photo/2024", l.path)
        assertEquals("/share/photo", l.parent)
        assertEquals(
            listOf(
                "/share/photo/2024/三月",
                "/share/photo/2024/a.jpg",
                "/share/photo/2024/b.mp4",
                "/share/photo/2024/note.txt",
            ),
            l.entries.map { it.path },
        )
        val dir = l.entries[0]
        assertTrue(dir.isDir)
        assertNull(dir.kind)
        assertFalse(dir.isRoot)

        val img = l.entries[1]
        assertTrue(img.isImage)
        assertFalse(img.isVideo)
        assertEquals(2048000L, img.bytes)
        assertEquals(1730000000000L, img.mtime)

        assertTrue(l.entries[2].isVideo)

        val other = l.entries[3]
        assertNull(other.kind)
        assertFalse(other.isImage)
        assertFalse(other.isVideo)
    }

    @Test
    fun `fsList 目录条目即使带了 kind 也当没有`() {
        // 服务端不会给目录带 kind，但名字叫 "假的.jpg" 的目录如果被当成图片，
        // 界面会把它列进"可选参考图"，点下去入库必失败。
        val e = CatalogParse.fsList(
            """{"path":"/share","entries":[{"name":"像照片的目录.jpg","isDir":true,"kind":"image"}]}""",
        ).entries.single()
        assertTrue(e.isDir)
        assertNull(e.kind)
        assertFalse(e.isImage)
    }

    @Test
    fun `fsList 顶层没有 parent 时是 null`() {
        val l = CatalogParse.fsList("""{"path":"/share/photo","parent":null,"entries":[]}""")
        assertNull(l.parent)
        assertFalse(l.atRoots)
    }

    @Test
    fun `fsList 缺 entries 数组时报错`() {
        try {
            CatalogParse.fsList("""{"path":"/share"}""")
            fail("应该抛 ApiParseException")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("entries"))
        }
    }

    // ---- joinPath ----

    @Test
    fun `joinPath 普通拼接`() {
        assertEquals("/share/a/b.jpg", CatalogParse.joinPath("/share/a", "b.jpg"))
    }

    @Test
    fun `joinPath 目录已带斜杠时不重复`() {
        assertEquals("/share/b.jpg", CatalogParse.joinPath("/share/", "b.jpg"))
        assertEquals("/b.jpg", CatalogParse.joinPath("/", "b.jpg"))
    }

    @Test
    fun `joinPath 目录为空时只剩名字`() {
        assertEquals("b.jpg", CatalogParse.joinPath(null, "b.jpg"))
        assertEquals("b.jpg", CatalogParse.joinPath("", "b.jpg"))
    }

    @Test
    fun `joinPath 不化简 dotdot`() {
        // 客户端化简会把一个本该被服务端 safepath 拒掉的路径洗白。原样发过去，
        // 让服务端做唯一的判定方。
        assertEquals("/share/a/../../etc/passwd", CatalogParse.joinPath("/share/a", "../../etc/passwd"))
    }

    // ---- /v1/history ----

    @Test
    fun `history 命中与未命中都能解`() {
        val h = CatalogParse.history(
            """
            {"entries":[
              {"ts":1730000000000,"photoId":"p1","title":"生日",
               "refThumbUrl":"/v1/photo/p1/thumb","inliers":57,"latencyMs":180,"via":"lan"},
              {"ts":1730000001000,"photoId":null,"title":null,
               "refThumbUrl":null,"inliers":8,"latencyMs":210,"via":"tunnel"}
            ]}
            """.trimIndent(),
        )
        assertEquals(2, h.size)
        assertTrue(h[0].matched)
        assertEquals("p1", h[0].photoId)
        assertEquals(57, h[0].inliers)
        assertEquals("lan", h[0].via)

        assertFalse(h[1].matched)
        assertNull(h[1].photoId)
        assertNull(h[1].refThumbUrl)
        assertEquals("tunnel", h[1].via)
    }

    @Test
    fun `history 空列表合法`() {
        assertTrue(CatalogParse.history("""{"entries":[]}""").isEmpty())
    }

    // ---- POST /v1/photo ----

    @Test
    fun `createResult 解出服务端 201 的全部字段`() {
        val r = CatalogParse.createResult(
            """
            {"photoId":"p7","qualityScore":88,"selfScore":142,"imgdbBytes":4380,
             "printWidthM":0.152,"transcoded":true,"elapsedMs":41230,"libraryPhotos":137}
            """.trimIndent(),
        )
        assertEquals("p7", r.photoId)
        assertEquals(88, r.qualityScore)
        assertEquals(142, r.selfScore)
        assertEquals(4380L, r.imgdbBytes)
        assertEquals(0.152f, r.printWidthM, 1e-6f)
        assertTrue(r.transcoded)
        assertEquals(41230L, r.elapsedMs)
        assertEquals(137, r.libraryPhotos)
    }

    @Test
    fun `createBody 只放该放的字段`() {
        val body = JSONObject(CatalogParse.createBody("/share/a.jpg", "/share/v.mp4", 152.0, "外婆生日"))
        assertEquals("/share/a.jpg", body.getString("refPath"))
        assertEquals("/share/v.mp4", body.getString("videoPath"))
        assertEquals(152.0, body.getDouble("printWidthMm"), 1e-9)
        assertEquals("外婆生日", body.getString("title"))
    }

    @Test
    fun `createBody 省略空的 videoPath 与 title`() {
        // 服务端把 videoPath 当可选；给一个空串会被当成路径去 resolve 然后 403。
        val a = JSONObject(CatalogParse.createBody("/share/a.jpg", null, 152.0, null))
        assertFalse(a.has("videoPath"))
        assertFalse(a.has("title"))

        val b = JSONObject(CatalogParse.createBody("/share/a.jpg", "   ", 152.0, ""))
        assertFalse(b.has("videoPath"))
        assertFalse(b.has("title"))
    }

    @Test
    fun `createResult 少了 photoId 报错`() {
        try {
            CatalogParse.createResult("""{"qualityScore":88}""")
            fail("应该抛 ApiParseException")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("photoId"))
        }
    }

    // ---- POST /v1/photo/{id}/video ----

    @Test
    fun `attachResult 解出资产 id`() {
        val r = CatalogParse.attachResult(
            """{"photoId":"p1","videoAssetId":"v9","playableAssetId":"v10","transcoded":true}""",
        )
        assertEquals("p1", r.photoId)
        assertEquals("v9", r.videoAssetId)
        assertEquals("v10", r.playableAssetId)
        assertTrue(r.transcoded)
    }

    @Test
    fun `attachResult 免转码时 playableAssetId 可以是 null`() {
        val r = CatalogParse.attachResult(
            """{"photoId":"p1","videoAssetId":"v9","playableAssetId":null,"transcoded":false}""",
        )
        assertNull(r.playableAssetId)
        assertFalse(r.transcoded)
    }

    @Test
    fun `attachBody 只有 videoPath`() {
        val body = JSONObject(CatalogParse.attachBody("/share/v.mp4"))
        assertEquals("/share/v.mp4", body.getString("videoPath"))
        assertEquals(1, body.length())
    }
}
