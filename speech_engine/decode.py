"""Декодирование произвольного аудиофайла в mono 16кГц float32 через ffmpeg.

Нужно всем потребителям пакета, которые получают ГОТОВЫЙ ФАЙЛ (OGG/Opus голосовое
из Telegram, mp3 и т.п.), а не сырой поток с микрофона (WhisperKey сам пишет
float32-массив и файл ему декодировать не нужно). thoughts-bot, transcribe-bot,
claude-tg-bot — все читают файл с диска, поэтому вынесено сюда одной функцией,
а не продублировано в каждом проекте (тот самый принцип, ради которого пакет
вообще существует).
"""
from __future__ import annotations

import os
import subprocess
import tempfile

import numpy as np


def decode_to_pcm16k(path: str, sample_rate: int = 16000) -> np.ndarray:
    """Декодирует произвольный аудиофайл в mono `sample_rate` Гц float32 через ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
             "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", tmp_path],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg не смог декодировать {path!r}: {r.stderr.strip()[:300]}")
        raw = np.fromfile(tmp_path, dtype=np.int16)
        return raw.astype(np.float32) / 32768.0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
