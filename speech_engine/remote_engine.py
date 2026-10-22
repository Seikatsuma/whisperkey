"""Ступень «удалённый каскад»: звук уходит на HTTP-мост `http_bridge/`, который
прогоняет ТОТ ЖЕ dictation-каскад (Deepgram → Groq → local) на стороне сервера.

Зачем (04.10.26): у Егора на Mac'е запасная ступень каскада (Groq) мёртва —
гео-блок 403 с домашнего IP (живой инцидент 11.08.26, см. agent.md). Итог:
любой сбой Deepgram из дома уронит диктовку прямо в локальную модель — и по
качеству (small на CPU, ~14% ошибок против nova-2), и по скорости. Мост на
сервере держит полный каскад с рабочими Deepgram и Groq — удалённая ступень
отдаёт качество «как через сервер» дословно.

Включается адресом: `REMOTE_ASR_URL` (например,
`http://127.0.0.1:8092/audio/transcriptions` через SSH-туннель к серверу) —
пусто = ступени нет вообще, никаких лишних запросов. `REMOTE_ASR_TOKEN` —
заголовок `X-Bridge-Token`, если мост выставлен наружу с проверкой (через
туннель на 127.0.0.1 не нужен).

Возвращает только текст (мост отдаёт `{"text": ...}` — свои chunk-метки и
words[] у него внутри). Темп НЕ применяем: сервер применит свой asr_tempo
внутри своего каскада — двойное ускорение 1.03×1.03 дало бы ~1.06.
"""
from __future__ import annotations

import io
import logging
import os

import numpy as np

from .audio import create_upload_audio
from .context import Context

logger = logging.getLogger("speech_engine.remote")

REMOTE_TIMEOUT = (5.0, 120.0)   # connect, read: каскад на длинной записи ~10с


def transcribe_remote(audio: np.ndarray, dur: float, ctx: Context) -> str:
    """Один запрос к мосту на ВСЮ запись (он сам дробит, если надо).
    Любой отказ → "", ступень просто пропускается, дальше и так есть локальная
    модель. Единственный повтор — после исключения соединения, когда клиентский
    `remote_reviver` (сторож туннеля на Mac) уже успел поднять мост обратно."""
    url = ctx.remote_asr_url
    if not url:
        # Сторож уже мог снять адрес после упавшей пробы, а туннель к моменту
        # записи восстановиться. Если клиент дал reviver — спрашиваем мост прямо
        # сейчас, не дожидаясь следующего тика сторожа.
        reviver = getattr(ctx, "remote_reviver", None)
        if reviver is None:
            return ""
        try:
            if not reviver():
                return ""
        except Exception:
            return ""
        url = ctx.remote_asr_url
        if not url:
            return ""
    upload = create_upload_audio(audio, ctx.sample_rate, tempo=1.0)
    if not upload:
        return ""
    payload, name, mime = upload
    headers = {}
    if ctx.remote_asr_token:
        headers["X-Bridge-Token"] = ctx.remote_asr_token

    def _post() -> str:
        files = {"file": (name, io.BytesIO(payload), mime)}
        r = ctx.session.post(ctx.remote_asr_url, files=files, headers=headers,
                             timeout=REMOTE_TIMEOUT)
        if r.status_code != 200:
            ctx.remote_last_error = f"статус {r.status_code}"
            return ""
        text = (r.json().get("text") or "").strip()
        ctx.remote_last_error = "" if text else "пустой ответ"
        return text

    try:
        text = _post()
        if not text:
            logger.warning("remote: %s, отказ", ctx.remote_last_error)
        return text
    except Exception as e:
        ctx.remote_last_error = f"исключение {type(e).__name__}"
        logger.warning("remote: исключение %s", type(e).__name__)

    # Туннель мог умереть посреди запроса: сторож переподнимает его за секунды,
    # но без повтора запись уезжала в локальную модель на ~50с (живой журнал
    # 22.10.26: обрыв домашнего интернета на Mac → 3 записи подряд по 48-61с
    # через local, хотя туннель каждый раз поднимался обратно). Даём
    # поднявшемуся мосту ОДИН повтор — дёшево против цены локальной модели.
    reviver = getattr(ctx, "remote_reviver", None)
    if reviver is None:
        return ""
    try:
        revived = bool(reviver())
    except Exception:
        revived = False
    if not revived or not ctx.remote_asr_url:
        return ""
    try:
        ctx.session.close()   # в пуле лежит мёртвый сокет старого туннеля
    except Exception:
        pass
    try:
        text = _post()
        if text:
            logger.warning("remote: повтор после поднятия туннеля — успех")
        else:
            logger.warning("remote: повтор после поднятия туннеля — %s",
                           ctx.remote_last_error)
        return text
    except Exception as e:
        ctx.remote_last_error = f"исключение {type(e).__name__}"
        logger.warning("remote: повтор после поднятия туннеля — %s", type(e).__name__)
        return ""
