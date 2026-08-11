"""Профиль dictation: Deepgram → Groq (со всей обвязкой) → локальная модель, по очереди.

Перенесено из WhisperKey `whisperkey.py` (`_recognize_with_whisper`, `_transcribe_one_chunk`,
`_needs_retry`, и верхнеуровневая логика каскада, которая раньше жила прямо в
`process_audio`). Ступени идут ПО ОЧЕРЕДИ, не параллельно: первая справляется быстрее,
чем вторая успела бы стартовать (замер: 1.6с против 5.6с на пятиминутной записи) —
см. `~/whisperkey/Whisper на MAC/agent.md`.

`fix_known_terms` применяется ВСЕГДА, независимо от того, какая ступень дала текст —
это было явное поведение `process_audio` (строка "Таблица названий работает ВСЕГДА").
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from . import chunking
from .context import Context
from .deepgram_engine import transcribe_deepgram
from .density_gate import needs_retry
from .groq_engine import transcribe_groq
from .local_engine import transcribe_local
from .terms import fix_known_terms
from .transfer import transfer_endings, transfer_punctuation
from .terms import transfer_terms
from .types import RecognitionResult
from .watermarks import strip_asr_artifacts

logger = logging.getLogger("speech_engine.dictation")

HALLUCINATION_TRIGGERS = [
    "спикер говорит",
    "смикер говорит",
    "голос за кадром",
]


def _transcribe_one_chunk(chunk: np.ndarray, chunk_idx: int, n_chunks: int, ctx: Context):
    """Распознаёт один кусок. Возвращает (индекс, текст|None, метка_качества, слова)."""
    chunk_text = None
    chunk_words: list = []
    quality = 'lost'
    chunk_dur = len(chunk) / ctx.sample_rate
    use_cloud = bool(ctx.groq_api_key) and not ctx.cloud_state.is_blocked

    if use_cloud:
        # use_prompt=True — как в оригинале (implicit default в transcribe_cloud_turbo).
        # Для профиля dictation groq_context_prompt="" всегда, так что на практике
        # это тождественно пустому промпту; для профиля calls (groq_context_prompt
        # непуст) это тот самый путь, которым VERBATIM_PROMPT уходит в основной запрос.
        raw_text, chunk_words = transcribe_groq(
            chunk, api_key=ctx.groq_api_key, profile=ctx.profile, sample_rate=ctx.sample_rate,
            state=ctx.cloud_state, throttle=ctx.throttle, session=ctx.session,
            use_prompt=True, return_words=True)
        degenerate = list(ctx.cloud_state.last_degenerate or [])
        if raw_text:
            lower = raw_text.lower()
            if any(t in lower for t in HALLUCINATION_TRIGGERS):
                cleaned, _removed = strip_asr_artifacts(raw_text)
                if cleaned and (len(cleaned.split()) >= 3 or chunk_dur <= 5.0):
                    chunk_text, quality = cleaned, 'cloud'
                else:
                    logger.info("guard: сегмент %d пуст после чистки", chunk_idx + 1)
            else:
                chunk_text, quality = raw_text, 'cloud'

        # ВНИМАНИЕ — сохранено дословно из оригинала (whisperkey.py, `_transcribe_one_chunk`):
        # условие включает "and ctx.profile.groq_context_prompt", а у профиля dictation
        # этот промпт ВСЕГДА пуст ("" ложно) — то есть в dictation эта ветка переспроса
        # НИКОГДА не срабатывает на практике (мёртвый код при пустом промпте). Это
        # поведение оригинала, не баг переноса: перенесено намеренно как есть, чтобы
        # не поменять число запросов к Groq по сравнению с уже измеренным WhisperKey.
        # Для профиля calls groq_context_prompt непуст — там ветка живая.
        if degenerate and ctx.profile.groq_context_prompt and \
                needs_retry(degenerate, chunk_words, ctx.profile.density_gate_min_words_per_sec):
            logger.info("gate: сегмент %d — переспрашиваю без промпта", chunk_idx + 1)
            retry_text, retry_words = transcribe_groq(
                chunk, api_key=ctx.groq_api_key, profile=ctx.profile, sample_rate=ctx.sample_rate,
                state=ctx.cloud_state, throttle=ctx.throttle, session=ctx.session,
                use_prompt=False, return_words=True)
            if retry_text and len(retry_text.split()) > len((chunk_text or '').split()):
                chunk_text, quality, chunk_words = retry_text, 'cloud_retry', retry_words

    if not chunk_text and ctx.local_model is not None:
        logger.info("local: сегмент %d — локальная модель (качество ниже)", chunk_idx + 1)
        try:
            chunk_text = transcribe_local(chunk, ctx.local_model) or None
            quality = 'local' if chunk_text else 'lost'
            chunk_words = []
        except Exception as e:
            logger.warning("local error: сегмент %d: %s", chunk_idx + 1, type(e).__name__)
            chunk_text, quality = None, 'lost'

    return chunk_idx, chunk_text, quality, chunk_words


def _recognize_with_groq_cascade(audio: np.ndarray, dur: float, ctx: Context):
    """Вторая и третья ступени: Groq, а при его отказе — локальная модель.

    Возвращает (текст, текст_до_переносов, потерянные_куски, качество_кусков).
    """
    p = ctx.profile
    audio_chunks, chunk_offsets = chunking.split_audio(
        audio, dur, ctx.sample_rate, p.chunk_threshold_seconds, p.chunk_size_seconds,
        p.chunk_overlap_seconds)
    n_chunks = len(audio_chunks)

    style_pool = style_future = None
    if (p.punct_transfer_min_seconds <= dur <= p.punct_transfer_max_seconds
            and ctx.groq_api_key and not ctx.cloud_state.is_blocked and p.style_prompt):
        style_pool = ThreadPoolExecutor(max_workers=1)
        style_future = style_pool.submit(
            transcribe_groq, audio, api_key=ctx.groq_api_key, profile=p, sample_rate=ctx.sample_rate,
            state=ctx.cloud_state, throttle=ctx.throttle, session=ctx.session,
            allow_retry=False, prompt_override=p.style_prompt)

    ordered_parts: list = [None] * n_chunks
    chunk_quality: list[str] = ['lost'] * n_chunks
    chunk_words: list[list] = [[] for _ in range(n_chunks)]

    parallel_ok = (p.parallel_cloud_chunks and n_chunks > 1
                  and ctx.groq_api_key and not ctx.cloud_state.is_blocked)

    if parallel_ok:
        with ThreadPoolExecutor(max_workers=p.max_cloud_workers) as pool:
            futures = {
                pool.submit(_transcribe_one_chunk, chunk, idx, n_chunks, ctx): idx
                for idx, chunk in enumerate(audio_chunks)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    idx, chunk_text, quality, words = fut.result()
                except Exception as e:
                    logger.warning("сегмент %d упал: %s", idx + 1, type(e).__name__)
                    chunk_text, quality, words = None, 'lost', []
                ordered_parts[idx] = chunk_text
                chunk_quality[idx] = quality
                chunk_words[idx] = words
    else:
        for idx, chunk in enumerate(audio_chunks):
            try:
                _, chunk_text, quality, words = _transcribe_one_chunk(chunk, idx, n_chunks, ctx)
            except Exception as e:
                logger.warning("сегмент %d упал: %s", idx + 1, type(e).__name__)
                chunk_text, quality, words = None, 'lost', []
            ordered_parts[idx] = chunk_text
            chunk_quality[idx] = quality
            chunk_words[idx] = words

    text, lost_marks = chunking.join_chunks(ordered_parts, chunk_quality, chunk_offsets, chunk_words)

    if not text:
        if style_pool is not None:
            style_pool.shutdown(wait=False)
        return "", "", lost_marks, chunk_quality

    assembled = text

    if style_future is not None:
        try:
            styled = style_future.result(timeout=8.0)
            if styled:
                text = transfer_punctuation(text, styled)
                text, _n_terms = transfer_terms(text, styled)
                text, _n_end = transfer_endings(text, styled)
        except Exception as e:
            logger.info("punct: второй проход не дошёл (%s) — текст без добавленных знаков",
                       type(e).__name__)
        finally:
            style_pool.shutdown(wait=False)

    return text, assembled, lost_marks, chunk_quality


def recognize(audio: np.ndarray, dur: float, ctx: Context) -> RecognitionResult:
    """Полный каскад dictation: Deepgram → Groq → локальная модель.

    fix_known_terms применяется всегда, вне зависимости от того, какая ступень
    дала текст (это поведение перенесено из process_audio дословно).
    """
    p = ctx.profile

    full_raw_text = None
    if p.deepgram_enabled and ctx.deepgram_api_key:
        full_raw_text = transcribe_deepgram(
            audio, api_key=ctx.deepgram_api_key, profile=p, sample_rate=ctx.sample_rate,
            state=ctx.deepgram_state, session=ctx.session)

    if full_raw_text:
        engine = "deepgram"
        assembled_text = full_raw_text
        lost_marks: list = []
        chunk_quality: list[str] = ['deepgram']
        degenerate: list = []
    else:
        engine = "groq" if ctx.groq_api_key else "local"
        full_raw_text, assembled_text, lost_marks, chunk_quality = _recognize_with_groq_cascade(
            audio, dur, ctx)
        degenerate = list(ctx.cloud_state.last_degenerate or [])
        if 'local' in chunk_quality and 'cloud' not in chunk_quality and 'cloud_retry' not in chunk_quality:
            engine = "local"

    if not full_raw_text:
        return RecognitionResult(text="", engine="", assembled_text="", lost_marks=lost_marks,
                                 chunk_quality=chunk_quality, degenerate=degenerate)

    full_raw_text, n_fix = fix_known_terms(full_raw_text)

    return RecognitionResult(
        text=full_raw_text, engine=engine, assembled_text=assembled_text,
        lost_marks=lost_marks, chunk_quality=chunk_quality, degenerate=degenerate,
        terms_fixed=n_fix,
        meta={
            "deepgram_seconds": ctx.deepgram_state.last_seconds,
            "api_text": ctx.cloud_state.last_api_text,
        },
    )
