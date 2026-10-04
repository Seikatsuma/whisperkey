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
    Любой отказ → "", ступень просто пропускается — без ретраев, дальше и так
    есть локальная модель."""
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
    files = {"file": (name, io.BytesIO(payload), mime)}
    try:
        r = ctx.session.post(url, files=files, headers=headers, timeout=REMOTE_TIMEOUT)
        if r.status_code != 200:
            logger.warning("remote: статус %s, отказ", r.status_code)
            return ""
        text = (r.json().get("text") or "").strip()
        if not text:
            logger.info("remote: пустой ответ")
        return text
    except Exception as e:
        logger.warning("remote: исключение %s", type(e).__name__)
        return ""
