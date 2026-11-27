"""롤 없이 테스트: 라이엇 Live Client API(https://127.0.0.1:2999)를 흉내내는 가짜 게임.

아리(미드) vs 제드. 배속으로 진행되며 2레벨 선점 → 솔킬 → 귀환 골드 → 데스 → 드래곤 순서로 이벤트가 나온다.
"""
from __future__ import annotations

import json
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ME = {"riotId": "나#KR1", "riotIdGameName": "나", "summonerName": "나"}


def _p(riot, champ, team, pos, level, k=0, d=0, a=0, cs=0, dead=False, respawn=0.0, items=()):
    name = riot.split("#")[0]
    return {"riotId": riot, "riotIdGameName": name, "summonerName": name, "championName": champ, "team": team,
            "position": pos, "level": level, "isDead": dead, "respawnTimer": respawn,
            "scores": {"kills": k, "deaths": d, "assists": a, "creepScore": cs, "wardScore": 0},
            "items": [{"itemID": i, "displayName": str(i)} for i in items], "summonerSpells": {}}


def game_at(t: float) -> dict:
    """게임 시간 t초의 상태"""
    my_lv = 1 if t < 95 else 2 if t < 150 else 3 if t < 200 else min(18, 3 + int((t - 200) // 60))
    zed_lv = 1 if t < 105 else 2 if t < 160 else 3 if t < 210 else min(18, 3 + int((t - 210) // 70))
    kills = 1 if t >= 200 else 0
    deaths = 1 if t >= 400 else 0
    zed_dead = 200 <= t < 214
    me_dead = 400 <= t < 418
    gold = max(0.0, 500 + max(0, t - 65) * 2.2 + kills * 300 - (1500 if t > 260 else 0))
    events = [{"EventID": 0, "EventName": "GameStart", "EventTime": 0.0}]
    if t >= 65:
        events.append({"EventID": 1, "EventName": "MinionsSpawning", "EventTime": 65.0})
    if t >= 200:
        events.append({"EventID": 2, "EventName": "ChampionKill", "EventTime": 200.0, "KillerName": "나",
                       "VictimName": "적미드", "Assisters": []})
    if t >= 400:
        events.append({"EventID": 3, "EventName": "ChampionKill", "EventTime": 400.0, "KillerName": "적정글",
                       "VictimName": "나", "Assisters": ["적미드"]})
    if t >= 420:
        events.append({"EventID": 4, "EventName": "DragonKill", "EventTime": 420.0, "DragonType": "Fire",
                       "KillerName": "적정글", "Stolen": "False"})
    players = [
        _p("나#KR1", "Ahri", "ORDER", "MIDDLE", my_lv, kills, deaths, 0, int(t / 10 // 10 * 10), me_dead,
           18 - (t - 400) if me_dead else 0, (1056,) if t < 260 else (1056, 3802)),
        _p("우리정글#KR1", "LeeSin", "ORDER", "JUNGLE", my_lv),
        _p("우리탑#KR1", "Jax", "ORDER", "TOP", my_lv),
        _p("우리원딜#KR1", "Jinx", "ORDER", "BOTTOM", my_lv),
        _p("우리서폿#KR1", "Thresh", "ORDER", "UTILITY", my_lv),
        _p("적미드#KR1", "Zed", "CHAOS", "MIDDLE", zed_lv, 0, kills, 1 if t >= 400 else 0, int(t / 11 // 10 * 10),
           zed_dead, 214 - t if zed_dead else 0),
        _p("적정글#KR1", "Vi", "CHAOS", "JUNGLE", zed_lv, 1 if t >= 400 else 0),
        _p("적탑#KR1", "Renekton", "CHAOS", "TOP", zed_lv),
        _p("적원딜#KR1", "Kaisa", "CHAOS", "BOTTOM", zed_lv),
        _p("적서폿#KR1", "Nautilus", "CHAOS", "UTILITY", zed_lv),
    ]
    return {"activePlayer": {**ME, "currentGold": round(gold, 1), "level": my_lv},
            "allPlayers": players, "events": {"Events": events},
            "gameData": {"gameTime": t, "gameMode": "CLASSIC", "mapNumber": 11}}


def run_mock(speed: float = 10.0, port: int = 2999) -> None:
    from .__main__ import ensure_cert
    start = time.monotonic()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if not self.path.startswith("/liveclientdata/allgamedata"):
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(game_at((time.monotonic() - start) * speed)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    cert, key = ensure_cert("127.0.0.1", name="mock")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"가짜 게임 실행 중: https://127.0.0.1:{port} ({speed}배속). 다른 창에서 python -m lolcoach 실행")
    srv.serve_forever()
