"""Тонкий CLI-вход: `python3 -m speech_engine.cli <путь_к_файлу> [--profile dictation|calls]`.

Печатает в stdout ровно одну строку JSON: `{"text": ..., "engine": ...}`.
Задуман под архитектурный шов claude-tg-bot (`TRANSCRIBE_BIN` — Node зовёт Python
как подпроцесс и разбирает его JSON, см. бриф унификации, раздел про claude-tg-bot).

Это ЕДИНСТВЕННЫЙ модуль пакета, который открывает файл, декодирует звук (через
ffmpeg — уже есть в стеке, лишней зависимости не добавляет) и печатает результат.
Он НЕ подключён ни к одному из пяти проектов в этой сдаче: подключение claude-tg-bot
(правка `TRANSCRIBE_BIN` в `bot.js`) — часть фазы F брифа унификации, которая ждёт
подтверждения объёма Егором. Модуль существует, чтобы это подключение было готово
сделать одной строкой конфига, когда/если подтверждение придёт.

Профиль по умолчанию — dictation (короткие голосовые в Telegram — тот же домен,
что у WhisperKey/thoughts-bot; см. брифа: "Toki-bot и claude-tg-bot, скорее всего,
тоже dictation... подтверди замером на их реальном материале, не бери на веру").
Ключи читаются из окружения (GROQ_API_KEY, DEEPGRAM_API_KEY) — как и everywhere
в этом стеке, секреты не читаются из аргументов командной строки и не печатаются.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .decode import decode_to_pcm16k
from .engine import Engine
from .profiles import DICTATION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m speech_engine.cli")
    parser.add_argument("audio_path", help="путь к аудиофайлу (любой формат, читаемый ffmpeg)")
    parser.add_argument("--profile", default="dictation", choices=["dictation"],
                        help="профиль calls сюда не подключён — см. calls.py, почему")
    args = parser.parse_args(argv)

    try:
        audio = decode_to_pcm16k(args.audio_path)
        dur = len(audio) / 16000.0
        engine = Engine(
            profile=DICTATION,
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
            deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            sample_rate=16000,
        )
        result = engine.recognize(audio, dur)
        print(json.dumps({"text": result.text, "engine": result.engine}, ensure_ascii=False))
        return 0
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        # JSON на stdout — как в happy path, контракт claude-tg-bot не меняется
        # (см. модульный докстринг). ПЛЮС та же ошибка на stderr — bot.js::
        # transcribeLocal() на ненулевом коде выхода читает именно stderr
        # (`err.trim() || 'exit ' + code`), stdout в этой ветке не разбирает;
        # без этой строки пользователь в Telegram видел бы голое "exit 1"
        # вместо причины (проверено вручную: не декодировался битый/несуществующий
        # файл — ffmpeg-ошибка терялась целиком).
        print(json.dumps({"text": "", "engine": "", "error": msg}, ensure_ascii=False))
        print(msg, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
