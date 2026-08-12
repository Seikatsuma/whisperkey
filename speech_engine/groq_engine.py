"""Groq whisper — вторая ступень dictation, единственная ступень calls.

Перенесено из WhisperKey `whisperkey.py` (`transcribe_cloud_turbo` → здесь
`transcribe_groq`, `_throttle`, `background_cloud_probe`, `cloud_status`).
429/5xx лечатся повтором с уважением к `retry-after` — без этого на 30-минутной
записи 32 куска в 4 потока без пауз ловили 429 и теряли куски текста насовсем
(см. agent.md WhisperKey и transcribe_core.py, `RateLimiter`).
"""
from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass, field

import requests

from .audio import create_audio_wav, restore_timeline
from .density_gate import text_from_response
from .profiles import Profile

logger = logging.getLogger("speech_engine.groq")

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


@dataclass
class CloudState:
    """Состояние облачной ступени. Одно на процесс/Engine, не модульная глобальная."""
    is_blocked: bool = False
    last_check_time: float = 0.0
    check_in_progress: bool = False
    consecutive_success: int = 0
    last_degenerate: list = field(default_factory=list)
    last_api_text: str = ""


class Throttle:
    """Разносит запросы во времени — один инстанс на процесс/Engine."""
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_ts = 0.0

    def wait(self) -> None:
        with self._lock:
            delta = time.time() - self._last_ts
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last_ts = time.time()


def background_cloud_probe(*, api_key: str, state: CloudState, session: requests.Session) -> None:
    """Фоновая проверка доступности Groq. Не блокирует вызывающий поток."""
    if state.check_in_progress:
        return

    def probe():
        state.check_in_progress = True
        try:
            headers = {"Authorization": f"Bearer {api_key}", "User-Agent": _USER_AGENT}
            response = session.get(GROQ_MODELS_URL, headers=headers, timeout=5)
            if response.status_code == 200:
                state.consecutive_success += 1
                if state.consecutive_success >= 1:
                    if state.is_blocked:
                        logger.info("groq: связь стабильна, возвращаю облако")
                    state.is_blocked = False
            else:
                # ВИДИМОСТЬ ПОПЫТКИ — до этой строки провал проверки не оставлял
                # ни следа в логе: пользователь видел только "local" раз за разом
                # и не мог отличить "код не пытается" от "пытается и падает"
                # (живой случай Егора 11.08.26, лог передан текстом, диагностировать
                # без этой строки было нечем). Раньше здесь просто молча
                # ставился is_blocked=True.
                logger.info("groq: проверка связи — статус %s, ещё заблокирован",
                           response.status_code)
                state.is_blocked = True
                state.consecutive_success = 0
        except Exception as e:
            logger.info("groq: проверка связи — %s, ещё заблокирован", type(e).__name__)
            state.is_blocked = True
            state.consecutive_success = 0
        finally:
            state.last_check_time = time.time()
            state.check_in_progress = False

    threading.Thread(target=probe, daemon=True).start()


def maybe_probe_if_blocked(*, api_key: str, state: CloudState, session: requests.Session) -> None:
    """Даёт заблокированному Groq шанс раскрыться сам, независимо от того, решил ли
    вызывающий код вообще звать transcribe_groq в этом цикле.

    Без этой функции периодическая проверка (`since > 60` внутри transcribe_groq)
    была мёртвым кодом для каскада dictation.py: там сам вызов transcribe_groq
    защищён условием `not state.is_blocked` (см. dictation.py `use_cloud` /
    `parallel_ok` / `style_future`) — то есть ровно пока is_blocked=True, функция
    не вызывается вовсе, и написанная внутри неё проверка никогда не выполняется.
    Живой инцидент 12.08.26: Groq поймал 403 один раз в начале сессии и молча
    простоял заблокированным до самого перезапуска WhisperKey — фоновая проверка
    ни разу не запустилась за час диктовок.
    """
    if not state.is_blocked or not api_key:
        return
    if time.time() - state.last_check_time > 60:
        background_cloud_probe(api_key=api_key, state=state, session=session)


def transcribe_groq(audio_data, *, api_key: str, profile: Profile, sample_rate: int,
                    state: CloudState, throttle: Throttle, session: requests.Session,
                    allow_retry: bool = True, use_prompt: bool = True,
                    return_words: bool = False, prompt_override: str = ""):
    """Расшифровка через Groq whisper. Возвращает текст (и, при return_words=True,
    список слов с таймкодами — они нужны для склейки перекрытий по времени)."""
    empty = (None, []) if return_words else None

    if state.is_blocked:
        since = time.time() - state.last_check_time
        if since > 60:
            logger.info("groq: заблокирован, запускаю фоновую проверку связи")
            background_cloud_probe(api_key=api_key, state=state, session=session)
        else:
            logger.info("groq: заблокирован, до следующей проверки %.0fс — ухожу на local",
                       60 - since)
        return empty

    if not api_key:
        return empty

    wav_data = create_audio_wav(audio_data, sample_rate, tempo=profile.asr_tempo)
    if not wav_data:
        return empty

    headers = {
        'Authorization': f'Bearer {api_key}',
        'User-Agent': _USER_AGENT,
        'Accept': 'application/json',
    }
    files = {'file': ('audio.wav', io.BytesIO(wav_data), 'audio/wav')}
    data = [
        ('model', profile.groq_model),
        ('language', profile.groq_language),
        ('temperature', str(profile.groq_temperature)),
        ('response_format', 'verbose_json'),
        ('timestamp_granularities[]', 'segment'),
        ('timestamp_granularities[]', 'word'),
    ]
    prompt = prompt_override or (profile.groq_context_prompt if use_prompt else "")
    if prompt:
        data.append(('prompt', prompt))

    for attempt in range(4):
        try:
            throttle.wait()
            response = session.post(GROQ_TRANSCRIBE_URL, headers=headers, files=files,
                                    data=data, timeout=60)

            if response.status_code == 200:
                result = response.json()
                restore_timeline(result, profile.asr_tempo)
                if not prompt:
                    state.last_api_text = (result.get('text') or '').strip()
                text, degenerate = text_from_response(
                    result, profile.density_gate_min_duration, profile.density_gate_min_words_per_sec)
                state.last_degenerate = degenerate
                if degenerate:
                    total = sum(d['dur'] for d in degenerate)
                    logger.info("groq: сорванных окон %d, суммарно %.1fс", len(degenerate), total)
                if return_words:
                    return text, (result.get('words') or [])
                return text

            if response.status_code == 403:
                logger.warning("groq: 403 (гео-блок). Ухожу на следующую ступень.")
                state.is_blocked = True
                state.last_check_time = time.time()
                background_cloud_probe(api_key=api_key, state=state, session=session)
                return empty

            if response.status_code in (429, 500, 502, 503, 504) and allow_retry and attempt < 3:
                wait = float(response.headers.get('retry-after', 0) or 0) or (2 ** attempt) * 2
                wait = min(wait, 20.0)
                logger.info("groq: %s, повтор через %.1fс (попытка %d из 4)",
                           response.status_code, wait, attempt + 2)
                time.sleep(wait)
                files = {'file': ('audio.wav', io.BytesIO(wav_data), 'audio/wav')}
                continue

            logger.warning("groq: статус %s, отказ", response.status_code)
            return empty

        except Exception as e:
            logger.warning("groq: исключение %s", type(e).__name__)
            if allow_retry and attempt < 3:
                time.sleep(2 ** attempt)
                files = {'file': ('audio.wav', io.BytesIO(wav_data), 'audio/wav')}
                continue
            return empty

    return empty
