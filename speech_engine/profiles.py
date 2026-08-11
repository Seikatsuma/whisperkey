"""Профили — домен передаётся явно, ни одна настройка не хардкожена в движке.

Два независимых замера в двух разных проектах дали ПРОТИВОПОЛОЖНЫЙ ответ на вопрос
"какой движок лучше": на диктовке (WhisperKey, один голос, близкий микрофон) Deepgram
nova-2 выигрывает у Groq на 7-8 п.п.; на звонках (transcribe-bot, несколько говорящих,
телефонное качество) Deepgram nova-3 ПРОИГРЫВАЕТ Groq turbo (92.3% против 92.9%), а nova-2
на этом материале ещё слабее (84.4%). Отсюда — профили, а не одна конфигурация:

  dictation — Deepgram nova-2 → Groq (турбо/large) → локальная модель, по очереди.
              WhisperKey, thoughts-bot. Один голос, близкий микрофон, важна задержка.
  calls     — только Groq turbo: основной проход + проход со сдвинутой сеткой нарезки,
              слияние по выравниванию текстов. transcribe-bot. Несколько говорящих,
              телефонное/Zoom-качество, задержка не критична (Егор явно подтвердил,
              что готов ждать расшифровку длиной с саму запись).

Источники чисел — `agent.md` этого пакета, раздел «Профили и откуда цифры».
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    name: str

    # ── Deepgram (первая ступень; для calls выключена целиком — deepgram_enabled=False) ──
    deepgram_enabled: bool
    deepgram_model: str = "nova-2"
    deepgram_params: dict = field(default_factory=dict)
    deepgram_timeout: float = 20.0
    deepgram_retries: int = 2
    deepgram_block_seconds: float = 900.0

    # ── Groq ──
    groq_model: str = "whisper-large-v3-turbo"
    groq_language: str = "ru"
    groq_temperature: float = 0.0
    min_request_interval: float = 0.7   # троттлинг между запросами одного процесса
    # Контекстный промпт ОСНОВНОГО запроса (не путать со style_prompt — тот идёт
    # вторым, параллельным проходом только в dictation). У dictation пуст намеренно:
    # на короткой диктовке любой промпт глушит речь (WhisperKey agent.md, замер
    # 06.08.26: 80 слов с промптом против 112 без). У calls, наоборот, дословный
    # разговорный промпт поднимает полноту с 76.7% до 92.1% (transcribe-bot
    # pipeline.py, VERBATIM_PROMPT) — запись длинная, короткий клип не рискует
    # схлопнуться в одно слово из промпта. Это ещё один пример "домен решает,
    # не универсальная константа" — не путать одно с другим.
    groq_context_prompt: str = ""

    # ── Темп звука перед отправкой в облако (только dictation; 1.0 = выключено) ──
    asr_tempo: float = 1.0

    # ── Второй проход за пунктуацией/терминами/формами (только dictation) ──
    style_prompt: str = ""
    punct_transfer_min_seconds: float = 0.0
    punct_transfer_max_seconds: float = 0.0

    # ── Нарезка длинных записей ──
    # 700, не 900: при 16 кГц/16 бит/моно (SAMPLE_RATE=16000 в WhisperKey) один кусок
    # длиннее ~780с уже весит больше 25 МБ — лимита Groq на бесплатном тарифе — и вместо
    # нарезки ловит тихий 413, откуда цепочка молча уходит на локальную модель (см.
    # agent.md WhisperKey, «Открытый дефект этого порога», зафиксировано 08.08.26,
    # исправлено 11.08.26). 700×32000=22.4 МБ — запас даже без учёта ASR_TEMPO,
    # который дополнительно уменьшает реальный вес перед отправкой.
    chunk_threshold_seconds: float = 700.0   # dictation: порог, ПОСЛЕ которого режем
    chunk_size_seconds: float = 60.0
    chunk_overlap_seconds: float = 3.0
    parallel_cloud_chunks: bool = True
    max_cloud_workers: int = 3

    # ── Гейт плотности (детектор срыва декодирования) ──
    density_gate_min_duration: float = 8.0
    density_gate_min_words_per_sec: float = 0.5

    # ── Голосование (только calls) ──
    voting_enabled: bool = False
    # Доля chunk_size_seconds, на которую сдвинута сетка второго прохода.
    # 0.5 = сдвиг на половину шага — единственная проверенная замером конфигурация
    # (research 2026-08-09-transcribe-bot-quality-research.md, кандидат 1).
    shifted_grid_fraction: float | None = None
    vote_min_run: int = 2          # минимум подряд слов voter'а без пары в primary
    vote_sparsity_ratio: float = 0.6  # primary в этом окне должен быть беднее voter'а не более чем во столько раз


DICTATION = Profile(
    name="dictation",
    deepgram_enabled=True,
    deepgram_model="nova-2",
    deepgram_params={
        "model": "nova-2",
        "language": "ru",
        "punctuate": "true",
        "smart_format": "false",   # переписывает "двадцать пять" в "25" — дословность важнее
        "filler_words": "true",    # "ну", "вот", "значит" — часть речи Егора, не мусор
        "numerals": "false",
    },
    deepgram_timeout=20.0,
    deepgram_retries=2,
    deepgram_block_seconds=900.0,
    groq_model="whisper-large-v3",
    groq_language="ru",
    groq_temperature=0.0,
    min_request_interval=0.7,
    asr_tempo=1.03,     # замер 06.08.26: 14.8% -> 13.3% ошибок на 19 диктовках
    style_prompt=(
        "Так, ну, в общем, смотри. Значит, вот. И, соответственно, дальше. "
        "Claude Code, Claude Design, WhisperKey, Notebook LM, GitHub, Telegram, "
        "Groq, DeepGram, CEO, API, Яндекс Маркет, Ozon, Wildberries, iPad, "
        "промпт, токены, агенты, скиллы. "
        "Сохрани. Раздели. Выстрой. Реализуй. Проверь. Покажи. Открой. "
        "Запусти. Найди. Убери. Оставь. Пришли. Собери. Напиши. Сделай. "
        "Отметь. Скинь. Запиши. Продолжай. Добавляй."
    ),
    punct_transfer_min_seconds=12.0,
    punct_transfer_max_seconds=600.0,
    chunk_threshold_seconds=700.0,
    chunk_size_seconds=60.0,
    chunk_overlap_seconds=3.0,
    parallel_cloud_chunks=True,
    max_cloud_workers=3,
    density_gate_min_duration=8.0,
    density_gate_min_words_per_sec=0.5,
    voting_enabled=False,
)

CALLS = Profile(
    name="calls",
    deepgram_enabled=False,   # Решение Егора 09.08.26: Deepgram в TranscribeBot не трогать.
    groq_model="whisper-large-v3-turbo",
    groq_language="ru",
    groq_temperature=0.0,
    min_request_interval=1.0 / 3.0,   # RATE_PER_SEC=3 в проде transcribe-bot
    groq_context_prompt=(
        "Ну вот, смотри, там как бы, да? Ну то есть, это самое, "
        "вот именно так, да. Понятно, ну ладно."
    ),   # VERBATIM_PROMPT из transcribe-bot pipeline.py — замер 92.1% против 76.7% без
    asr_tempo=1.0,        # ×1.03 в WhisperKey не переносится: не измерялось на звонках,
                           # а исследование про звонки явно предостерегает от предобработки
                           # звука на этом домене (кандидат "громкость" — подтверждённый вред).
    chunk_threshold_seconds=0.0,   # calls режется всегда, порога "не резать" здесь нет
    chunk_size_seconds=45.0,       # число подобрано отдельным замером именно на звонках
    chunk_overlap_seconds=6.0,     # research: 3с->6с включить как бесплатную добавку
    parallel_cloud_chunks=True,
    max_cloud_workers=4,           # CONCURRENCY в transcribe_core.py
    density_gate_min_duration=8.0,
    density_gate_min_words_per_sec=0.8,   # DEGENERATE_WPS в transcribe_core.py (не 0.5!)
    voting_enabled=True,
    shifted_grid_fraction=0.5,
    vote_min_run=2,
    vote_sparsity_ratio=0.6,
)


def get(name: str) -> Profile:
    if name == "dictation":
        return DICTATION
    if name == "calls":
        return CALLS
    raise ValueError(f"неизвестный профиль: {name!r} (ожидался 'dictation' или 'calls')")
