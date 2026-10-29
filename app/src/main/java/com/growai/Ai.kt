package com.growai

import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject

/** 눈 + 두뇌: OpenAI 호환 LLM (FreeLLMAPI 등). 설정을 매번 읽어서 키를 바꾸면 바로 반영 */
class Llm(private val cfg: Config) {
    private val vision get() = cfg["vision_model"]
    private val text get() = cfg["text_model"].ifBlank { vision }

    private fun chat(model: String, content: Any, max: Int = 400): String {
        val body = JSONObject().put("model", model).put("max_tokens", max)
            .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", content)))
        return Http.post(cfg["llm_base_url"].trimEnd('/') + "/chat/completions", cfg["llm_api_key"], body)
            .getJSONArray("choices").getJSONObject(0).getJSONObject("message").getString("content").trim()
    }

    private fun withImage(prompt: String, jpg: ByteArray) = JSONArray()
        .put(JSONObject().put("type", "text").put("text", prompt))
        .put(JSONObject().put("type", "image_url").put("image_url",
            JSONObject().put("url", "data:image/jpeg;base64," + Base64.encodeToString(jpg, Base64.NO_WRAP))))

    fun identify(jpg: ByteArray, pkg: String) =
        chat(vision, withImage("앱 패키지: '$pkg'. 이 게임 이름만 한 줄로 답해.", jpg), 30).lines().first().trim()

    fun describe(jpg: ByteArray, game: String) = chat(vision, withImage(
        "게임: $game\n화면을 보고 지금 상황을 5줄 이내로 설명해. 캐릭터 위치, 적/장애물, 메뉴/대화창 여부, " +
        "누를 수 있어 보이는 버튼과 대략적 위치(화면 비율 x,y), 목표로 보이는 것.", jpg))

    fun ask(prompt: String, max: Int = 300) = chat(text, prompt, max)

    fun solve(game: String, situation: String, history: String, web: String) = ask(
        "게임 '$game'에서 막혔다.\n상황:\n$situation\n최근 행동:\n$history\n웹 검색 결과:\n$web\n\n" +
        "이 상황을 넘기 위한 구체적 방법을 1~3줄로. 가능한 행동 이름으로 표현해.", 200)
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
