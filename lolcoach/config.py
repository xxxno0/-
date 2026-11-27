"""설정: config.json (없으면 기본값) + 환경변수.

키는 파일에만 저장되고 깃에는 올라가지 않음 (.gitignore).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DATA_DIR = Path(os.environ.get("LOLCOACH_DATA", Path.home() / ".lolcoach"))
CONFIG_PATH = DATA_DIR / "config.json"


@dataclass
class Config:
    # Claude (코치 두뇌). 비워두면 ANTHROPIC_API_KEY 환경변수 / `ant auth login` 사용
    anthropic_api_key: str = ""
    model: str = "claude-opus-5-5"
    live_effort: str = "low"        # 게임 중 답변: 속도 우선
    review_effort: str = "high"     # 경기 후 리뷰: 깊이 우선

    # 라이엇 API (https://developer.riotgames.com 에서 발급, 개발키는 24시간마다 갱신)
    riot_api_key: str = ""
    platform: str = "kr"            # kr, jp1, na1, euw1 ...
    region: str = "asia"            # asia, americas, europe, sea
    my_riot_id: str = ""            # 예: "닉네임#KR1" (경기 후 리뷰용)

    # 학습 대상: 이 계정들은 '프로'로 취급해 가중치를 더 줌 (예: "Hide on bush#KR1")
    pro_accounts: list[str] = field(default_factory=list)
    top_players_per_champion: int = 5   # 챔피언마다 정점 플레이어 몇 명을 합의에 쓸지
    min_games_on_champion: int = 5      # 그 챔피언을 최소 몇 판 한 사람만 '장인'으로 인정

    # 내 화면 보기 (미니맵의 웨이브·적 정글·와드, 체력/쿨타임을 AI가 직접 읽음)
    screen_enabled: bool = True
    monitor: int = 1                    # 롤이 떠 있는 모니터 번호 (1=주 모니터)
    minimap_fraction: float = 0.28      # 화면 높이 대비 미니맵 영역 크기
    minimap_side: str = "right"         # 미니맵 위치 (롤 설정에서 왼쪽으로 옮겼으면 "left")

    # 서버
    host: str = "0.0.0.0"
    port: int = 8443
    https: bool = True              # 폰 브라우저에서 마이크를 쓰려면 HTTPS 필요
    live_url: str = "https://127.0.0.1:2999"
    poll_seconds: float = 1.0
    min_gap_between_calls: float = 10.0  # 음성 콜 사이 최소 간격(초)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in raw.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        env = {
            "anthropic_api_key": "ANTHROPIC_API_KEY",
            "riot_api_key": "RIOT_API_KEY",
        }
        for attr, var in env.items():
            if not getattr(cfg, attr) and os.environ.get(var):
                setattr(cfg, attr, os.environ[var])
        return cfg

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    def public(self) -> dict:
        """화면에 보여줄 때 키는 가림"""
        d = asdict(self)
        for k in ("anthropic_api_key", "riot_api_key"):
            d[k] = "설정됨" if d[k] else ""
        return d
