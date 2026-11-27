"""내 화면 보기: 게임 화면 전체(축소) + 미니맵(원본 해상도 확대)을 캡처한다.

라이엇 API가 안 주는 것 — 미니언 웨이브 위치, 미니맵에 보이는 적 정글, 와드, 내 체력/마나/스킬 쿨 —
을 AI가 화면에서 직접 읽게 하기 위함. 보이는 화면만 찍으므로 플레이어가 보는 것 이상은 없다.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

log = logging.getLogger("lolcoach")


@dataclass
class Shot:
    full: bytes      # JPEG, 가로 1280으로 축소한 전체 화면
    minimap: bytes   # JPEG, 오른쪽 아래 미니맵 영역 (축소 안 함)


class Screen:
    def __init__(self, monitor: int = 1, minimap_fraction: float = 0.28, minimap_side: str = "right"):
        self.monitor = monitor
        self.frac = minimap_fraction
        self.side = minimap_side
        self._warned = False

    def capture(self) -> Shot | None:
        try:
            import mss
            from PIL import Image
            with mss.mss() as sct:
                mon = sct.monitors[min(self.monitor, len(sct.monitors) - 1)]
                raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception as e:      # 화면 없는 환경, 권한 문제 등
            if not self._warned:
                log.warning("화면 캡처 실패 (화면 없이 계속): %s", e)
                self._warned = True
            return None
        return make_shot(img, self.frac, self.side)


def make_shot(img, frac: float = 0.28, side: str = "right") -> Shot:
    w, h = img.size
    s = int(h * frac)               # 미니맵은 정사각형, 크기 설정 최대치까지 덮도록 여유 있게
    left = w - s if side == "right" else 0
    mini = img.crop((left, h - s, left + s, h))
    full = img.copy()
    if w > 1280:
        full = full.resize((1280, round(h * 1280 / w)))
    return Shot(full=_jpg(full, 70), minimap=_jpg(mini, 85))


def _jpg(img, q: int) -> bytes:
    b = io.BytesIO()
    img.save(b, "JPEG", quality=q)
    return b.getvalue()
