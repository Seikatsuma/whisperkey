"""Профиль calls — фасад, не самостоятельный каскад.

В отличие от `dictation.py` (полный каскад "дай мне аудио, получи текст" — там
он оправдан, потому что WhisperKey/thoughts-bot ничего похожего не имели), профиль
calls в transcribe-bot встраивается ПОВЕРХ уже существующего, боевого, проверенного
конвейера (`transcribe_core.py`: нарезка, `RateLimiter`, спасение сорванных сегментов
и дыр, учёт общей квоты). Переписывать этот конвейер внутри speech_engine — не задача
этой фазы и прямой риск (см. красная линия брифа: "бот в проде, не переизобретать то,
что уже работает"). Поэтому calls.py даёт только СТРОИТЕЛЬНЫЕ БЛОКИ, которые
transcribe-bot вызывает как библиотеку, оставляя себе оркестрацию:

  - CALLS профиль (`profiles.CALLS`) — параметры нарезки/гейта/голосования.
  - `plan_voter_grid()` — сдвинутая сетка нарезки для второго (голосующего) прохода.
  - `vote_merge()` (из `vote.py`) — слияние primary+voter по выравниванию.

Пример интеграции (эскиз, НЕ подключён к прод-пути transcribe-bot в этой сдаче —
см. `~/transcribe-bot/speech_engine_calls_integration.py` и отчёт о сдаче):

    from speech_engine import CALLS, calls, vote

    primary_words = <результат штатного прохода transcribe_core, words[] как есть>
    voter_plan = calls.plan_voter_grid(duration, CALLS)
    voter_words = <прогнать voter_plan через ТОТ ЖЕ transcribe_core, тот же чанкер>
    merged, stats = vote.vote_merge(
        vote.words_from_groq(primary_words), vote.words_from_groq(voter_words),
        min_run=CALLS.vote_min_run, sparsity_ratio=CALLS.vote_sparsity_ratio,
        has_loop_fn=transcribe_core.has_loop,   # прод-версия, не дублировать
    )
"""
from __future__ import annotations

from . import chunking
from .profiles import Profile


def plan_voter_grid(duration: float, profile: Profile) -> list[chunking.ChunkPlan]:
    """Сдвинутая сетка нарезки для второго (голосующего) прохода профиля calls."""
    if not profile.voting_enabled or not profile.shifted_grid_fraction:
        raise ValueError("голосование выключено в этом профиле "
                         "(voting_enabled=False или shifted_grid_fraction не задан)")
    return chunking.plan_chunks_shifted(
        duration, profile.chunk_size_seconds, profile.chunk_overlap_seconds,
        fraction=profile.shifted_grid_fraction)


def plan_primary_grid(duration: float, profile: Profile) -> list[chunking.ChunkPlan]:
    """Штатная (несдвинутая) сетка нарезки — для симметрии с plan_voter_grid."""
    return chunking.plan_chunks(duration, profile.chunk_size_seconds, profile.chunk_overlap_seconds)
