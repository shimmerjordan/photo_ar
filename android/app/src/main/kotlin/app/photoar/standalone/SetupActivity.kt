package app.photoar.standalone

import android.app.Activity
import android.content.Context
import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import app.photoar.arview.Endpoints
import app.photoar.arview.ui.ArScanActivity

/**
 * 独立装机壳的唯一界面：填三个值，进扫描。
 *
 * Phase 3 的 Flutter 外壳会取代它（那边有 NAS 浏览、关联、历史，端点也由
 * EndpointResolver 自动探）。这里只是为了 Phase 2 能单独装到真机上验 AR 体验 ——
 * `:arview` 是 library，自己装不了。
 */
class SetupActivity : Activity() {

    private companion object {
        const val PREFS = "photoar"
        const val KEY_API = "apiBase"
        const val KEY_MEDIA = "mediaBase"
        const val KEY_TOKEN = "token"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        val api = field(prefs.getString(KEY_API, "") ?: "", "http://10.0.0.9:8770")
        val media = field(prefs.getString(KEY_MEDIA, "") ?: "", "留空＝与 API 相同")
        val token = field(prefs.getString(KEY_TOKEN, "") ?: "", "").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }

        val scan = Button(this).apply {
            text = "开始扫描"
            setOnClickListener {
                val a = api.text.toString().trim()
                val t = token.text.toString().trim()
                if (a.isEmpty() || t.isEmpty()) {
                    Toast.makeText(this@SetupActivity, "地址和令牌都要填", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }
                // 媒体通道留空就与 API 同源：局域网直连时两者本来就是同一台机器。
                // 分开配是为了走隧道时把大流量留在局域网 / Tailscale 上（§4.1）。
                val m = media.text.toString().trim().ifEmpty { a }
                prefs.edit()
                    .putString(KEY_API, a)
                    .putString(KEY_MEDIA, m)
                    .putString(KEY_TOKEN, t)
                    .apply()
                ArScanActivity.start(this@SetupActivity, Endpoints(a, m, t))
            }
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val pad = dp(16)
            setPadding(pad, pad, pad, pad)
            addView(title("照片 AR · 独立装机壳"))
            addView(label("API 地址"))
            addView(api)
            addView(label("媒体地址"))
            addView(media)
            addView(label("访问令牌"))
            addView(token)
            addView(scan)
        }
        setContentView(root)
    }

    private fun title(text: String) = TextView(this).apply {
        this.text = text
        textSize = 20f
        setPadding(0, 0, 0, dp(16))
    }

    private fun label(text: String) = TextView(this).apply {
        this.text = text
        textSize = 13f
        setPadding(0, dp(12), 0, 0)
    }

    private fun field(value: String, hint: String) = EditText(this).apply {
        this.hint = hint
        setText(value)
        setSingleLine()
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
