package com.growai

import android.graphics.Bitmap
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.sqrt
import kotlin.random.Random

/** 생명체: 경험치·레벨, 호기심, 새 기술(콤보), 안 먹히는 행동 잠재우기 */
class Life(private val mem: Memory, private val game: String) {
    var xp = mem.totalXp(game)
        private set
    val level get() = sqrt(xp / 5).toInt() + 1                       // 클수록 레벨업이 점점 어려움
    @Volatile var boost = false  // 딥서치 기다리는 동안엔 호기심을 올려서 스스로도 길을 찾음
    val curiosity get() = max(if (boost) 0.35 else 0.08, 0.45 / level.toDouble().pow(0.5))  // 어릴수록 호기심 많고, 0은 안 됨

    fun gain(score: Double) {
        val before = level; xp += score
        if (level > before) Bus.log("🌱 레벨 업! Lv.$level (호기심 ${(curiosity * 100).toInt()}%)")
    }

    /** 15번 넘게 해도 안 먹힌 행동은 잠재우고, 10% 확률로 다시 깨워 재도전 */
    fun awake(acts: Map<String, Act>, rates: Map<String, Double>, counts: Map<String, Int>): Map<String, Act> =
        acts.filter { (k, _) -> !((counts[k] ?: 0) >= 15 && (rates[k] ?: 1.0) < 0.15) || Random.nextDouble() < 0.1 }
            .ifEmpty { acts }

    /** 호기심 발동 → 가장 적게 해본 행동을 직접 실험 */
    fun explore(acts: Map<String, Act>, counts: Map<String, Int>): String? =
        if (Random.nextDouble() < curiosity) acts.keys.minByOrNull { (counts[it] ?: 0) + Random.nextDouble() } else null

    /** 연달아 성공한 행동 2개를 묶어 새 기술로 */
    fun evolve(acts: Map<String, Act>): Map<String, Act> {
        val seq = mem.recentSeq(game, 300)
        val pairs = mutableMapOf<Pair<String, String>, MutableList<Double>>()
        seq.zipWithNext().forEach { (x, y) ->
            if (x.first != y.first && x.first in acts && y.first in acts)
                pairs.getOrPut(x.first to y.first) { mutableListOf() }.add(minOf(x.second, y.second))
        }
        val new = mutableMapOf<String, Act>()
        for ((p, sc) in pairs) {
            val name = "combo_${p.first}_${p.second}".take(40)
            if (sc.size >= 3 && sc.average() > 0.7 && name !in acts) {
                val a = acts.getValue(p.first); val b = acts.getValue(p.second)
                new[name] = Act("기술: ${a.desc} → ${b.desc}", a.does + b.does)
                Bus.log("✨ 새 기술 습득: $name")
            }
        }
        return new
    }
}

/** 화면 비교용: 64x36 흑백으로 줄여 평균 밝기 차이 계산 (AI 호출 없이 빠름) */
object Pix {
    fun gray(b: Bitmap): IntArray {
        val s = Bitmap.createScaledBitmap(b, 64, 36, true)
        val px = IntArray(64 * 36); s.getPixels(px, 0, 64, 0, 0, 64, 36)
        return IntArray(px.size) { ((px[it] shr 16 and 255) + (px[it] shr 8 and 255) + (px[it] and 255)) / 3 }
    }
    fun diff(a: IntArray, b: IntArray) = a.indices.sumOf { abs(a[it] - b[it]) }.toDouble() / a.size
}

/** 조작법 자동 발견: 웹 검색 + 직접 눌러보고 화면이 바뀌는 입력만 채택 */
class Discover(private val svc: BrainService, private val brain: Brain, private val llm: Llm) {
    private val probes = listOf(0.2, 0.5, 0.8).flatMap { y -> listOf(0.15, 0.5, 0.85).map { x -> "tap:$x:$y" } } + listOf(
        "swipe:0.5:0.7:0.5:0.3", "swipe:0.5:0.3:0.5:0.7", "swipe:0.8:0.5:0.2:0.5", "swipe:0.2:0.5:0.8:0.5",
        "hold:0.15:0.8:0.5", "hold:0.85:0.8:0.5")

    private fun gray(b: Bitmap) = Pix.gray(b)
    private fun diff(a: IntArray, b: IntArray) = Pix.diff(a, b)

    private fun probe(): Map<String, Act> {
        val base = (1..2).maxOf { val a = gray(svc.bitmap()); Thread.sleep(300); diff(a, gray(svc.bitmap())) }
        val th = base * 2 + 1.5
        val found = mutableMapOf<String, Act>()
        for (p in probes) {
            if (!BrainService.running) break
            val before = gray(svc.bitmap()); brain.act(p); Thread.sleep(350)
            val d = diff(before, gray(svc.bitmap()))
            if (d > th) {
                found[p.replace(Regex("\\W"), "_")] = Act("'$p' 입력 (화면 변화 ${d.toInt()})", listOf(p))
                Bus.log("  ✔ $p 반응 있음")
            }
        }
        return found
    }

    private fun fromWeb(game: String): Map<String, Act> = try {
        val web = llm.research("모바일 게임 '$game'의 조작법(버튼 위치, 터치/스와이프 방법)과 초반 진행 방법을 조사해서 정리해줘.")
        val t = llm.ask("게임 '$game'(모바일)의 조작법 검색 결과:\n$web\n\n이 게임을 플레이할 행동 목록을 JSON만 출력해. " +
            "형식: {\"행동이름\": {\"desc\": \"설명\", \"do\": [\"입력\"]}}. 입력 형식: tap:x:y / swipe:x1:y1:x2:y2 / " +
            "hold:x:y:초 / key:back / wait:초 (x,y는 0~1 화면 비율). 6~14개.", 800)
        Memory.fromJson(JSONObject(t.substring(t.indexOf('{'), t.lastIndexOf('}') + 1)))
    } catch (e: Exception) { emptyMap() }

    fun run(game: String): MutableMap<String, Act> {
        Bus.log("🔍 조작법 스스로 찾는 중...")
        val web = fromWeb(game); Bus.log("  웹/AI 추정 ${web.size}개")
        val acts = (probe() + web).toMutableMap()   // 이름 겹치면 웹(의미 있는 이름) 우선
        acts.putIfAbsent("wait", Act("잠시 기다리며 관찰", listOf("wait:1")))
        acts.putIfAbsent("back", Act("뒤로/닫기", listOf("key:back")))
        Bus.log("✅ 조작 ${acts.size}개 확보")
        return acts
    }
}
