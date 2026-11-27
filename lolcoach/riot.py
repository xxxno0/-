"""라이엇 공식 API + Data Dragon 클라이언트 (표준 라이브러리만 사용).

개발 키 한도: 초당 20회, 2분에 100회. 429가 오면 Retry-After 만큼 기다렸다 재시도.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque


class RiotError(Exception):
    pass


class RateLimiter:
    def __init__(self, limits=((20, 1.0), (100, 120.0))):
        self.limits = limits
        self.calls: deque[float] = deque()

    def wait(self) -> None:
        while True:
            now = time.monotonic()
            longest = max(w for _, w in self.limits)
            while self.calls and now - self.calls[0] > longest:
                self.calls.popleft()
            sleep = 0.0
            for n, window in self.limits:
                recent = [t for t in self.calls if now - t < window]
                if len(recent) >= n:
                    sleep = max(sleep, window - (now - recent[0]) + 0.05)
            if sleep <= 0:
                self.calls.append(now)
                return
            time.sleep(sleep)


def _get(url: str, headers: dict | None = None, timeout: float = 20.0, ctx: ssl.SSLContext | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": "lolcoach", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


class Riot:
    def __init__(self, api_key: str, platform: str = "kr", region: str = "asia"):
        if not api_key:
            raise RiotError("라이엇 API 키가 없습니다 (설정에서 riot_api_key)")
        self.key = api_key
        self.platform = f"https://{platform}.api.riotgames.com"
        self.region = f"https://{region}.api.riotgames.com"
        self.limiter = RateLimiter()

    def _call(self, url: str, retries: int = 4):
        for attempt in range(retries):
            self.limiter.wait()
            try:
                return _get(url, {"X-Riot-Token": self.key})
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(float(e.headers.get("Retry-After", 10)))
                    continue
                if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                if e.code == 404:
                    return None
                if e.code in (401, 403):
                    raise RiotError("라이엇 API 키가 틀렸거나 만료됨 (개발 키는 24시간마다 재발급)") from e
                raise RiotError(f"라이엇 API 오류 {e.code}: {url}") from e
            except urllib.error.URLError as e:
                if attempt == retries - 1:
                    raise RiotError(f"라이엇 API 연결 실패: {e.reason}") from e
                time.sleep(2 ** attempt)
        raise RiotError("라이엇 API 한도 초과가 계속됨")

    # --- 계정 ---
    def account(self, riot_id: str) -> dict | None:
        name, _, tag = riot_id.partition("#")
        if not tag:
            raise RiotError("라이엇 ID는 '닉네임#태그' 형식이어야 합니다")
        q = f"{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}"
        return self._call(f"{self.region}/riot/account/v1/accounts/by-riot-id/{q}")

    # --- 랭킹 ---
    def apex_league(self, tier: str) -> list[dict]:
        """tier: challenger | grandmaster | master. 각 entry에 puuid, leaguePoints, wins, losses"""
        d = self._call(f"{self.platform}/lol/league/v4/{tier}leagues/by-queue/RANKED_SOLO_5x5")
        return (d or {}).get("entries", [])

    # --- 경기 ---
    def match_ids(self, puuid: str, count: int = 100, start: int = 0, queue: int = 420) -> list[str]:
        q = urllib.parse.urlencode({"queue": queue, "count": count, "start": start})
        return self._call(f"{self.region}/lol/match/v5/matches/by-puuid/{puuid}/ids?{q}") or []

    def match(self, match_id: str) -> dict | None:
        return self._call(f"{self.region}/lol/match/v5/matches/{match_id}")

    def timeline(self, match_id: str) -> dict | None:
        return self._call(f"{self.region}/lol/match/v5/matches/{match_id}/timeline")


class DataDragon:
    """아이템 가격/이름 (한국어). 버전마다 캐시."""

    def __init__(self):
        self.version: str | None = None
        self.items: dict[int, dict] = {}

    def load(self) -> None:
        if self.items:
            return
        self.version = _get("https://ddragon.leagueoflegends.com/api/versions.json")[0]
        raw = _get(f"https://ddragon.leagueoflegends.com/cdn/{self.version}/data/ko_KR/item.json")
        self.items = {int(k): v for k, v in raw["data"].items()}

    def name(self, item_id: int) -> str:
        return self.items.get(item_id, {}).get("name", str(item_id))

    def cost(self, item_id: int) -> int:
        return int(self.items.get(item_id, {}).get("gold", {}).get("total", 0))

    def is_legendary(self, item_id: int) -> bool:
        """완성 아이템: 상위 아이템이 없고 가격이 2000 이상"""
        it = self.items.get(item_id, {})
        return not it.get("into") and self.cost(item_id) >= 2000


def live_get(base: str, path: str, timeout: float = 1.5):
    """게임 클라이언트의 로컬 API (자체 서명 인증서라 검증 생략, 127.0.0.1 전용)"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return _get(base.rstrip("/") + path, timeout=timeout, ctx=ctx)


def live_post(base: str, path: str, body: dict, timeout: float = 2.0):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8") or "{}")
