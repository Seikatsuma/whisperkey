"""Yandex SpeechKit — резервная облачная ступень, доступная без VPN из РФ.

Зачем (19.10.26): жалоба Егора «диктовка работает только при включённом VPN».
Deepgram с домашнего IP недоступен (проверено поведением: без VPN connect
не поднимается), Groq под гео-блоком 403. Мост через SSH-туннель закрывает
большинство случаев, но туннель может быть не поднят (ручной запуск без
лаунчера, умерший ssh). Yandex Cloud — российский сервис, его API
доступен без VPN — это последняя облачная ступень перед локальной моделью.

Включается ТОЛЬКО ключом в конфиге: `YANDEX_API_KEY` (и опционально
`YANDEX_FOLDER_ID` — для аккаунтов, где привязка по каталогу). Ключа нет —
ступени нет, ни одного лишнего запроса. Ключ получается в консоли
cloud.yandex.ru → сервисный аккаунт → «API-ключ» (бесплатный грант на старте
покрывает диктовку с запасом).

Ограничение синхронного API: до ~30 секунд звука и ~1 МБ на запрос.
Диктовки длиннее YANDEX_MAX_SECONDS ступень пропускает — длинная запись
уйдёт на локальную модель (async API SpeechKit с S3-выгрузкой здесь
намеренно не делаем: сложность не оправдана резервным характером ступени).

Качество ru на диктовке не измерялось на корпусе Егора — ступень стоит
ПОСЛЕ моста и ДО локальной модели: хуже Deepgram она быть не должна по
документации, но это проверяется вживую, не догадкой. Помечает себя в
`ctx.yandex_last_error` для строки [engine fallback].
"""
from __future__ import annotations

import logging

import numpy as np
import requests

from .context import Context

logger = logging.getLogger("speech_engine.yandex")

YANDEX_STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
YANDEX_MAX_SECONDS = 29.0     # синхронный API — потолок ~30с/1МБ на запрос
YANDEX_TIMEOUT = (5.0, 60.0)  # connect, read


def transcribe_yandex(audio: np.ndarray, dur: float, ctx: Context) -> str:
    """Один синхронный запрос к SpeechKit. Любой отказ → "" и причина в
    ctx.yandex_last_error — ступень пропускается, каскад идёт дальше."""
    if not ctx.yandex_api_key:
        return ""
    if dur > YANDEX_MAX_SECONDS:
        ctx.yandex_last_error = f"запись {dur:.0f}с длиннее лимита синхронного API ({YANDEX_MAX_SECONDS:.0f}с)"
        logger.info("yandex: %s", ctx.yandex_last_error)
        return ""

    try:
        pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767).astype(np.int16)
    except Exception as e:
        ctx.yandex_last_error = f"звук не собрался ({type(e).__name__})"
        return ""

    headers = {"Authorization": f"Api-Key {ctx.yandex_api_key}"}
    params = {
        "lang": "ru-RU",
        "format": "lpcm",
        "sampleRateHertz": str(ctx.sample_rate),
        "punctuation": "true",
    }
    if ctx.yandex_folder_id:
        params["folderId"] = ctx.yandex_folder_id

    try:
        r = ctx.session.post(YANDEX_STT_URL, params=params, headers=headers,
                             data=pcm.tobytes(), timeout=YANDEX_TIMEOUT)
        if r.status_code != 200:
            # У SpeechKit тело ошибки — JSON с errorCode/error_message, в отличие
            # от немого статуса оно объясняет причину (квота, ключ, folderId).
            detail = ""
            try:
                detail = (r.json().get("error_message") or "")[:120]
            except Exception:
                pass
            ctx.yandex_last_error = f"статус {r.status_code}" + (f" ({detail})" if detail else "")
            logger.warning("yandex: %s", ctx.yandex_last_error)
            return ""
        text = (r.json().get("result") or "").strip()
        ctx.yandex_last_error = "" if text else "пустой ответ"
        return text
    except Exception as e:
        ctx.yandex_last_error = f"исключение {type(e).__name__}"
        logger.warning("yandex: исключение %s", type(e).__name__)
        return ""
