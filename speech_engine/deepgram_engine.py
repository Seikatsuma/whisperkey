"""Deepgram — первая ступень каскада dictation. Перенесено из WhisperKey `whisperkey.py`
(`transcribe_deepgram`). Профиль calls Deepgram не использует вовсе (решение Егора
09.08.26 — см. agent.md, «Профиль calls»); эта ступень включается только когда
`Profile.deepgram_enabled=True` и в вызывающем коде передан `deepgram_api_key`.

Отказы разделены по лечимости (см. `~/whisperkey/Whisper на MAC/agent.md`):
401/402/403 (ключ/деньги/доступ) не лечатся повтором — движок блокируется на
`deepgram_block_seconds`; 429/5xx — короткий повтор; пустой транскрипт и битый JSON —
не ошибка, а сигнал уйти на следующую ступень.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

from .audio import create_audio_wav
from .profiles import Profile

logger = logging.getLogger("speech_engine.deepgram")

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


@dataclass
class DeepgramState:
    """Состояние первой ступени. Одно на процесс/Engine — НЕ модульная глобальная
    переменная, чтобы несколько ботов в одном сервере не путали состояния друг друга."""
    blocked_until: float = 0.0
    last_reason: str = ""
    last_seconds: float = 0.0


def transcribe_deepgram(audio_data, *, api_key: str, profile: Profile, sample_rate: int,
                        state: DeepgramState, session: requests.Session | None = None) -> str | None:
    """Первая ступень каскада. Возвращает текст либо None — тогда работает следующая ступень.

    Возврат None здесь никогда не означает потерю диктовки: вызывающий код должен
    молча уйти на следующую ступень.
    """
    if not api_key:
        return None
    if time.time() < state.blocked_until:
        return None

    wav_data = create_audio_wav(audio_data, sample_rate, tempo=profile.asr_tempo)
    if not wav_data:
        return None

    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/wav",
    }
    http = session or requests

    t0 = time.time()
    for attempt in range(profile.deepgram_retries):
        try:
            response = http.post(
                DEEPGRAM_URL, params=profile.deepgram_params, headers=headers,
                data=wav_data, timeout=profile.deepgram_timeout,
            )

            if response.status_code == 200:
                try:
                    alt = response.json()["results"]["channels"][0]["alternatives"][0]
                except (KeyError, IndexError, ValueError) as e:
                    logger.info("deepgram: ответ не разобран (%s) — ухожу дальше по каскаду",
                                type(e).__name__)
                    return None
                text = (alt.get("transcript") or "").strip()
                state.last_seconds = time.time() - t0
                if not text:
                    logger.info("deepgram: пусто — ухожу дальше по каскаду")
                    return None
                return text

            if response.status_code in (401, 402, 403):
                reason = {401: "ключ не принят", 402: "кончились деньги на счёте",
                          403: "доступ запрещён"}[response.status_code]
                state.blocked_until = time.time() + profile.deepgram_block_seconds
                state.last_reason = reason
                mins = int(profile.deepgram_block_seconds / 60)
                logger.warning("deepgram: %s (HTTP %s). Перехожу на следующую ступень, "
                               "повторю попытку через %d мин.", reason, response.status_code, mins)
                return None

            if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < profile.deepgram_retries:
                time.sleep(0.5)
                continue

            logger.info("deepgram: статус %s — ухожу дальше по каскаду", response.status_code)
            return None

        except requests.Timeout:
            logger.info("deepgram: не ответил за %.0fс — ухожу дальше по каскаду",
                        profile.deepgram_timeout)
            return None
        except Exception as e:
            if attempt + 1 < profile.deepgram_retries:
                time.sleep(0.5)
                continue
            logger.info("deepgram: %s — ухожу дальше по каскаду", type(e).__name__)
            return None

    return None
