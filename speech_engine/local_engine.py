"""Локальная модель — третья, последняя ступень dictation (когда нет интернета вовсе).

Перенесено из WhisperKey `whisperkey.py` (`_transcribe_local`). Модель — объект
`faster_whisper.WhisperModel`, ЗАГРУЖЕННЫЙ И ПЕРЕДАННЫЙ вызывающим кодом (dependency
injection), а не создаваемый пакетом: загрузка модели — дорогая (память, время
старта), и решение, держать ли её в памяти процесса, принимает каждый бот сам.
Если `model=None` — ступень пропускается, каскад отдаёт то, что есть (см. `dictation.py`),
это штатное поведение, не ошибка: у thoughts-bot и transcribe-bot локальной модели
сейчас нет вовсе, добавлять её этим переносом брифом не предписано.
"""
from __future__ import annotations

import numpy as np


def transcribe_local(chunk: np.ndarray, model, *, language: str = "ru",
                     context_prompt: str = "") -> str:
    """model — объект с методом .transcribe() в духе faster_whisper.WhisperModel."""
    max_val = np.max(np.abs(chunk))
    if max_val > 0.0001:
        chunk = chunk / max_val * 0.99
    segments, _ = model.transcribe(
        chunk, language=language,
        beam_size=5, patience=1.0, repetition_penalty=1.2,
        vad_filter=False, suppress_blank=True, without_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=context_prompt or None,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()
