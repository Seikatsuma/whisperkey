"""Гейт плотности — детектор срыва декодирования Whisper.

Перенесено дословно из WhisperKey `whisperkey.py` (`_find_degenerate_segments`,
`_words_in_range`, `_text_from_response`, `_needs_retry`). `no_speech_prob` не ловит
этот класс отказа (у замеренных пустых сегментов он был 0.028 и 0.581 — «речь точно
есть»); признак «длиннее MIN_DURATION секунд и меньше MIN_WORDS_PER_SEC слов в секунду»
ловит надёжно (см. `agent.md` WhisperKey и transcribe-bot).

Пороги — часть Profile (`density_gate_min_duration`, `density_gate_min_words_per_sec`),
а не константы модуля: у dictation 0.5 слов/с, у calls — 0.8 (DEGENERATE_WPS в
transcribe_core.py, уже подобран отдельным замером именно на звонках). Функции здесь
принимают порог явным параметром.
"""
from __future__ import annotations

import logging

from .chunking import fmt_mmss

logger = logging.getLogger("speech_engine.density_gate")


def find_degenerate_segments(segments: list, min_duration: float, min_words_per_sec: float) -> list:
    """Сегменты, где модель сорвалась: длинные и почти без слов."""
    bad = []
    for seg in segments:
        try:
            dur = float(seg.get('end', 0.0)) - float(seg.get('start', 0.0))
        except (TypeError, ValueError):
            continue
        if dur < min_duration:
            continue
        n_words = len(seg.get('text', '').split())
        if dur > 0 and (n_words / dur) < min_words_per_sec:
            bad.append({'start': float(seg.get('start', 0.0)),
                        'end': float(seg.get('end', 0.0)),
                        'words': n_words, 'dur': dur})
    return bad


def words_in_range(words: list, start: float, end: float) -> str:
    """Слова из words[], попадающие в интервал времени."""
    picked = []
    for w in words:
        try:
            w_start = float(w.get('start', -1))
        except (TypeError, ValueError):
            continue
        if start <= w_start < end:
            token = (w.get('word') or '').strip()
            if token:
                picked.append(token)
    return ' '.join(picked)


def text_from_response(result: dict, min_duration: float, min_words_per_sec: float) -> tuple[str, list]:
    """Собирает текст: пунктуация из segments[], потерянное — из words[]."""
    words = result.get('words') or []
    segments = result.get('segments') or []
    degenerate = find_degenerate_segments(segments, min_duration, min_words_per_sec)
    degenerate_spans = {(d['start'], d['end']) for d in degenerate}

    if not segments:
        if words:
            return ' '.join((w.get('word') or '').strip()
                            for w in words if (w.get('word') or '').strip()), degenerate
        return (result.get('text') or '').strip(), degenerate

    pieces = []
    prev_end = 0.0
    for seg in segments:
        try:
            s_start = float(seg.get('start', 0.0))
            s_end = float(seg.get('end', 0.0))
        except (TypeError, ValueError):
            s_start = s_end = 0.0
        seg_text = (seg.get('text') or '').strip()

        if words and s_start > prev_end + 0.5:
            gap = words_in_range(words, prev_end, s_start)
            if gap:
                pieces.append(gap)

        if (s_start, s_end) in degenerate_spans or not seg_text:
            recovered = words_in_range(words, s_start, s_end) if words else ''
            if recovered:
                pieces.append(recovered)
            elif seg_text:
                pieces.append(seg_text)
            else:
                # Сегмент признан сорванным (или пуст изначально), а words[] для этого
                # интервала тоже пуст — кусок речи потерян безвозвратно, восстановить
                # нечем (см. живой случай 16.08.26, agent.md). Раньше это происходило
                # молча, без следа ни в тексте, ни в логе — неотличимо от "тут и правда
                # была тишина". Вместо попытки угадать/восстановить (риск которой уже
                # виден на живом эксперименте с keywords-буст — модель начинает уверенно
                # галлюцинировать) — тот же принцип, что уже работает в chunking.join_chunks
                # для целиком потерянных кусков: видимая метка в тексте, не догадка.
                mark = f"[не распознано {fmt_mmss(s_start)}]"
                pieces.append(mark)
                logger.warning("density_gate: сегмент %.1f-%.1fс потерян целиком — "
                               "ни text, ни words[] для этого интервала, вставлена метка %s",
                               s_start, s_end, mark)
        elif seg_text:
            pieces.append(seg_text)

        prev_end = max(prev_end, s_end)

    if words:
        tail = words_in_range(words, prev_end, float('inf'))
        if tail:
            pieces.append(tail)

    text = ' '.join(p for p in pieces if p).strip()
    return (text or (result.get('text') or '').strip()), degenerate


def needs_retry(degenerate: list, words: list, min_words_per_sec: float) -> bool:
    """Стоит ли переспрашивать окно, которое модель провалила."""
    for d in degenerate:
        span = d['end'] - d['start']
        if span <= 0:
            continue
        recovered = sum(1 for w in words
                        if d['start'] <= float(w.get('start', -1)) < d['end'])
        if (recovered / span) < min_words_per_sec:
            return True
    return False
