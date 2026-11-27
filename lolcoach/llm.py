"""코치 두뇌 = Claude. 스트리밍으로 받아서 문장 단위로 바로 음성으로 읽게 한다."""
from __future__ import annotations

import json as _json
from typing import AsyncIterator, Protocol

import anthropic

from .config import Config


class Llm(Protocol):
    def stream(self, system: list[dict], messages: list[dict], effort: str,
               max_tokens: int = 2000) -> AsyncIterator[str]: ...

    async def json(self, system: list[dict], messages: list[dict], schema: dict, effort: str) -> dict: ...


class ClaudeLlm:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key or None)

    async def stream(self, system: list[dict], messages: list[dict], effort: str,
                     max_tokens: int = 2000) -> AsyncIterator[str]:
        try:
            async with self.client.beta.messages.stream(
                model=self.cfg.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config={"effort": effort},
                # 안전 필터가 드물게 답을 거절해도 다른 모델이 이어서 답하게 함
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as s:
                async for text in s.text_stream:
                    yield text
                final = await s.get_final_message()
                if final.stop_reason == "refusal":
                    yield "이 질문에는 답할 수 없어요."
        except anthropic.AuthenticationError:
            yield "Claude API 키가 없거나 틀렸어요. 설정에서 확인해 주세요."
        except anthropic.RateLimitError:
            yield "AI 사용량 한도에 걸렸어요. 잠시 후 다시 물어봐 주세요."
        except anthropic.APIStatusError as e:
            yield f"AI 서버 오류({e.status_code})가 났어요."
        except anthropic.APIConnectionError:
            yield "AI 서버에 연결할 수 없어요. 인터넷을 확인해 주세요."
        except TypeError as e:
            if "authentication" in str(e):      # 키가 아예 없음
                yield "Claude API 키가 없어요. 설정 탭에서 넣어 주세요."
            else:
                raise


    async def json(self, system: list[dict], messages: list[dict], schema: dict, effort: str) -> dict:
        """구조화 출력: 스키마에 맞는 JSON을 보장받음 (경기 후 리뷰용, 스트리밍으로 타임아웃 방지)"""
        async with self.client.beta.messages.stream(
            model=self.cfg.model,
            max_tokens=32000,
            system=system,
            messages=messages,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as s:
            final = await s.get_final_message()
        if final.stop_reason == "refusal":
            raise RuntimeError("AI가 리뷰 생성을 거절했어요")
        if final.stop_reason == "max_tokens":
            raise RuntimeError("리뷰가 너무 길어서 잘렸어요")
        text = next(b.text for b in final.content if b.type == "text")
        return _json.loads(text)


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """스트리밍 버퍼에서 끝난 문장들만 떼어냄 (TTS가 문장별로 바로 읽도록)"""
    out, start = [], 0
    for i, ch in enumerate(buffer):
        if ch == "\n" or (ch in ".!?。" and i + 1 < len(buffer) and buffer[i + 1] in " \n"):
            seg = buffer[start:i + 1].strip()
            if seg:
                out.append(seg)
            start = i + 1
    return out, buffer[start:]
