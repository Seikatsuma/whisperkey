"""Публичный вход пакета для профиля dictation: `Engine`.

`Engine` держит ключи, http-сессию и состояние ступеней (см. `context.Context`) и
даёт один метод — `recognize(audio, dur) -> RecognitionResult`. Это то, что
WhisperKey/thoughts-bot зовут из своего `process_audio`/`transcribe()`.

Профиль calls НЕ имеет своего `Engine` — см. `calls.py`, почему (интеграция идёт
поверх существующего конвейера transcribe-bot, не поверх нового каскада).
"""
from __future__ import annotations

import numpy as np
import requests

from . import dictation
from .context import Context
from .profiles import DICTATION, Profile
from .types import RecognitionResult


class Engine:
    def __init__(self, *, profile: Profile = DICTATION, groq_api_key: str = "",
                deepgram_api_key: str = "", sample_rate: int = 16000,
                local_model=None, session: requests.Session | None = None):
        self.ctx = Context(
            profile=profile, sample_rate=sample_rate, groq_api_key=groq_api_key,
            deepgram_api_key=deepgram_api_key, local_model=local_model,
            session=session or requests.Session(),
        )

    def recognize(self, audio: np.ndarray, dur: float) -> RecognitionResult:
        if self.ctx.profile.name != "dictation":
            raise ValueError(
                f"Engine.recognize реализован для профиля dictation, получен {self.ctx.profile.name!r}. "
                "Профиль calls встраивается как библиотека в существующий конвейер — см. calls.py.")
        return dictation.recognize(audio, dur, self.ctx)
