package com.growai

import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

/** 무료 AI 하나 = OpenAI 호환 주소 + 키 + 비전 모델 + 텍스트 모델 */
private class Provider(val name: String, val base: String, val key: String, val vision: String, val text: String)

/** AI 라우터: 일마다 가장 빠른 무료 AI부터 쓰고, 한도(429)·오류가 나면 잠깐 쉬게 하고 다음 AI로 자동 전환.
 *  - 눈(화면 설명)·잡무(JSON, 교훈, 회고): Groq → 제미나이 → 직접 입력 LLM
 *  - 딥서치(모르는 것 조사): 제미나이 + 구글 검색 → 실패 시 무료 웹검색 + Groq 요약
 *  설정을 매번 읽어서 키를 바꾸면 바로 반영 */
class Llm(private val cfg: Config) {
    private val restUntil = ConcurrentHashMap<String, Long>()

    private fun providers(): List<Provider> = listOfNotNull(
        cfg["groq_api_key"].takeIf { it.isNotEmpty() }?.let {
            Provider("Groq", "https://api.groq.com/openai/v1", it, cfg["groq_vision_model"], cfg["groq_text_model"]) },
        cfg["gemini_api_key"].takeIf { it.isNotEmpty() }?.let {
            Provider("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", it, cfg["gemini_fast_model"], cfg["gemini_fast_model"]) },
        cfg["llm_base_url"].takeIf { it.isNotEmpty() }?.let {
            Provider("직접입력", it.trimEnd('/'), cfg["llm_api_key"], cfg["vision_model"], cfg["text_model"].ifBlank { cfg["vision_model"] }) },
    )

    private fun chat(vision: Boolean, content: Any, max: Int = 400): String {
        val now = System.currentTimeMillis()
        val list = providers().filter { (if (vision) it.vision else it.text).isNotBlank() }
        if (list.isEmpty()) throw IllegalStateException("Groq·제미나이·LLM 중 키를 하나 이상 넣어주세요")
        val ready = list.filter { (restUntil[it.name] ?: 0) < now }.ifEmpty { list }
        var last: Exception? = null
        for (p in ready) {
            try {
                val body = JSONObject().put("model", if (vision) p.vision else p.text).put("max_tokens", max)
                    .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", content)))
                return Http.post("${p.base}/chat/completions", p.key, body)
                    .getJSONArray("choices").getJSONObject(0).getJSONObject("message").optString("content").trim()
            } catch (e: Exception) {
                last = e
                val wait = if (e.message?.contains("HTTP 429") == true) 60_000L else 15_000L
                restUntil[p.name] = System.currentTimeMillis() + wait
                Bus.log("  ↪ ${p.name} 쉬는 중 (${wait / 1000}초) → 다음 AI로")
            }
        }
        throw last ?: IllegalStateException("AI 응답 없음")
    }

    private fun withImage(prompt: String, jpg: ByteArray) = JSONArray()
        .put(JSONObject().put("type", "text").put("text", prompt))
        .put(JSONObject().put("type", "image_url").put("image_url",
            JSONObject().put("url", "data:image/jpeg;base64," + Base64.encodeToString(jpg, Base64.NO_WRAP))))

    fun identify(jpg: ByteArray, pkg: String) =
        chat(true, withImage("앱 패키지: '$pkg'. 이 게임 이름만 한 줄로 답해.", jpg), 30).lines().first().trim()

    fun describe(jpg: ByteArray, game: String) = chat(true, withImage(
        "게임: $game\n화면을 보고 지금 상황을 5줄 이내로 설명해. 캐릭터 위치, 적/장애물, 메뉴/대화창 여부, " +
        "누를 수 있어 보이는 버튼과 대략적 위치(화면 비율 x,y), 목표로 보이는 것.", jpg))

    /** 빠른 잡무 (JSON 만들기, 회고 등) */
    fun ask(prompt: String, max: Int = 300) = chat(false, prompt, max)

    /** 딥서치: 제미나이가 구글 검색으로 직접 조사 → 출처 기반 해결법. 안 되면 무료 웹검색 + 빠른 AI */
    fun research(question: String): String {
        val key = cfg["gemini_api_key"]
        if (key.isNotEmpty() && (restUntil["GeminiSearch"] ?: 0) < System.currentTimeMillis()) {
            try {
                val body = JSONObject()
                    .put("contents", JSONArray().put(JSONObject().put("parts", JSONArray().put(JSONObject().put("text", question)))))
                    .put("tools", JSONArray().put(JSONObject().put("google_search", JSONObject())))
                val r = Http.post("https://generativelanguage.googleapis.com/v1beta/models/${cfg["gemini_research_model"]}:generateContent",
                    "", body, 90_000, mapOf("x-goog-api-key" to key))
                val parts = r.getJSONArray("candidates").getJSONObject(0).getJSONObject("content").getJSONArray("parts")
                val text = (0 until parts.length()).joinToString("") { parts.getJSONObject(it).optString("text") }.trim()
                if (text.isNotEmpty()) { Bus.log("  🔎 제미나이 딥서치 완료"); return text }
            } catch (e: Exception) {
                restUntil["GeminiSearch"] = System.currentTimeMillis() + 120_000
                Bus.log("  ↪ 제미나이 딥서치 실패 → 무료 웹검색으로 (${e.message?.take(80)})")
            }
        }
        return ask("$question\n\n웹 검색 결과:\n${Http.search(question.lines().first().take(120))}", 400)
    }

    fun solve(game: String, situation: String, history: String) = research(
        "모바일 게임 '$game'에서 막혔다. 공략·가이드를 조사해서 해결법을 알려줘.\n상황:\n$situation\n최근 행동:\n$history\n\n" +
        "답은 한국어 1~3줄, 화면 어디를 탭/스와이프하면 되는지처럼 구체적인 행동으로.")
}

/** 판단: TypeSafe Jev → 행동 선택(choice) / 진전 판정(boolean) */
class Jev(private val cfg: Config) {
    private fun ask(state: String, q: JSONObject): JSONObject =
        Http.post(cfg["typesafe_base_url"].trimEnd('/') + cfg["typesafe_path"], cfg["typesafe_api_key"],
            JSONObject().put("model", cfg["jev_model"]).put("state", state.take(8000)).put("questions", q), 30000)
            .optJSONObject("answers") ?: JSONObject()

    private fun num(o: JSONObject, vararg keys: String): Double {
        for (k in keys) { val v = o.optDouble(k, Double.NaN); if (!v.isNaN()) return v }
        return 0.5
    }

    fun choose(state: String, options: Map<String, String>): Pair<String, Double> {
        val opts = JSONObject(); options.forEach { (k, v) -> opts.put(k, v) }
        val a = ask(state, JSONObject().put("action", JSONObject().put("type", "choice")
            .put("instructions", "게임을 진행하고 목표에 가까워지기 위해 지금 할 가장 좋은 행동은?")
            .put("options", opts))).optJSONObject("action") ?: JSONObject()
        return a.optString("choice").ifBlank { options.keys.first() } to num(a, "confidence", "probability")
    }

    fun progress(before: String, action: String, after: String): Double {
        val a = ask("행동 전:\n$before\n\n한 행동: $action\n\n행동 후:\n$after", JSONObject().put("progress",
            JSONObject().put("type", "boolean").put("instructions", "이 행동으로 게임이 앞으로 진행됐거나 상황이 좋아졌는가?")))
            .optJSONObject("progress") ?: JSONObject()
        return num(a, "probability", "value")
    }
}
