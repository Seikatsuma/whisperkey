"""Снятие водяных знаков ASR ("субтитры сделал DimaTorzok" и подобное) + названия брендов.

Перенесено из WhisperKey `whisperkey.py` (`strip_asr_artifacts`, `clean_noise`,
`BOH_TAIL_MARKERS`, `NARRATOR_LOOP_PATTERN`, `BRAND_NAMES`). Единственное отличие
от оригинала: вместо мутации глобального списка `artifacts_removed` функция ВОЗВРАЩАЕТ
список того, что вырезала — так пакет остаётся пригодным для использования из
нескольких процессов/потоков одновременно (thoughts-bot, transcribe-bot, WhisperKey —
у каждого свой процесс, общий мутируемый список на уровне модуля был бы гонкой).
Вызывающий код сам решает, что делать со списком (WhisperKey — копит его в СВОЙ
`artifacts_removed` для текста уведомления, как и раньше).

thoughts-bot уже имеет свой независимый `_strip_watermarks` (перенесённый руками
раньше) — этот модуль НЕ навязывает замену, просто делает общую версию доступной.
"""
from __future__ import annotations

import re

NARRATOR_LOOP_PATTERN = r'(?:спикер|смикер|speaker)\s+говорит'

# Только водяные знаки Whisper из субтитровых корпусов. Обычные слова русского языка
# сюда попадать НЕ должны: "корректор" и "продолжение следует" съедали до 75% фразы.
BOH_TAIL_MARKERS = [
    "редактор субтитров",
    "субтитры сделал",
    "субтитры сделала",
    "субтитры подогнал",
    "субтитры делал",
    "субтитры создавал",
    "subtitles by",
    "thanks for watching",
    "dimatorzok",
    "ссылка на сайт в описании",
]

# Субтитровые заглушки, которые Whisper пишет на тишине и шуме. Это НЕ водяные
# знаки: внутри живой фразы «продолжение следует» может быть настоящим словом
# (verbatim-тесты это проверяют). Поэтому список применяется только в
# boilerplate_only — когда заглушка составляет ВЕСЬ текст целиком; из середины
# реальной фразы ничего не вырезается.
SILENCE_BOILERPLATE = [
    "продолжение следует",
    "спасибо за просмотр",
    "спасибо за внимание",
    "подписывайтесь на канал",
    "подпишитесь на канал",
    "подпишись на канал",
    "ставьте лайк",
    "ставь лайк",
    "the end",
]

# Аннотации звука в скобках — Whisper помечает ими неречевые участки:
# (музыка), [аплодисменты], (смех) и т.п. На чистом шуме это весь вывод.
_NOISE_ANNOTATION = re.compile(
    r'[\[\(]\s*(?:музыка|аплодисменты|смех|кашель|шум|тишина|вздох|вскрик|звук|молчание)[^\]\)]*[\]\)]',
    flags=re.IGNORECASE)


def boilerplate_only(text: str) -> bool:
    """Вставлять нечего: текст пуст либо целиком состоит из субтитровых
    заглушек и скобочных пометок звука.

    Проверка только на ВЕСЬ текст: «задача сложная, но продолжение следует»
    остаётся (это живая фраза), а «Продолжение следует... Продолжение
    следует...» глушится (это галлюцинация на тишине).
    """
    if not text or not text.strip():
        return True
    stripped = _NOISE_ANNOTATION.sub(" ", text.lower())
    for marker in SILENCE_BOILERPLATE:
        stripped = stripped.replace(marker, " ")
    return not re.sub(r'[\W_]+', '', stripped, flags=re.UNICODE)


NORMALIZE_BRAND_NAMES = True
BRAND_NAMES = {
    r'\bclaude\b': 'Claude',
    r'\bcursor\b': 'Cursor',
    r'\bgroq\b': 'Groq',
    r'\btelegram\b': 'Telegram',
    r'\bwhisper\b': 'Whisper',
    r'\bgithub\b': 'GitHub',
    r'\bapi\b': 'API',
}

_MAX_TAIL_WORDS = 6


def strip_asr_artifacts(text: str) -> tuple[str, list[str]]:
    """Удаляет водяные знаки Whisper, не трогая окружающий текст.

    Возвращает (очищенный_текст, список_того_что_вырезано).
    """
    cleaned = text.strip()
    removed: list[str] = []
    if not cleaned:
        return cleaned, removed

    loop_matches = list(re.finditer(NARRATOR_LOOP_PATTERN, cleaned, flags=re.IGNORECASE))
    if len(loop_matches) >= 2:
        cut_pos = loop_matches[0].start()
        removed.append(f"зацикливание ({len(loop_matches)} повторов)")
        cleaned = cleaned[:cut_pos].strip()

    if not cleaned:
        return cleaned, removed

    for marker in BOH_TAIL_MARKERS:
        pattern = re.compile(
            r'(?:(?<=^)|(?<=[.!?…]))(\s*' + re.escape(marker) + r'([^.!?…]*))',
            flags=re.IGNORECASE,
        )
        while True:
            match = pattern.search(cleaned)
            if not match:
                break
            tail_words = match.group(2).split()
            if len(tail_words) > _MAX_TAIL_WORDS:
                break
            cleaned = (cleaned[:match.start(1)] + ' ' + cleaned[match.end(1):]).strip()
            cleaned = re.sub(r'^\s*[.!?…]+\s*', '', cleaned)
            removed.append(marker)

    return re.sub(r'\s{2,}', ' ', cleaned).strip(), removed


def clean_noise(text: str) -> tuple[str, list[str]]:
    """Снимает водяные знаки ASR и нормализует написание брендов."""
    if not text:
        return "", []
    text, removed = strip_asr_artifacts(text)
    if not text:
        return "", removed

    text = re.sub(r'[.]{4,}', '...', text).strip()

    if NORMALIZE_BRAND_NAMES:
        for pattern, replacement in BRAND_NAMES.items():
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    return text.strip(), removed
