package com.growai

/** 생명 루프: 보기 → 기억 → (호기심 or Jev 판단) → 터치 → 평가 → 저장 → 성장 → 막히면 스스로 해결.
 *  오류가 나도 죽지 않고 5초 쉬었다 다시 일어난다. */
class Brain(private val svc: BrainService, private val cfg: Config, private val mem: Memory) {
    private val llm = Llm(cfg)
    private val jev = Jev(cfg)

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
        var state = llm.describe(svc.jpeg(), game)
        var stuck = 0; var rounds = 0; var n = 0
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

            val newState = llm.describe(svc.jpeg(), game)
            val score = jev.progress(state, action, newState)
            mem.addExp(game, state, action, score)
            life.gain(score)
            Bus.status("$game · Lv.${life.level} · XP ${life.xp.toInt()} · 조작 ${acts.size}개 · 호기심 ${(life.curiosity * 100).toInt()}%")

            if (n % 25 == 0) life.evolve(acts).takeIf { it.isNotEmpty() }?.let { acts.putAll(it); mem.saveActions(game, acts) }
            if (n % 60 == 0) {  // 회고(일기)
                val tip = llm.solve(game, state, mem.recent(game, 30), "(회고: 잘 된 것/안 된 것 정리)")
                mem.addLesson(game, "회고 " + state.take(200), tip); Bus.log("📔 회고: $tip")
            }
            stuck = if (score > 0.5) 0 else stuck + 1
            if (stuck >= stuckAfter) {
                Bus.log("🧩 막힘 → 웹검색+AI로 해결 중")
                val fix = llm.solve(game, newState, mem.recent(game), Http.search("$game ${newState.take(80)} guide"))
                mem.addLesson(game, newState, fix); Bus.log("💡 교훈: $fix")
                stuck = 0; rounds++
                if (rounds % 3 == 0) { acts.putAll(Discover(svc, this, llm).run(game)); mem.saveActions(game, acts) }
            }
            state = newState
        }
    }
}
