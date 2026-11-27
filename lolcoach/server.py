"""PC에서 돌아가는 코치 서버. 롤 게임 데이터를 읽고, 폰/PC 브라우저 화면에 실시간으로 보낸다."""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import state as state_mod
from .coach import Coach, list_sessions, load_session
from .config import Config
from .learn import Brain, benchmark, crawl, describe
from .llm import ClaudeLlm, Llm
from .review import review_latest, seek_replay
from .riot import DataDragon, Riot, RiotError, live_get
from .screen import Screen

log = logging.getLogger("lolcoach")
WEB = Path(__file__).parent / "web"


# 요청 본문 모델 (모듈 최상위에 있어야 FastAPI가 본문으로 인식)
class Ask(BaseModel):
    q: str


class Live(BaseModel):
    enabled: bool


class Context(BaseModel):
    champion: str
    position: str | None = None


class Learn(BaseModel):
    champion: str | None = None
    max_calls: int = 600
    matches_per_player: int = 20


class Review(BaseModel):
    session_id: str | None = None
    match_id: str | None = None


class Seek(BaseModel):
    t: float


class Hub:
    """연결된 화면들(폰, PC)에 메시지 뿌리기"""

    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def send(self, msg: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(json.dumps(msg, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def create_app(cfg: Config | None = None, llm: Llm | None = None, brain: Brain | None = None,
               dd: DataDragon | None = None, poll: bool = True, capture=None) -> FastAPI:
    cfg = cfg or Config.load()
    hub = Hub()
    brain = brain or Brain()
    dd = dd if dd is not None else DataDragon()
    holder = {"llm": llm}

    def get_llm() -> Llm:
        if holder["llm"] is None:
            holder["llm"] = ClaudeLlm(cfg)
        return holder["llm"]

    class LazyLlm:
        def stream(self, *a, **k):
            return get_llm().stream(*a, **k)

        async def json(self, *a, **k):
            return await get_llm().json(*a, **k)

    if capture is None:
        capture = Screen(cfg.monitor, cfg.minimap_fraction, cfg.minimap_side).capture
    coach = Coach(cfg, LazyLlm(), brain, dd, hub.send, capture=capture)
    status = {"in_game": False, "learning": False}

    async def poller():
        while True:
            try:
                data = await asyncio.to_thread(live_get, cfg.live_url, "/liveclientdata/allgamedata")
                st = state_mod.parse(data)
                if not status["in_game"]:
                    status["in_game"] = True
                    await hub.send({"type": "status", "in_game": True})
                await coach.on_state(st)
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError, json.JSONDecodeError):
                if status["in_game"]:
                    status["in_game"] = False
                    sid = coach.end_game()
                    await hub.send({"type": "status", "in_game": False, "ended_session": sid})
            except (ValueError, KeyError) as e:      # 로딩 화면, 관전 등
                log.debug("게임 데이터 해석 불가: %s", e)
            except Exception:
                log.exception("상태 처리 오류")
            await asyncio.sleep(cfg.poll_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if poll:
            async def load_dd():
                try:
                    await asyncio.to_thread(dd.load)
                except Exception as e:
                    log.warning("아이템 데이터(Data Dragon) 로딩 실패: %s", e)
            tasks = [asyncio.create_task(load_dd()), asyncio.create_task(poller())]
        yield
        for t in tasks:
            t.cancel()
        coach.end_game()

    app = FastAPI(title="롤 코치", lifespan=lifespan)
    app.state.coach = coach
    app.state.hub = hub

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        hub.clients.add(sock)
        await sock.send_text(json.dumps({"type": "status", "in_game": status["in_game"],
                                         "live": coach.live_enabled}, ensure_ascii=False))
        try:
            while True:
                msg = json.loads(await sock.receive_text())
                if msg.get("type") == "ask" and msg.get("q", "").strip():
                    asyncio.create_task(coach.ask(msg["q"].strip(), source=msg.get("source", "voice")))
        except (WebSocketDisconnect, json.JSONDecodeError):
            pass
        finally:
            hub.clients.discard(sock)


    @app.post("/api/ask")
    async def ask(body: Ask):
        return {"answer": await coach.ask(body.q, source="text")}


    @app.post("/api/live")
    async def live(body: Live):
        coach.live_enabled = body.enabled
        return {"live": coach.live_enabled}

    @app.get("/api/config")
    async def get_config():
        return {**cfg.public(), "status": status}

    @app.post("/api/config")
    async def set_config(body: dict):
        for k, v in body.items():
            if hasattr(cfg, k) and v not in (None, ""):
                cur = getattr(cfg, k)
                if isinstance(cur, list) and isinstance(v, str):
                    v = [x.strip() for x in v.splitlines() if x.strip()]
                elif isinstance(cur, bool):
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
                elif isinstance(cur, (int, float)) and not isinstance(cur, bool):
                    v = type(cur)(v)
                setattr(cfg, k, v)
        cfg.save()
        holder["llm"] = llm      # 키가 바뀌었을 수 있으니 다시 만들게
        return cfg.public()


    @app.post("/api/context")
    async def context(body: Context):
        return {"benchmark": coach.set_context(body.champion, body.position or None)}

    @app.get("/api/benchmark")
    async def get_benchmark(champion: str, position: str | None = None, vs: str | None = None):
        b = benchmark(brain, champion, position or None, top_n=cfg.top_players_per_champion,
                      min_games=cfg.min_games_on_champion, opp_champion=vs or None, dd=dd)
        return {"benchmark": b, "text": describe(b)}

    @app.get("/api/brain")
    async def brain_stats():
        return {**brain.stats(), "learning": status["learning"]}


    @app.post("/api/learn")
    async def learn(body: Learn):
        if status["learning"]:
            raise HTTPException(409, "이미 학습 중이에요")
        try:
            riot = Riot(cfg.riot_api_key, cfg.platform, cfg.region)
        except RiotError as e:
            raise HTTPException(400, str(e))
        loop = asyncio.get_running_loop()

        def logline(s: str):
            asyncio.run_coroutine_threadsafe(hub.send({"type": "learn_log", "text": s}), loop)

        async def run():
            status["learning"] = True
            try:
                if not dd.items:
                    await asyncio.to_thread(dd.load)
                r = await asyncio.to_thread(crawl, riot, brain, pro_accounts=cfg.pro_accounts,
                                            champion=body.champion or None, max_calls=body.max_calls,
                                            matches_per_player=body.matches_per_player, log=logline)
                await hub.send({"type": "learn_done", **r})
            except Exception as e:
                await hub.send({"type": "learn_log", "text": f"학습 중단: {e}"})
                await hub.send({"type": "learn_done", "error": str(e)})
            finally:
                status["learning"] = False

        asyncio.create_task(run())
        return {"started": True}

    @app.get("/api/sessions")
    async def sessions():
        return list_sessions(coach.sessions_dir)

    @app.get("/api/sessions/{sid}")
    async def session(sid: str):
        d = load_session(sid, coach.sessions_dir)
        if not d:
            raise HTTPException(404, "기록 없음")
        return d


    @app.get("/api/sessions/{sid}/img/{name}")
    async def session_img(sid: str, name: str):
        p = coach.sessions_dir / Path(sid).name / Path(name).name
        if not p.exists():
            raise HTTPException(404, "이미지 없음")
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/screen")
    async def screen_now():
        """설정 확인용: 지금 캡처되는 미니맵 영역"""
        shot = await coach.shot()
        if not shot:
            raise HTTPException(400, "화면 캡처가 꺼져 있거나 실패했어요")
        from fastapi import Response
        return Response(shot.minimap, media_type="image/jpeg")

    @app.post("/api/review")
    async def review(body: Review):
        try:
            if not dd.items:
                await asyncio.to_thread(dd.load)
            return await review_latest(cfg, coach.llm, brain, dd, body.session_id, body.match_id)
        except (ValueError, RiotError, RuntimeError) as e:
            raise HTTPException(400, str(e))


    @app.post("/api/replay/seek")
    async def seek(body: Seek):
        try:
            await asyncio.to_thread(seek_replay, cfg, body.t)
            return {"ok": True}
        except (urllib.error.URLError, OSError):
            raise HTTPException(400, "롤 리플레이가 열려 있지 않거나 Replay API가 꺼져 있어요 (README 참고)")

    return app
