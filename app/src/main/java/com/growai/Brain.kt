package com.growai

import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** 생명 루프: 보기 → 기억 → (호기심 or Jev 판단) → 터치 → 평가 → 저장 → 성장 → 막히면 스스로 해결.
 *  오류가 나도 죽지 않고 5초 쉬었다 다시 일어난다. */
class Brain(private val svc: BrainService, private val cfg: Config, private val mem: Memory) {
    private val llm = Llm(cfg)
    private val jev = Jev(cfg)
    private val helper = Executors.newSingleThreadExecutor()  // 딥서치·회고는 뒤에서, 플레이는 안 멈춤
    private val thinking = AtomicBoolean(false)

    /** 오래 걸리는 생각을 백그라운드로. 이미 생각 중이면 건너뜀 */
    private fun think(life: Life, what: String, job: () -> Unit) {
        if (!thinking.compareAndSet(false, true)) return
        life.boost = true; Bus.log(what)
        helper.execute {
            try { job() } catch (e: Exception) { Bus.log("  ↪ 생각 실패: ${e.message?.take(120)}") }
            finally { life.boost = false; thinking.set(false) }
        }
    }

    /** 입력 실행: tap:x:y / swipe:x1:y1:x2:y2 / hold:x:y:초 / key:back / wait:초 (x,y는 화면 비율) */
    fun act(spec: String) {
        val p = spec.split(":")
        if (p[0] == "wait") { Thread.sleep(((p.getOrNull(1)?.toFloatOrNull() ?: 1f) * 1000).toLong()); return }
        val hand = HandsService.me ?: throw IllegalStateException("손(접근성 서비스)이 꺼져 있어요")
        fun x(i: Int) = (p[i].toFloat() * svc.w).coerceIn(1f, svc.w - 1f)
        fun y(i: Int) = (p[i].toFloat() * svc.h).coerceIn(1f, svc.h - 1f)
        when (p[0]) {
            "tap", "click" -> hand.tap(x(1), y(2))
            "swipe" -> hand.swipe(x(1), y(2), x(3), y(4), 300)
            "hold" -> hand.swipe(x(1), y(2), x(1), y(2), ((p.getOrNull(3)?.toFloatOrNull() ?: 0.5f) * 1000).toLong())
            "key" -> hand.key(p.getOrElse(1) { "back" })
        }
    }

    fun live() {
        while (BrainService.running) {
            try { run() } catch (e: Exception) {
                if (!BrainService.running) break
                Bus.log("⚠ 다쳤지만 회복 중... ${e.message?.take(200)}")
                Thread.sleep(5000)
            }
        }
        helper.shutdownNow()
    }

    /** 게임 화면이 앞에 올 때까지 기다림 (GrowAI 자신·홈 화면은 무시) */
    private fun waitForGame(): String {
        var told = false
        while (BrainService.running) {
            val p = HandsService.pkg
            if (p.isNotEmpty() && p != "com.growai" && !p.contains("launcher")) return p
            if (!told) { Bus.log("🎮 게임을 열면 바로 시작해요"); Bus.status("게임 기다리는 중 👀"); told = true }
            Thread.sleep(1000)
        }
        return ""
    }

    private fun run() {
        val pkg = waitForGame().ifEmpty { return }
        Thread.sleep(1500)  // 게임 화면이 다 뜰 때까지
        val game = llm.identify(svc.jpeg(), pkg).ifBlank { pkg }
        val life = Life(mem, game)
        val (steps, lessons) = mem.stats(game)
        Bus.log("🎮 $game | Lv.${life.level} · ${steps}스텝 · 교훈 ${lessons}개")
        val acts = mem.getActions(game) ?: Discover(svc, this, llm).run(game).also { mem.saveActions(game, it) }
        var shot = svc.bitmap()
        var state = llm.describe(svc.jpeg(shot), game)
        var stuck = 0; var rounds = 0; var n = 0; var saved = 0
        val stuckAfter = cfg["stuck_after"].toIntOrNull() ?: 5

        while (BrainService.running) {
            n++
            if (HandsService.pkg != pkg && n % 3 == 0) {  // 다른 앱으로 바뀜 → 잠깐 멈추고 다시 인식
                Bus.log("↩ 게임 밖으로 나갔어요"); return
            }
            val rates = mem.rates(game); val counts = mem.counts(game)
            val live = life.awake(acts, rates, counts)
            val picked = life.explore(live, counts)
            val (chosen, why) = if (picked != null) picked to "호기심" else {
                val choices = live.mapValues { (k, v) ->
                    v.desc + (rates[k]?.let { " [성공률 ${(it * 100).toInt()}%]" } ?: " [아직 안 해봄]") }
                val (a, conf) = jev.choose("게임: $game\n현재 상황:\n$state\n\n기억:\n${mem.recall(game, state)}", choices)
                a to "판단 ${(conf * 100).toInt()}%"
            }
            val action: String = if (acts.containsKey(chosen)) chosen else live.keys.first()
            Bus.log("▶ $action ($why)")
            for (s in acts.getValue(action).does) { if (!BrainService.running) return; act(s) }

            // 가속: 화면이 거의 안 바뀌었으면 AI 호출 2번(눈·판정)을 생략하고 "효과 없음"으로 기록
            val newShot = svc.bitmap()
            val changed = Pix.diff(Pix.gray(shot), Pix.gray(newShot)) > 2.0
            val newState = if (changed) llm.describe(svc.jpeg(newShot), game) else state
            val score = if (changed) jev.progress(state, action, newState) else { saved += 2; 0.05 }
            shot = newShot
            mem.addExp(game, state, action, score)
            life.gain(score)
            Bus.status("$game · Lv.${life.level} · XP ${life.xp.toInt()} · 조작 ${acts.size}개 · " +
                "호기심 ${(life.curiosity * 100).toInt()}% · 아낀 호출 $saved" + if (thinking.get()) " · 🔎조사 중" else "")

            if (n % 25 == 0) life.evolve(acts).takeIf { it.isNotEmpty() }?.let { acts.putAll(it); mem.saveActions(game, acts) }
            if (n % 60 == 0) {  // 회고(일기): 빠른 AI로 뒤에서 정리
                val snap = state; val hist = mem.recent(game, 30)
                think(life, "📔 회고 쓰는 중...") {
                    val tip = llm.ask("게임 '$game' 플레이 기록:\n$hist\n지금 상황:\n$snap\n\n" +
                        "잘 된 것·안 된 것을 보고 다음에 쓸 전략을 한국어 1~2줄로.", 200)
                    mem.addLesson(game, "회고 " + snap.take(200), tip); Bus.log("📔 회고: $tip")
                }
            }
            stuck = if (score > 0.5) 0 else stuck + 1
            if (stuck >= stuckAfter) {  // 막힘 → 제미나이 딥서치는 뒤에서, 그동안 호기심 올려서 계속 시도
                val snap = newState; val hist = mem.recent(game)
                think(life, "🧩 막힘 → 제미나이 딥서치 시작 (플레이는 계속)") {
                    val fix = llm.solve(game, snap, hist)
                    mem.addLesson(game, snap, fix); Bus.log("💡 교훈: ${fix.take(300)}")
                }
                stuck = 0; rounds++
                if (rounds % 3 == 0) { acts.putAll(Discover(svc, this, llm).run(game)); mem.saveActions(game, acts) }
            }
            state = newState
        }
    }
}
