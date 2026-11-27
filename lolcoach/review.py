"""경기 후 판단 리플레이: 내 경기 타임라인을 챌린저 장인 합의와 비교하고,
중요한 순간마다 '챌린저/프로라면 어떻게 했을지'를 Claude가 설명한다.

롤 클라이언트에서 리플레이를 켜두면 각 순간의 시간으로 바로 이동(Replay API)도 가능.
"""
from __future__ import annotations

import json

from .coach import load_session
from .config import Config
from .knowledge import ANSWER_RULES, PLAYBOOK
from .learn import Brain, benchmark, describe, extract
from .llm import Llm
from .riot import DataDragon, Riot, live_post
from .state import fmt_time

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "priorities": {"type": "array", "items": {"type": "string"}},
        "moments": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "t": {"type": "integer"},
                "title": {"type": "string"},
                "what_you_did": {"type": "string"},
                "challenger_would": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["t", "title", "what_you_did", "challenger_would", "why"],
            "additionalProperties": False,
        }},
    },
    "required": ["headline", "priorities", "moments"],
    "additionalProperties": False,
}

REVIEW_RULES = """\
[경기 후 리뷰 규칙]
- 너는 다이아 2 플레이어를 챌린저로 만드는 코치다. 이번 경기의 라인전을 정점 플레이어 합의 통계와 비교해 리뷰한다.
- moments: 제공된 '핵심 순간' 각각에 대해 내가 한 것, 챌린저라면 했을 것, 이유를 구체적으로. t는 그 순간의 초.
  타임라인으로 확인할 수 없는 것(웨이브 위치 등)은 단정하지 말고 "웨이브가 ~였다면" 조건으로 말해라.
- priorities: 다음 게임에서 고칠 것 3개, 가장 효과 큰 순서. 각각 실행 가능한 한 문장.
- headline: 이번 경기 한 줄 평가.
- 숫자는 제공된 데이터에 있는 것만 쓴다.
"""


def key_moments(me: dict, match: dict, timeline: dict, session: dict | None) -> list[dict]:
    """내 경기에서 리뷰할 순간들 (시간순)"""
    f = me["features"]
    info = match["info"]
    pid = next(p["participantId"] for p in info["participants"] if p["puuid"] == me["puuid"])
    champs = {p["participantId"]: p["championName"] for p in info["participants"]}
    out = []
    for fr in timeline["info"]["frames"]:
        for e in fr.get("events", []):
            if e.get("type") != "CHAMPION_KILL":
                continue
            ts = e["timestamp"] // 1000
            if e.get("victimId") == pid:
                helpers = [champs.get(a, "?") for a in e.get("assistingParticipantIds", [])]
                out.append({"t": ts, "type": "death", "desc": f"사망 (킬: {champs.get(e.get('killerId'), '?')}"
                            + (f", 도움: {', '.join(helpers)}" if helpers else "") + ")",
                            "pos": e.get("position")})
            elif e.get("killerId") == pid:
                out.append({"t": ts, "type": "kill", "desc": f"{champs.get(e.get('victimId'), '?')} 처치"
                            + (" (솔킬)" if not e.get("assistingParticipantIds") else ""), "pos": e.get("position")})
    for a in f.get("after_lane_kill", []):
        out.append({"t": a["t"] + 1, "type": "after_kill",
                    "desc": "맞라이너 처치 후 " + (f"{a['back_delay']}초 뒤 귀환" if a["backed"] else "75초 안에 귀환 안 함")
                    + (", 플레이트 파괴" if a["plate"] else "")})
    if "first_back" in f:
        out.append({"t": f["first_back"], "type": "first_back",
                    "desc": f"첫 귀환 (보유 골드 약 {f.get('first_back_gold', '?')})"})
    for m in (10, 15):
        if f"cs{m}" in f:
            out.append({"t": m * 60, "type": "checkpoint",
                        "desc": f"{m}분: CS {f[f'cs{m}']}, 맞라이너 대비 골드 {f.get(f'golddiff{m}', '?')}, "
                                f"CS {f.get(f'csdiff{m}', '?')}"})
    if session:
        for c in session.get("calls", []):
            if c["kind"] in ("solo_kill", "death", "back_gold", "level_lead", "level_behind"):
                out.append({"t": c["t"], "type": "coach_call", "desc": f"게임 중 코치 콜: {c['text']}"})
    out.sort(key=lambda x: x["t"])
    return [m for m in out if m["t"] <= 20 * 60][:25]   # 라인전 중심


async def review_latest(cfg: Config, llm: Llm, brain: Brain, dd: DataDragon | None,
                        session_id: str | None = None, match_id: str | None = None) -> dict:
    if not cfg.my_riot_id:
        raise ValueError("설정에서 내 라이엇 ID(닉네임#태그)를 먼저 입력해 주세요")
    riot = Riot(cfg.riot_api_key, cfg.platform, cfg.region)
    acc = riot.account(cfg.my_riot_id)
    if not acc:
        raise ValueError("라이엇 ID를 찾을 수 없어요")
    if not match_id:
        ids = riot.match_ids(acc["puuid"], count=1, queue=420) or riot.match_ids(acc["puuid"], count=1, queue=440)
        if not ids:
            raise ValueError("최근 랭크 경기가 없어요")
        match_id = ids[0]
    match, timeline = riot.match(match_id), riot.timeline(match_id)
    if not match or not timeline:
        raise ValueError("경기 데이터를 불러올 수 없어요")
    me = extract(match, timeline, keep_puuids={acc["puuid"]})[0]
    bench = benchmark(brain, me["champion"], me["position"], top_n=cfg.top_players_per_champion,
                      min_games=cfg.min_games_on_champion, opp_champion=me["opp_champion"], dd=dd)
    session = load_session(session_id) if session_id else None
    moments = key_moments(me, match, timeline, session)

    f = me["features"]
    mine = {k: f.get(k) for k in ("cs10", "cs15", "golddiff10", "golddiff15", "csdiff10", "first_back",
                                  "first_back_gold", "deaths14", "wards_pm", "plates", "solo_kills")}
    mine["skills"] = "".join(f.get("skills", [])[:9])
    if dd and dd.items:
        mine["build"] = [dd.name(i) for _, i in f.get("purchases", []) if dd.is_legendary(i)][:4]
    prompt = (f"[내 경기] {me['champion']} {me['position']} vs {me['opp_champion']} — "
              f"{'승리' if me['win'] else '패배'}\n[내 지표] {json.dumps(mine, ensure_ascii=False)}\n"
              f"[핵심 순간]\n" + "\n".join(f"- t={m['t']} ({fmt_time(m['t'])}) {m['desc']}" for m in moments))
    system = [{"type": "text", "text": PLAYBOOK + "\n" + ANSWER_RULES + "\n" + REVIEW_RULES},
              {"type": "text", "text": "[챌린저·프로 학습 데이터]\n" + describe(bench),
               "cache_control": {"type": "ephemeral"}}]
    result = await llm.json(system, [{"role": "user", "content": prompt}], REVIEW_SCHEMA, cfg.review_effort)
    return {"match_id": match_id, "champion": me["champion"], "opponent": me["opp_champion"],
            "win": me["win"], "mine": mine, "benchmark": bench, "raw_moments": moments, **result}


def seek_replay(cfg: Config, seconds: float) -> None:
    """롤 클라이언트에서 열린 리플레이를 해당 시간(5초 전)으로 이동.
    game.cfg [General] 에 EnableReplayApi=1 필요."""
    live_post(cfg.live_url, "/replay/playback", {"time": max(0.0, seconds - 5), "paused": False})
