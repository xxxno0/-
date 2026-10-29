package com.growai

import android.Manifest
import android.app.Activity
import android.content.ClipboardManager
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/** 메인 화면: API 키 붙여넣기 설정, 손(접근성) 켜기, 시작/정지, 성장 로그 */
class MainActivity : Activity() {
    private lateinit var cfg: Config
    private lateinit var logView: TextView
    private lateinit var statusView: TextView
    private lateinit var handsBtn: Button
    private val fields = mutableMapOf<String, EditText>()
    private val dp get() = resources.displayMetrics.density

    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        cfg = Config(this)
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(px(16), px(20), px(16), px(16)) }
        col.addView(TextView(this).apply { text = "GrowAI 🌱"; textSize = 26f; setTypeface(null, Typeface.BOLD) })
        statusView = TextView(this).apply { text = "잠자는 중 💤"; setTextColor(Color.rgb(22, 163, 74)); textSize = 15f }
        col.addView(statusView)

        col.addView(section("⚙ API 설정 (복사 → 📋 붙여넣기)"))
        for ((key, label, secret) in Config.FIELDS) {
            col.addView(TextView(this).apply { text = label; textSize = 13f; setPadding(0, px(8), 0, 0) })
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            val hidden = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            val shown = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            val e = EditText(this).apply {
                setSingleLine(); setText(cfg[key]); inputType = if (secret) hidden else shown
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }
            fields[key] = e; row.addView(e)
            row.addView(small("📋") {
                val clip = getSystemService(ClipboardManager::class.java).primaryClip
                val t = clip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString()?.trim()
                if (t.isNullOrEmpty()) toast("클립보드가 비어 있어요") else e.setText(t)
            })
            if (secret) row.addView(small("👁") { e.inputType = if (e.inputType == hidden) shown else hidden; e.setSelection(e.text.length) })
            col.addView(row)
        }
        col.addView(big("💾 설정 저장", Color.rgb(34, 197, 94)) {
            fields.forEach { (k, e) -> cfg[k] = e.text.toString().trim() }
            toast("저장됨 · 실행 중이어도 바로 반영돼요")
        })

        col.addView(section("🚀 시작"))
        handsBtn = big("1. 손 켜기 (접근성 → GrowAI 켜기)", Color.rgb(100, 116, 139)) {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            toast("설치된 앱 → GrowAI → 사용 켜기")
        }
        col.addView(handsBtn)
        col.addView(big("2. 🎮 깨우기 (그다음 게임 열기)", Color.rgb(59, 130, 246)) { wake() })
        col.addView(big("■ 재우기 (정지)", Color.rgb(71, 85, 105)) {
            startService(Intent(this, BrainService::class.java).setAction(BrainService.STOP))
        })

        col.addView(section("📜 성장 기록"))
        logView = TextView(this).apply { typeface = Typeface.MONOSPACE; textSize = 11f; setTextIsSelectable(true); text = Bus.history.toString() }
        col.addView(logView)
        setContentView(ScrollView(this).apply { addView(col) })

        if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        if (cfg["typesafe_api_key"].isEmpty()) toast("먼저 API 키를 붙여넣고 저장해주세요")
    }

    override fun onResume() {
        super.onResume()
        Bus.onLog = { logView.append(it + "\n") }
        Bus.onStatus = { statusView.text = it }
        val on = HandsService.me != null
        handsBtn.text = if (on) "1. 손 켜짐 ✔" else "1. 손 켜기 (접근성 → GrowAI 켜기)"
        handsBtn.setBackgroundColor(if (on) Color.rgb(22, 163, 74) else Color.rgb(100, 116, 139))
        if (BrainService.running) statusView.text = "살아있음 🌱"
    }

    override fun onPause() { super.onPause(); Bus.onLog = null; Bus.onStatus = null }

    private fun wake() {
        fields.forEach { (k, e) -> cfg[k] = e.text.toString().trim() }
        when {
            cfg["typesafe_api_key"].isEmpty() -> toast("TypeSafe (Jev) API 키를 넣어주세요")
            cfg["llm_base_url"].isEmpty() || cfg["vision_model"].isEmpty() -> toast("LLM 주소와 비전 모델을 넣어주세요")
            HandsService.me == null -> toast("먼저 1번 '손 켜기'를 해주세요")
            BrainService.running -> toast("이미 깨어 있어요")
            else -> startActivityForResult(
                getSystemService(MediaProjectionManager::class.java).createScreenCaptureIntent(), 7)
        }
    }

    override fun onActivityResult(req: Int, res: Int, data: Intent?) {
        super.onActivityResult(req, res, data)
        if (req != 7) return
        if (res != RESULT_OK || data == null) { toast("화면 보기를 허용해야 게임을 볼 수 있어요"); return }
        startForegroundService(Intent(this, BrainService::class.java).putExtra("code", res).putExtra("data", data))
        toast("깨어났어요! 이제 게임을 열어주세요")
        moveTaskToBack(true)
    }

    private fun px(v: Int) = (v * dp).toInt()
    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
    private fun section(t: String) = TextView(this).apply {
        text = t; textSize = 16f; setTypeface(null, Typeface.BOLD); setPadding(0, px(18), 0, px(4))
    }
    private fun small(t: String, on: (View) -> Unit) = Button(this).apply {
        text = t; minWidth = 0; minimumWidth = px(48); setOnClickListener(on)
    }
    private fun big(t: String, color: Int, on: (View) -> Unit) = Button(this).apply {
        text = t; setTextColor(Color.WHITE); setBackgroundColor(color); setAllCaps(false)
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = px(8) }
        setOnClickListener(on)
    }
}
