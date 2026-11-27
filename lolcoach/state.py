"""라이엇 Live Client Data API(https://127.0.0.1:2999/liveclientdata/allgamedata) 응답을 다루기 쉬운 형태로.

이 API는 라이엇이 공식 제공하며, 플레이어가 게임 화면(스코어보드 포함)에서 볼 수 있는 정보만 담고 있음.
적 위치·적 스펠 쿨타임 같은 '안 보이는 정보'는 없음.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Player:
    name: str                 # riotId 우선 ("닉#태그")
    names: set[str]           # 이벤트에서 이 사람을 가리킬 수 있는 모든 이름
    champion: str
    team: str                 # ORDER / CHAOS
    position: str             # TOP JUNGLE MIDDLE BOTTOM UTILITY 또는 ""
    level: int
    kills: int
    deaths: int
    assists: int
    cs: int                   # Live API는 10개 단위로 갱신됨
    ward_score: float
    is_dead: bool
    respawn: float
    items: list[dict]
    spells: list[str]

    @property
    def item_ids(self) -> set[int]:
        return {int(i.get("itemID", 0)) for i in self.items}


@dataclass
class GameState:
    time: float
    mode: str
    me: Player
    gold: float
    allies: list[Player]
    enemies: list[Player]
    events: list[dict] = field(default_factory=list)

    @property
    def opponent(self) -> Player | None:
        """맞라이너: 같은 포지션의 적. 포지션 정보가 없으면 None"""
        if not self.me.position:
            return None
        for e in self.enemies:
            if e.position == self.me.position:
                return e
        return None

    def find(self, name: str) -> Player | None:
        for p in [self.me, *self.allies, *self.enemies]:
            if name in p.names:
                return p
        return None

    def summary(self) -> dict:
        """LLM에 넣을 짧은 요약 (토큰 절약)"""
        def brief(p: Player) -> dict:
            return {
                "챔피언": p.champion, "포지션": p.position or "?", "레벨": p.level,
                "KDA": f"{p.kills}/{p.deaths}/{p.assists}", "CS": p.cs,
                "죽음": round(p.respawn) if p.is_dead else 0,
                "아이템": [i.get("displayName", "") for i in p.items if i.get("displayName")],
            }
        opp = self.opponent
        return {
            "게임시간": fmt_time(self.time),
            "나": {**brief(self.me), "보유골드": int(self.gold), "와드점수": round(self.me.ward_score, 1)},
            "맞라이너": brief(opp) if opp else None,
            "아군": [brief(p) for p in self.allies],
            "적군": [brief(p) for p in self.enemies],
        }


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _names(p: dict) -> set[str]:
    out = set()
    for k in ("riotId", "riotIdGameName", "summonerName"):
        v = p.get(k)
        if v:
            out.add(v)
            out.add(v.split("#")[0])
    return out


def _player(p: dict) -> Player:
    sc = p.get("scores", {})
    spells = p.get("summonerSpells", {})
    names = _names(p)
    return Player(
        name=p.get("riotId") or p.get("summonerName") or p.get("championName", "?"),
        names=names,
        champion=p.get("championName", "?"),
        team=p.get("team", ""),
        position=p.get("position", "") or "",
        level=int(p.get("level", 1)),
        kills=int(sc.get("kills", 0)),
        deaths=int(sc.get("deaths", 0)),
        assists=int(sc.get("assists", 0)),
        cs=int(sc.get("creepScore", 0)),
        ward_score=float(sc.get("wardScore", 0.0)),
        is_dead=bool(p.get("isDead", False)),
        respawn=float(p.get("respawnTimer", 0.0)),
        items=list(p.get("items", [])),
        spells=[s.get("displayName", "") for s in spells.values() if isinstance(s, dict)],
    )


def parse(data: dict) -> GameState:
    active = data.get("activePlayer", {})
    my_names = _names(active)
    players = [_player(p) for p in data.get("allPlayers", [])]
    me = next((p for p in players if p.names & my_names), None)
    if me is None:
        raise ValueError("내 챔피언을 찾을 수 없음 (관전/리플레이 중일 수 있음)")
    allies = [p for p in players if p.team == me.team and p is not me]
    enemies = [p for p in players if p.team != me.team]
    return GameState(
        time=float(data.get("gameData", {}).get("gameTime", 0.0)),
        mode=data.get("gameData", {}).get("gameMode", ""),
        me=me,
        gold=float(active.get("currentGold", 0.0)),
        allies=allies,
        enemies=enemies,
        events=list(data.get("events", {}).get("Events", [])),
    )
