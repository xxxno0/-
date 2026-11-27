"""실시간 코치: 게임 상태 → 순간 감지 → (필요하면 Claude 판단) → 음성 콜. 질문/대답도 여기서.

모든 콜·질문·답변·상태는 세션 파일에 기록돼서 경기 후 '판단 리플레이'로 다시 볼 수 있다.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from .config import DATA_DIR, Config
from .knowledge import ANSWER_RULES, PLAYBOOK
from .learn import Brain, benchmark, describe
from .llm import Llm, split_sentences
from .riot import DataDragon
from .screen import Shot
from .state import GameState, fmt_time
from .triggers import Detector, Moment

Broadcast = Callable[[dict], Awaitable[None]]
SESSIONS = DATA_DIR / "sessions"


class Recorder:
    """한 게임의 기록 (판단 리플레이용)"""

    def __init__(self, champion: str, opponent: str | None, folder: Path = SESSIONS):
        folder.mkdir(parents=True, exist_ok=True)
        self.id = time.strftime("%Y%m%d-%H%M%S")
        self.path = folder / f"{self.id}.json"
        self.data = {"id": self.id, "started": time.time(), "champion": champion, "opponent": opponent,
                     "snapshots": [], "calls": [], "qa": []}
        self._last_snap = -999.0
        self.img_dir = folder / self.id

    def image(self, t: float, kind: str, shot: Shot | None) -> str | None:
        """그 순간의 내 화면 저장 (리플레이에서 보여줌)"""
        if not shot:
            return None
        self.img_dir.mkdir(exist_ok=True)
        name = f"{int(t):05d}-{kind}.jpg"
        (self.img_dir / name).write_bytes(shot.full)
        return name

    def snapshot(self, st: GameState) -> None:
        if st.time - self._last_snap < 10:
            return
        self._last_snap = st.time
        opp = st.opponent
        self.data["snapshots"].append({
            "t": round(st.time), "gold": int(st.gold), "cs": st.me.cs, "lvl": st.me.level,
            "kda": [st.me.kills, st.me.deaths, st.me.assists], "dead": st.me.is_dead,
            "opp": {"lvl": opp.level, "cs": opp.cs, "kda": [opp.kills, opp.deaths, opp.assists]} if opp else None,
        })

    def call(self, t: float, kind: str, text: str, img: str | None = None) -> None:
        self.data["calls"].append({"t": round(t), "kind": kind, "text": text, "img": img})

    def qa(self, t: float, q: str, a: str, img: str | None = None) -> None:
        self.data["qa"].append({"t": round(t), "q": q, "a": a, "img": img})

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")


def list_sessions(folder: Path = SESSIONS) -> list[dict]:
    if not folder.exists():
        return []
    out = []
    for p in sorted(folder.glob("*.json"), reverse=True)[:50]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": d["id"], "champion": d.get("champion"), "opponent": d.get("opponent"),
                        "calls": len(d.get("calls", [])), "qa": len(d.get("qa", []))})
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def load_session(sid: str, folder: Path = SESSIONS) -> dict | None:
    p = folder / f"{Path(sid).name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


class Coach:
    def __init__(self, cfg: Config, llm: Llm, brain: Brain, dd: DataDragon | None, broadcast: Broadcast,
                 sessions_dir: Path = SESSIONS, capture: Callable[[], Shot | None] | None = None):
        self.cfg, self.llm, self.brain, self.dd, self.broadcast = cfg, llm, brain, dd, broadcast
        self.sessions_dir = sessions_dir
        self.state: GameState | None = None
        self.detector: Detector | None = None
        self.recorder: Recorder | None = None
        self.bench: dict = {}
        self.bench_text = ""
        self.history: list[dict] = []     # 최근 질문/답변 (이어서 묻기)
        self.live_enabled = True          # 실시간 음성 콜 on/off
        self.last_spoken = -999.0
        self.manual_champion: tuple[str, str | None] | None = None
        self._tasks: set[asyncio.Task] = set()
        self.capture = capture

    async def shot(self) -> Shot | None:
        if not self.capture or not self.cfg.screen_enabled:
            return None
        return await asyncio.to_thread(self.capture)

    # ------------------------------------------------------------ 게임 상태

    async def on_state(self, st: GameState) -> None:
        new_game = (self.state is None or self.detector is None or st.time + 5 < self.state.time
                    or st.me.champion != self.state.me.champion)
        if new_game:
            self._start_game(st)
        self.state = st
        self.recorder.snapshot(st)
        for m in self.detector.update(st):
            await self._moment(m)
        await self.broadcast({"type": "state", "state": st.summary()})

    def _start_game(self, st: GameState) -> None:
        if self.recorder:
            self.recorder.save()
        opp = st.opponent
        self.load_benchmark(st.me.champion, st.me.position or None, opp.champion if opp else None)
        self.detector = Detector(self.bench, self.dd)
        self.recorder = Recorder(st.me.champion, opp.champion if opp else None, self.sessions_dir)
        self.history = []

    def end_game(self) -> str | None:
        if not self.recorder:
            return None
        self.recorder.save()
        sid = self.recorder.id
        self.state = self.detector = self.recorder = None
        return sid

    def load_benchmark(self, champion: str, position: str | None, opp: str | None = None) -> None:
        self.bench = benchmark(self.brain, champion, position, top_n=self.cfg.top_players_per_champion,
                               min_games=self.cfg.min_games_on_champion, opp_champion=opp, dd=self.dd)
        self.bench_text = describe(self.bench)

    def set_context(self, champion: str, position: str | None) -> str:
        """게임 밖에서 질문할 때 어떤 챔피언 기준으로 답할지"""
        self.manual_champion = (champion, position)
        self.load_benchmark(champion, position)
        return self.bench_text

    async def _moment(self, m: Moment) -> None:
        if self.recorder:
            # AI 판단 순간은 ask()에서 찍은 화면이 저장되므로 여기선 일반 콜만 찍음
            img = None if m.llm else self.recorder.image(m.time, m.kind, await self.shot())
            self.recorder.call(m.time, m.kind, m.text, img)
        await self.broadcast({"type": "moment", "kind": m.kind, "t": fmt_time(m.time), "text": m.text,
                              "priority": m.priority})
        if not self.live_enabled:
            return
        now = time.monotonic()
        if m.priority >= 3 and now - self.last_spoken < self.cfg.min_gap_between_calls:
            return      # 참고용 콜은 직전에 말했으면 생략
        if m.llm:
            self._spawn(self.ask(m.question, source=m.kind, speak_prefix=m.text))
        else:
            self.last_spoken = now
            await self.broadcast({"type": "say", "text": m.text})

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ 질문/대답

    def system_prompt(self) -> list[dict]:
        blocks = [{"type": "text", "text": PLAYBOOK + "\n" + ANSWER_RULES}]
        if self.bench_text:
            blocks.append({"type": "text", "text": "[챌린저·프로 학습 데이터]\n" + self.bench_text})
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    def _context(self) -> str:
        st = self.state
        if not st:
            if self.manual_champion:
                return f"(지금은 게임 밖. 기준 챔피언: {self.manual_champion[0]})"
            return "(지금은 게임 밖. 게임 데이터 없음)"
        recent = [e for e in st.events if st.time - float(e.get("EventTime", 0)) <= 90
                  and e.get("EventName") not in ("GameStart", "MinionsSpawning")]
        ev = "; ".join(f"{fmt_time(float(e['EventTime']))} {e.get('EventName')} "
                       f"{e.get('KillerName', '')}→{e.get('VictimName', '')}".strip() for e in recent[-8:])
        return (f"[현재 게임 상태]\n{json.dumps(st.summary(), ensure_ascii=False)}\n"
                f"[최근 90초 이벤트] {ev or '없음'}")

    async def ask(self, question: str, source: str = "voice", speak_prefix: str = "") -> str:
        qid = uuid.uuid4().hex[:8]
        await self.broadcast({"type": "answer_start", "id": qid, "q": question, "source": source})
        if speak_prefix:
            await self.broadcast({"type": "say", "text": speak_prefix, "id": qid})
        shot = await self.shot() if self.state else None
        text = f"{self._context()}\n\n[질문] {question}"
        if shot:
            text = "[지금 내 화면: 첫 이미지 전체 화면, 둘째 이미지 미니맵 확대]\n" + text
            content: str | list = [_img(shot.full), _img(shot.minimap), {"type": "text", "text": text}]
        else:
            content = text
        messages = [*self.history[-8:], {"role": "user", "content": content}]
        buf, full = "", ""
        async for chunk in self.llm.stream(self.system_prompt(), messages, self.cfg.live_effort, max_tokens=4000):
            buf += chunk
            full += chunk
            done, buf = split_sentences(buf)
            for s in done:
                await self.broadcast({"type": "say", "text": s, "id": qid})
            await self.broadcast({"type": "answer_delta", "id": qid, "text": chunk})
        if buf.strip():
            await self.broadcast({"type": "say", "text": buf.strip(), "id": qid})
        self.last_spoken = time.monotonic()
        full = full.strip()
        await self.broadcast({"type": "answer_end", "id": qid, "text": full})
        # 다음 질문 때 이어지도록 질문만 짧게 기록 (상태 JSON은 매번 새로 넣음)
        self.history += [{"role": "user", "content": question}, {"role": "assistant", "content": full or "-"}]
        if self.recorder:
            t = self.state.time if self.state else 0
            self.recorder.qa(t, question, full, self.recorder.image(t, "qa", shot))
            self.recorder.save()
        return full


def _img(jpg: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpg).decode()}}
