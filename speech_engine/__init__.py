"""speech_engine — общий движок распознавания речи для проектов Егора.

Один пакет вместо пяти независимых копий (WhisperKey, thoughts-bot, transcribe-bot,
toki-bot, claude-tg-bot) — доводка каскада делается один раз здесь, а не переносится
руками в каждый проект. Устройство, профили и красные зоны — см. `agent.md` рядом
с этим файлом.

Быстрый старт (профиль dictation — WhisperKey/thoughts-bot):

    from speech_engine import Engine, DICTATION

    engine = Engine(profile=DICTATION, groq_api_key=..., deepgram_api_key=...)
    result = engine.recognize(audio_np_float32, duration_seconds)
    print(result.text, result.engine)

Профиль calls (transcribe-bot) НЕ имеет своего Engine — см. `calls.py`, почему.
"""
from .context import Context
from .engine import Engine
from .profiles import CALLS, DICTATION, Profile, get as get_profile
from .types import RecognitionResult, Word

__all__ = [
    "Engine", "Context", "Profile", "DICTATION", "CALLS", "get_profile",
    "RecognitionResult", "Word",
]

__version__ = "0.1.0"
