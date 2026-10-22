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
import threading
import time
from dataclasses import dataclass, field

import requests

from .audio import create_upload_audio
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
    # True, когда причина блокировки — сеть недоступна (таймаут/ConnectionError),
    # а не отказ ключа. Такие блоки короткие (NETWORK_BLOCK_SECONDS) и их можно
    # снимать пробой — см. maybe_probe_deepgram_if_blocked. Блок по 401/402/403
    # пробой не лечится: ключ не починится за минуту.
    blocked_by_network: bool = False
    check_in_progress: bool = False   # фоновая проба уже летит — не плодить потоки
    # Конец последнего слова последнего ответа, уже в секундах исходной записи
    # (темп 1.03 развёрнут обратно). 0 — слов не было / запроса не было. Нужно
    # детектору «речь в хвосте записи не распознана» в dictation.
    last_word_end: float = 0.0


# Сетевой блок держим коротким: домашний VPN мог включиться в любой момент,
# и сидеть 15 минут без первой ступени из-за одного разрыва незачем.
NETWORK_BLOCK_SECONDS = 120.0

_DEEPGRAM_PROBE_URL = "https://api.deepgram.com/"


def maybe_probe_deepgram_if_blocked(*, state: DeepgramState,
                                    session: requests.Session | None = None) -> None:
    """Проверяет, не вернулась ли сеть, пока ступень заблокирована сетевой причиной.

    Аналог `groq_engine.maybe_probe_if_blocked` для первой ступени. Разница:
    пробуем ТОЛЬКО сетевые блоки — блок по 401/402/403 живёт свой таймер целиком
    (проба ключ не лечит, а запросы жгут). Проба — просто HEAD на хост: достаточно
    факта, что TCP+TLS поднимаются; ответ любой (даже 404) = сеть есть.

    Проба ФОНОВАЯ (как у groq): синхронная версия клала до 3с внутрь
    recognize() на каждой диктовке раз в ~30с, пока сеть не вернётся —
    жалоба Егора 19.10.26 «транскрибация стала длиннее». Диктовка пробу
    не ждёт: ступень разблокируется к следующей записи.
    """
    if not state.blocked_by_network or time.time() < state.blocked_until:
        return
    if state.check_in_progress:
        return
    state.check_in_progress = True

    def _probe():
        try:
            (session or requests).head(_DEEPGRAM_PROBE_URL, timeout=3.0)
            state.blocked_until = 0.0
            state.blocked_by_network = False
            state.last_reason = ""
            logger.info("deepgram: связь вернулась, ступень разблокирована")
        except Exception:
            # Тихо ждём следующего срока — запись состояния уже стоит.
            state.blocked_until = time.time() + 30.0
        finally:
            state.check_in_progress = False

    threading.Thread(target=_probe, daemon=True).start()


def transcribe_deepgram(audio_data, *, api_key: str, profile: Profile, sample_rate: int,
                        state: DeepgramState, session: requests.Session | None = None,
                        upload: tuple[bytes, str] | None = None) -> str | None:
    """Первая ступень каскада. Возвращает текст либо None — тогда работает следующая ступень.

    Возврат None здесь никогда не означает потерю диктовки: вызывающий код должен
    молча уйти на следующую ступень.

    `upload=(байты, mime)` — готовый файл как есть (webm/ogg/mp4 из браузера):
    Deepgram сам разбирает контейнер, серверный ffmpeg не нужен; `audio_data`
    тогда не используется, темп не применяется (метки слов — в шкале записи).
    """
    if not api_key:
        return None
    if time.time() < state.blocked_until:
        # Видимость: без этой строки блокировка молчит до самого истечения —
        # с терминала не отличить "ждёт таймер" от "забыл про Deepgram вовсе"
        # (тот же пробел, что был у groq_engine.py, найден на одном и том же
        # живом инциденте 11.08.26).
        logger.info("deepgram: заблокирован (%s), ещё %.0fс — ухожу на groq/local",
                   state.last_reason or "?", state.blocked_until - time.time())
        return None

    if upload is not None:
        payload, mime = upload
        tempo = 1.0
    else:
        packed = create_upload_audio(audio_data, sample_rate, tempo=profile.asr_tempo)
        if not packed:
            return None
        payload, _filename, mime = packed
        tempo = profile.asr_tempo

    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": mime,
    }
    http = session or requests

    t0 = time.time()
    for attempt in range(profile.deepgram_retries):
        try:
            # timeout=(подключение, чтение): недоступный хост раньше держал
            # целиком deepgram_timeout до каждого ухода на следующую ступень —
            # на медленной сети это был основной источник «долго думает».
            response = http.post(
                DEEPGRAM_URL, params=profile.deepgram_params, headers=headers,
                data=payload, timeout=(5.0, profile.deepgram_timeout),
            )

            if response.status_code == 200:
                try:
                    alt = response.json()["results"]["channels"][0]["alternatives"][0]
                except (KeyError, IndexError, ValueError) as e:
                    state.last_reason = f"ответ не разобран ({type(e).__name__})"
                    logger.info("deepgram: ответ не разобран (%s) — ухожу дальше по каскаду",
                                type(e).__name__)
                    return None
                text = (alt.get("transcript") or "").strip()
                state.last_seconds = time.time() - t0
                words = alt.get("words") or []
                # Словам уходит ускоренный звук (tempo) — их метки в шкале
                # загруженного файла; умножаем обратно на реальную шкалу записи.
                state.last_word_end = (words[-1].get("end") or 0.0) * tempo if words else 0.0
                if not text:
                    state.last_reason = "пустой ответ"
                    logger.info("deepgram: пусто — ухожу дальше по каскаду")
                    return None
                return text

            if response.status_code in (401, 402, 403):
                reason = {401: "ключ не принят", 402: "кончились деньги на счёте",
                          403: "доступ запрещён"}[response.status_code]
                state.blocked_until = time.time() + profile.deepgram_block_seconds
                state.last_reason = reason
                state.blocked_by_network = False
                mins = int(profile.deepgram_block_seconds / 60)
                logger.warning("deepgram: %s (HTTP %s). Перехожу на следующую ступень, "
                               "повторю попытку через %d мин.", reason, response.status_code, mins)
                return None

            if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < profile.deepgram_retries:
                time.sleep(0.5)
                continue

            state.last_reason = f"статус {response.status_code}"
            logger.info("deepgram: статус %s — ухожу дальше по каскаду", response.status_code)
            return None

        except (requests.Timeout, requests.ConnectionError) as e:
            # Хост недостижим (без VPN, обрыв сети, DNS): без блока КАЖДАЯ диктовка
            # платит connect-таймаутом × retries — у Егора это были лишние ~10-20с
            # на каждой записи, пока VPN выключен. Ставим короткий сетевой блок —
            # снимается пробой (maybe_probe_deepgram_if_blocked) или таймером.
            state.blocked_until = time.time() + NETWORK_BLOCK_SECONDS
            state.blocked_by_network = True
            state.last_reason = f"нет связи ({type(e).__name__})"
            logger.warning("deepgram: нет связи (%s) — ступень выкл на %.0fс, "
                           "ухожу дальше по каскаду", type(e).__name__, NETWORK_BLOCK_SECONDS)
            return None
        except Exception as e:
            if attempt + 1 < profile.deepgram_retries:
                time.sleep(0.5)
                continue
            state.last_reason = f"исключение {type(e).__name__}"
            logger.info("deepgram: %s — ухожу дальше по каскаду", type(e).__name__)
            return None

    return None
