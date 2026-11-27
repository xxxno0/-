"""게임 상태 변화에서 '코칭할 순간'을 잡아낸다.

즉시 말해야 하는 건 템플릿 문장으로 바로 내보내고(지연 0초),
판단이 필요한 순간(솔킬 직후, 게임 시작 플랜 등)은 llm=True로 표시해서 코치가 Claude에게 물어보게 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .knowledge import OBJECTIVES
from .riot import DataDragon
from .state import GameState, fmt_time


@dataclass
class Moment:
    kind: str
    time: float
    text: str                    # 바로 읽을 문장 (llm=True면 AI 답 전까지 자리표시)
    priority: int = 2            # 1=긴급, 2=중요, 3=참고
    llm: bool = False            # True면 Claude가 상황 판단해서 말함
    question: str = ""           # Claude에게 보낼 질문
    data: dict = field(default_factory=dict)


class Detector:
    def __init__(self, benchmark: dict | None = None, dd: DataDragon | None = None):
        self.bench = benchmark or {}
        self.dd = dd
        self.prev: GameState | None = None
        self.seen_events: set[int] = set()
        self.flags: set[str] = set()
        self.last_back_call = -999.0
        self.next_dragon: float | None = OBJECTIVES["dragon_first"]
        self.next_baron: float | None = OBJECTIVES["baron_first"]
        self.opp_items: set[int] | None = None

    def update(self, st: GameState) -> list[Moment]:
        out: list[Moment] = []
        if self.prev is None or st.time + 5 < self.prev.time:   # 첫 상태 or 새 게임
            if st.time < 120:
                out.append(self._game_plan(st))
            # 게임 중간에 켰으면 지난 이벤트는 무시
            self.seen_events = {e.get("EventID") for e in st.events}
            self.prev = st
            return out
        for e in st.events:
            eid = e.get("EventID")
            if eid in self.seen_events:
                continue
            self.seen_events.add(eid)
            out.extend(self._event(st, e))
        out.extend(self._levels(st))
        out.extend(self._back(st))
        out.extend(self._objectives(st))
        out.extend(self._checkpoints(st))
        out.extend(self._opp_items(st))
        self.prev = st
        return out

    # ------------------------------------------------------------ 규칙들

    def _game_plan(self, st: GameState) -> Moment:
        opp = st.opponent
        vs = f" 상대는 {opp.champion}" if opp else ""
        return Moment("game_plan", st.time, f"{st.me.champion} 게임 시작.{vs}", 2, llm=True,
                      question="게임 시작이야. 이 매치업에서 라인전 플랜을 정점 플레이어 기준으로 짧게 말해줘: "
                               "1~3레벨 스킬, 초반 교환 타이밍, 첫 귀환 목표(시간과 살 아이템), 조심할 것 하나.")

    def _event(self, st: GameState, e: dict) -> list[Moment]:
        name = e.get("EventName")
        t = float(e.get("EventTime", st.time))
        me, opp = st.me, st.opponent
        if name == "ChampionKill":
            killer, victim = e.get("KillerName", ""), e.get("VictimName", "")
            assisters = e.get("Assisters", [])
            vp = st.find(victim)
            respawn = int(vp.respawn) if vp and vp.respawn else 0
            if killer in me.names and opp and victim in opp.names and not assisters:
                return [Moment("solo_kill", t, f"솔킬! 상대 부활까지 {respawn}초.", 1, llm=True,
                               question=f"방금 {opp.champion} 솔킬 땄어. 상대 부활까지 {respawn}초. "
                                        "지금 귀환할지, 웨이브를 어떻게 처리할지 정점 플레이어처럼 판단해줘.",
                               data={"respawn": respawn})]
            if killer in me.names and vp and vp.team != me.team:
                return [Moment("kill", t, f"{vp.champion} 처치. 부활까지 {respawn}초. "
                                          "웨이브 밀어 넣고 귀환이나 오브젝트를 노리세요.", 2)]
            if victim in me.names:
                kp = st.find(killer)
                by = kp.champion if kp else "적"
                return [Moment("death", t, f"{by}에게 사망, 부활 {int(me.respawn)}초. "
                                           "정글 위치, 웨이브 위치, 체력 관리 중 무엇 때문이었는지 기억해 두세요. "
                                           "부활하면 웨이브부터 확인.", 2, data={"killer": by})]
            if opp and victim in opp.names:
                return [Moment("opp_dead", t, f"맞라이너 {opp.champion} 사망, {respawn}초. "
                                              "웨이브를 포탑에 밀어 넣고 플레이트나 귀환하세요.", 2)]
        elif name == "DragonKill":
            self.next_dragon = t + OBJECTIVES["dragon_respawn"]
            self.flags.discard("dragon_warn")
        elif name == "BaronKill":
            self.next_baron = t + OBJECTIVES["baron_respawn"]
            self.flags.discard("baron_warn")
        return []

    def _levels(self, st: GameState) -> list[Moment]:
        out = []
        opp, prev = st.opponent, self.prev
        if not opp or not prev:
            return out
        for lv in (2, 3, 6):
            key = f"lv{lv}"
            if key in self.flags:
                continue
            if st.me.level >= lv > opp.level:
                self.flags.add(key)
                out.append(Moment("level_lead", st.time, f"{lv}레벨 먼저 찍었어요. 상대가 {opp.level}레벨일 때 바로 교환 거세요.", 1))
            elif opp.level >= lv > st.me.level:
                self.flags.add(key)
                out.append(Moment("level_behind", st.time, f"상대가 {lv}레벨 먼저. {lv}레벨 찍을 때까지 교환 피하세요.", 1))
            elif st.me.level >= lv and opp.level >= lv:
                self.flags.add(key)
        return out

    def next_core(self, st: GameState) -> dict | None:
        for c in self.bench.get("core_build", []):
            if c["id"] not in st.me.item_ids:
                return c
        return None

    def _back(self, st: GameState) -> list[Moment]:
        if st.me.is_dead or st.time - self.last_back_call < 90 or st.time < 180:
            return []
        core = self.next_core(st)
        if core and st.gold >= core["cost"]:
            self.last_back_call = st.time
            return [Moment("back_gold", st.time, f"골드 {int(st.gold)}. 귀환하면 {core['item']} 바로 완성. "
                                                 "웨이브 크래시하고 귀환하세요.", 2)]
        if not core and st.gold >= 1300:
            self.last_back_call = st.time
            return [Moment("back_gold", st.time, f"골드 {int(st.gold)}. 큰 부품을 살 수 있어요. "
                                                 "웨이브 크래시 타이밍에 귀환 고려.", 3)]
        return []

    def _objectives(self, st: GameState) -> list[Moment]:
        out = []
        for key, nxt, label in (("dragon_warn", self.next_dragon, "드래곤"), ("baron_warn", self.next_baron, "바론")):
            if nxt is None or key in self.flags:
                continue
            if 0 < nxt - st.time <= 60:
                self.flags.add(key)
                out.append(Moment("objective", st.time, f"{label} 1분 전({fmt_time(nxt)}). "
                                                        "지금 웨이브 밀고 귀환해서 준비하세요.", 2))
        return out

    def _checkpoints(self, st: GameState) -> list[Moment]:
        out = []
        for m in (10, 15):
            key = f"cp{m}"
            target = self.bench.get(f"cs{m}")
            if key in self.flags or st.time < m * 60 or not target:
                continue
            self.flags.add(key)
            if st.time > m * 60 + 30:    # 늦게 켰으면 생략
                continue
            gap = int(target - st.me.cs)
            if gap > 5:
                txt = f"{m}분 CS {st.me.cs}, 장인 중앙값 {int(target)}. {gap}개 부족해요. 웨이브 놓치지 마세요."
            else:
                txt = f"{m}분 CS {st.me.cs}, 장인 중앙값 {int(target)}. 좋아요, 유지하세요."
            out.append(Moment("checkpoint", st.time, txt, 3))
        return out

    def _opp_items(self, st: GameState) -> list[Moment]:
        opp = st.opponent
        if not opp or not self.dd or not self.dd.items:
            return []
        now = opp.item_ids
        if self.opp_items is None:
            self.opp_items = now
            return []
        new = [i for i in now - self.opp_items if self.dd.is_legendary(i)]
        self.opp_items = now
        return [Moment("opp_item", st.time, f"상대 {opp.champion} {self.dd.name(i)} 완성. 파워 스파이크, 교환 조심.", 2)
                for i in new]
