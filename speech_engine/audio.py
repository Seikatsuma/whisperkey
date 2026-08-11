"""Темп звука перед отправкой в облако + упаковка в WAV.

Перенесено из WhisperKey `whisperkey.py` (`_change_tempo`, `_restore_timeline`,
`create_audio_wav`). Обоснование трюка с темпом (`ASR_TEMPO`) и что было исключено
(паддинг тишиной, нормировка) — в `agent.md` этого пакета и в
`~/whisperkey/Whisper на MAC/agent.md`, раздел «Звук уходит в облако ускоренным на 3%».

Область действия темпа — ТОЛЬКО профиль dictation (`Profile.asr_tempo=1.03`). Профиль
calls держит `asr_tempo=1.0` (тождество, функция ничего не делает) — на звонках
эффект не измерялся, а отдельное исследование (2026-08-09-transcribe-bot-quality-research.md,
кандидат 3) прямо показало, что ЛЮБАЯ предобработка звука (там — громкость) вредит
на этом домене; распространять непроверенный трюк с темпом того же прежде, чем
кто-то его измерит на звонках, значит повторить ту же ошибку с новым именем.
"""
from __future__ import annotations

import io
import wave

import numpy as np


def change_tempo(audio_data: np.ndarray, factor: float) -> np.ndarray:
    """Меняет темп линейной интерполяцией по новой сетке отсчётов. factor=1.0 — тождество."""
    if factor == 1.0:
        return audio_data
    n = int(len(audio_data) / factor)
    if n < 2:
        return audio_data
    grid = np.linspace(0, len(audio_data) - 1, n)
    return np.interp(grid, np.arange(len(audio_data)), audio_data).astype(np.float32)


def restore_timeline(result: dict, tempo: float) -> None:
    """Возвращает таймкоды ответа к реальному времени записи (мутирует result на месте)."""
    if tempo == 1.0:
        return
    for key in ('segments', 'words'):
        for item in (result.get(key) or []):
            for field in ('start', 'end'):
                v = item.get(field)
                if isinstance(v, (int, float)):
                    item[field] = v * tempo


def create_audio_wav(audio_data: np.ndarray, sample_rate: int, tempo: float = 1.0) -> bytes | None:
    """Упаковка звука в WAV. Единственная обработка — ускорение темпа (если tempo != 1.0)."""
    try:
        audio_data = np.asarray(audio_data, dtype=np.float32)
        if tempo != 1.0:
            audio_data = change_tempo(audio_data, tempo)
        audio_data = np.clip(audio_data, -1.0, 1.0)

        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes((audio_data * 32767).astype(np.int16).tobytes())

        return wav_io.getvalue()
    except Exception:
        return None
