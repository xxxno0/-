import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from lolcoach.coach import Coach, list_sessions, load_session
from lolcoach.config import Config
from lolcoach.learn import Brain, benchmark, describe, extract, skill_max_order
from lolcoach.llm import split_sentences
from lolcoach.mock import game_at
from lolcoach.riot import DataDragon
from lolcoach.server import create_app
from lolcoach.state import parse
from lolcoach.triggers import Detector


# ------------------------------------------------------------------ 가짜 데이터

def fake_dd() -> DataDragon:
    dd = DataDragon()
    dd.items = {
        1056: {"name": "도란의 반지", "gold": {"total": 400}, "into": []},
        3802: {"name": "잃어버린 장", "gold": {"total": 1200}, "into": [6655]},
        6655: {"name": "루덴의 동반자", "gold": {"total": 2750}},
        3020: {"name": "마법사의 신발", "gold": {"total": 1100}},
        4645: {"name": "그림자불꽃", "gold": {"total": 3200}},
        3089: {"name": "라바돈의 죽음모자", "gold": {"total": 3600}},
    }
    return dd


def fake_match(mid: str, me_puuid: str, win: bool, back_after_kill: bool = True, opp="Zed"):
    parts = []
    for i in range(1, 11):
        team = 100 if i <= 5 else 200
        pos = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"][(i - 1) % 5]
        parts.append({"participantId": i, "puuid": me_puuid if i == 3 else f"x{mid}{i}", "teamId": team,
                      "teamPosition": pos, "championName": "Ahri" if i == 3 else (opp if i == 8 else f"C{i}"),
                      "win": win if team == 100 else not win, "summoner1Id": 4, "summoner2Id": 14,
                      "perks": {"styles": [{"style": 8100, "selections": [{"perk": 8112}]}, {"style": 8200}]}})
    frames = []
    for m in range(16):
        pf = {str(i): {"minionsKilled": m * 8 + (4 if i == 3 else 0), "jungleMinionsKilled": 0,
                       "totalGold": 500 + m * 400 + (300 if i == 3 else 0), "currentGold": 300 + m * 50, "level": 1 + m // 2}
              for i in range(1, 11)}
        frames.append({"participantFrames": pf, "events": []})
    ev = frames[0]["events"]
    for slot in [1, 3, 2, 1, 1, 4, 1, 1, 3, 3, 4, 3, 3]:
        ev.append({"type": "SKILL_LEVEL_UP", "participantId": 3, "skillSlot": slot, "levelUpType": "NORMAL", "timestamp": 1000})
    ev.append({"type": "ITEM_PURCHASED", "participantId": 3, "itemId": 1056, "timestamp": 5_000})
    frames[4]["events"].append({"type": "CHAMPION_KILL", "killerId": 3, "victimId": 8,
                                "assistingParticipantIds": [], "timestamp": 250_000, "position": {"x": 7000, "y": 7000}})
    if back_after_kill:
        frames[4]["events"].append({"type": "ITEM_PURCHASED", "participantId": 3, "itemId": 3802, "timestamp": 290_000})
    frames[4]["events"].append({"type": "TURRET_PLATE_DESTROYED", "killerId": 3, "timestamp": 270_000})
    frames[6]["events"].append({"type": "ITEM_PURCHASED", "participantId": 3, "itemId": 6655, "timestamp": 400_000})
    frames[7]["events"].append({"type": "CHAMPION_KILL", "killerId": 8, "victimId": 3,
                                "assistingParticipantIds": [7], "timestamp": 430_000})
    frames[10]["events"].append({"type": "ITEM_PURCHASED", "participantId": 3, "itemId": 3020, "timestamp": 610_000})
    frames[12]["events"].append({"type": "ITEM_PURCHASED", "participantId": 3, "itemId": 4645, "timestamp": 750_000})
    frames[12]["events"].append({"type": "WARD_PLACED", "creatorId": 3, "wardType": "CONTROL_WARD", "timestamp": 760_000})
    match = {"info": {"participants": parts, "gameDuration": 1500, "gameVersion": "16.23"}}
    return match, {"info": {"frames": frames}}


@pytest.fixture
def brain(tmp_path):
    b = Brain(tmp_path / "brain.db")
    for p in range(6):
        b.upsert_player(f"p{p}", "CHALLENGER", 1500 - p * 100, 300, 250, riot_id=f"장인{p}#KR1")
        for g in range(6):
            mid = f"KR_{p}_{g}"
            m, tl = fake_match(mid, f"p{p}", win=g % 3 != 0, back_after_kill=(g != 0))
            b.add_match(mid, "16.23", extract(m, tl, keep_puuids={f"p{p}"}))
    return b


class FakeLlm:
    def __init__(self, answer="웨이브를 밀고 귀환하세요. 상대가 없을 때가 기회예요."):
        self.answer = answer
        self.calls = []

    async def stream(self, system, messages, effort, max_tokens=2000):
        self.calls.append({"system": system, "messages": messages, "effort": effort})
        for i in range(0, len(self.answer), 7):
            yield self.answer[i:i + 7]

    async def json(self, system, messages, schema, effort):
        self.calls.append({"system": system, "messages": messages, "effort": effort})
        return {"headline": "좋았음", "priorities": ["a", "b", "c"], "moments": []}


# ------------------------------------------------------------------ 테스트

def test_extract_features():
    m, tl = fake_match("KR_1", "me", True)
    s = extract(m, tl, keep_puuids={"me"})[0]
    f = s["features"]
    assert s["champion"] == "Ahri" and s["position"] == "MIDDLE" and s["opp_champion"] == "Zed"
    assert f["cs10"] == 84 and f["golddiff10"] == 300 and f["csdiff10"] == 4
    assert f["first_back"] == 290 and f["first_back_items"] == [3802]
    assert f["after_lane_kill"] == [{"t": 250, "backed": True, "back_delay": 40, "plate": True, "gold": 500}]
    assert f["deaths14"] == 1 and f["solo_kills"] == 1 and f["plates"] == 1 and f["control_wards"] == 1
    assert f["keystone"] == 8112 and f["skills"][:3] == ["Q", "E", "W"]


def test_skill_max_order():
    assert skill_max_order(list("QEWQQRQQEEREE")) == "Q>E>W"


def test_benchmark_consensus(brain):
    b = benchmark(brain, "Ahri", top_n=5, min_games=5, opp_champion="Zed", dd=fake_dd())
    assert b["position"] == "MIDDLE" and len(b["experts"]) == 5 and not b["low_confidence"]
    assert b["cs10"] == 84 and b["skill_max"] == {"order": "Q>E>W", "agree": 100}
    assert [c["item"] for c in b["core_build"]] == ["루덴의 동반자", "그림자불꽃"]
    a = b["after_lane_kill"]
    assert a["cases"] == 30 and a["back_within_75s"] == 83 and a["plate_within_60s"] == 100
    assert b["matchup"]["vs"] == "Zed"
    text = describe(b)
    assert "정점 플레이어 5명" in text and "75초 안에 귀환 83%" in text


def test_benchmark_empty(tmp_path):
    b = benchmark(Brain(tmp_path / "e.db"), "Teemo")
    assert "학습 데이터 없음" in describe(b) or "데이터 없음" in describe(b)


def test_parse_state():
    st = parse(game_at(210))
    assert st.me.champion == "Ahri" and st.opponent.champion == "Zed"
    assert st.opponent.is_dead and st.me.kills == 1
    assert st.summary()["맞라이너"]["챔피언"] == "Zed"


def test_detector_sequence():
    det = Detector({"core_build": [{"id": 6655, "item": "루덴의 동반자", "cost": 1000}], "cs10": 80}, fake_dd())
    kinds = []
    for t in range(0, 700, 5):
        kinds += [(m.kind, m.llm) for m in det.update(parse(game_at(t)))]
    names = [k for k, _ in kinds]
    assert names[0] == "game_plan" and kinds[0][1] is True
    assert "level_lead" in names                     # 2레벨 선점
    assert ("solo_kill", True) in kinds              # 솔킬 → AI 판단
    assert "death" in names and "objective" in names and "back_gold" in names
    assert names.index("level_lead") < names.index("solo_kill") < names.index("death")
    assert names.count("solo_kill") == 1             # 같은 이벤트 중복 없음


def test_detector_midgame_start_ignores_old_events():
    det = Detector()
    assert det.update(parse(game_at(500))) == []
    assert all(m.kind != "solo_kill" for m in det.update(parse(game_at(505))))


def test_split_sentences():
    done, rest = split_sentences("웨이브 밀고 귀환. 지금 3.5초 남았")
    assert done == ["웨이브 밀고 귀환."] and rest == " 지금 3.5초 남았"


def test_coach_solo_kill_flow(brain, tmp_path):
    sent = []

    async def bc(m):
        sent.append(m)

    llm = FakeLlm()
    coach = Coach(Config(min_gap_between_calls=0), llm, brain, fake_dd(), bc, sessions_dir=tmp_path / "s")

    async def run():
        for t in range(0, 230, 5):
            await coach.on_state(parse(game_at(t)))
        await asyncio.gather(*coach._tasks)
        return coach.end_game()

    sid = asyncio.run(run())
    says = [m["text"] for m in sent if m["type"] == "say"]
    assert any("솔킬" in s for s in says) and "웨이브를 밀고 귀환하세요." in says
    q = [c for c in llm.calls if "솔킬" in c["messages"][-1]["content"]][0]
    assert "75초 안에 귀환" in q["system"][-1]["text"]             # 챌린저 데이터가 프롬프트에 들어감
    assert q["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert '"맞라이너"' in q["messages"][-1]["content"] and q["effort"] == "low"
    rec = load_session(sid, tmp_path / "s")
    assert rec["champion"] == "Ahri" and rec["opponent"] == "Zed"
    assert any(c["kind"] == "solo_kill" for c in rec["calls"]) and rec["qa"] and rec["snapshots"]
    assert list_sessions(tmp_path / "s")[0]["id"] == sid


def test_coach_followup_keeps_history(brain, tmp_path):
    async def bc(m):
        pass
    llm = FakeLlm("좋아요.")
    coach = Coach(Config(), llm, brain, None, bc, sessions_dir=tmp_path / "s")
    asyncio.run(coach.ask("첫 질문"))
    asyncio.run(coach.ask("이어서 질문"))
    msgs = llm.calls[-1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"] and msgs[0]["content"] == "첫 질문"


def test_server_endpoints(brain, tmp_path, monkeypatch):
    monkeypatch.setattr("lolcoach.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("lolcoach.config.DATA_DIR", tmp_path)
    app = create_app(Config(), FakeLlm(), brain, fake_dd(), poll=False)
    app.state.coach.sessions_dir = tmp_path / "s"
    with TestClient(app) as c:
        assert "롤 코치" in c.get("/").text
        assert c.post("/api/ask", json={"q": "솔킬 땄는데 집 가면 라인 걸쳐져"}).json()["answer"]
        b = c.get("/api/benchmark", params={"champion": "Ahri"}).json()
        assert b["benchmark"]["position"] == "MIDDLE"
        assert c.get("/api/brain").json()["samples"] == 36
        r = c.post("/api/config", json={"riot_api_key": "RGAPI-x", "pro_accounts": "A#1\nB#2"}).json()
        assert r["riot_api_key"] == "설정됨" and r["pro_accounts"] == ["A#1", "B#2"]
        assert json.loads((tmp_path / "config.json").read_text())["riot_api_key"] == "RGAPI-x"
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "status"
            ws.send_json({"type": "ask", "q": "지금 뭐해?"})
            types = [ws.receive_json()["type"] for _ in range(3)]
            assert types[0] == "answer_start"
        assert c.post("/api/review", json={}).status_code == 400   # 라이엇 ID 없음 → 안내
