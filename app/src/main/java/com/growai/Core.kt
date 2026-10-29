package com.growai

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/** 화면과 두뇌 사이 연락통: 로그·상태를 메인 화면으로 보냄 */
object Bus {
    private val main = Handler(Looper.getMainLooper())
    val history = StringBuilder()
    var onLog: ((String) -> Unit)? = null
    var onStatus: ((String) -> Unit)? = null
    fun log(s: String) = main.post {
        history.append(s).append('\n')
        if (history.length > 20000) history.delete(0, 6000)
        onLog?.invoke(s)
    }
    fun status(s: String) = main.post { onStatus?.invoke(s) }
}

/** 설정(API 키 등): 앱 전용 저장소에 보관, 백업 꺼둠 */
class Config(ctx: Context) {
    private val p = ctx.getSharedPreferences("growai", Context.MODE_PRIVATE)
    operator fun get(k: String): String = p.getString(k, null)?.takeIf { it.isNotBlank() } ?: DEFAULTS[k] ?: ""
    operator fun set(k: String, v: String) { p.edit().putString(k, v).apply() }

    companion object {
        /** (키, 표시 이름, 비밀값 여부) */
        val FIELDS = listOf(
            Triple("typesafe_api_key", "TypeSafe (Jev) API 키", true),
            Triple("jev_model", "Jev 모델", false),
            Triple("typesafe_base_url", "TypeSafe 주소", false),
            Triple("llm_base_url", "LLM 주소 (FreeLLMAPI 등)", false),
            Triple("llm_api_key", "LLM API 키", true),
            Triple("vision_model", "비전 모델 (이미지 가능)", false),
            Triple("text_model", "텍스트 모델 (비우면 비전 모델)", false),
        )
        val DEFAULTS = mapOf(
            "jev_model" to "jev-latest",
            "typesafe_base_url" to "https://api.typesafe.ai",
            "typesafe_path" to "/v1/systemone",
            "stuck_after" to "5",
        )
    }
}

object Http {
    fun post(url: String, key: String, body: JSONObject, timeoutMs: Int = 60000): JSONObject {
        val c = URL(url).openConnection() as HttpURLConnection
        c.requestMethod = "POST"; c.doOutput = true
        c.connectTimeout = 15000; c.readTimeout = timeoutMs
        c.setRequestProperty("Content-Type", "application/json")
        if (key.isNotEmpty()) c.setRequestProperty("Authorization", "Bearer $key")
        c.outputStream.use { it.write(body.toString().toByteArray()) }
        val code = c.responseCode
        val txt = (if (code < 400) c.inputStream else c.errorStream)?.bufferedReader()?.use { it.readText() } ?: ""
        if (code >= 400) throw RuntimeException("HTTP $code ($url): ${txt.take(300)}")
        return JSONObject(txt)
    }

    fun get(url: String): String {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = 15000; c.readTimeout = 20000
        c.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android 14) GrowAI")
        return c.inputStream.bufferedReader().use { it.readText() }
    }

    /** 무료 웹검색(DuckDuckGo HTML) → 결과 요약 몇 줄 */
    fun search(q: String, n: Int = 4): String = try {
        val html = get("https://html.duckduckgo.com/html/?q=" + URLEncoder.encode(q, "UTF-8"))
        Regex("class=\"result__snippet\"[^>]*>(.*?)</a>", RegexOption.DOT_MATCHES_ALL).findAll(html).take(n)
            .joinToString("\n") { "- " + it.groupValues[1].replace(Regex("<[^>]+>"), "").replace("&#x27;", "'").replace("&quot;", "\"").replace("&amp;", "&").trim() }
            .ifBlank { "(검색 결과 없음)" }
    } catch (e: Exception) { "(검색 실패: ${e.message})" }
}
