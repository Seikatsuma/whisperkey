"""Нарезка длинных записей и склейка кусков — два независимых, по-разному устроенных
алгоритма для двух профилей. Домен решает форму нарезки, как и выбор движка:

`split_audio`/`join_chunks` (dictation) — работает с numpy-массивом в памяти (микрофон),
режет только если запись длиннее `chunk_threshold_seconds` (в норме диктовка НЕ режется
вовсе — см. `agent.md` WhisperKey, «порог нарезки 15 минут»), перекрытие снимается
по таймкодам слов с точным текстовым сравнением как запасным путём.
Перенесено дословно из WhisperKey `whisperkey.py` (`_split_audio`, `_norm_word`,
`_drop_overlap`, `_fmt_mmss`, `_overlap_word_count`, `_join_chunks`).

`plan_chunks` (calls) — работает с длительностью файла на диске (transcribe-bot режет
ffmpeg-ом по таймкодам, а не в памяти), режется ВСЕГДА кусками по `chunk_size_seconds`
с перекрытием `chunk_overlap_seconds` на каждую сторону. Перенесено из
`~/transcribe-bot/transcribe_core.py` (`ChunkPlan`, `plan_chunks`, `MIN_TAIL_SEC`).

`plan_chunks_shifted` — сдвинутая сетка нарезки для голосования профиля calls.
Реализация здесь НЕ является портом прототипа исследователя (`run_variant.py`
существовал только в scratch-каталоге агента-исследователя и не сохранился на диске
— см. `~/vault/tasks/2026-08-09-transcribe-bot-quality-research.md`, где описан как
"5 несложных строк монки-патча"). Реализация ниже написана заново по словесному
описанию исследования ("сетка со сдвигом на CHUNK_SEC/2") и ПЕРЕПРОВЕРЕНА замером
в этой сессии (см. agent.md, раздел «Профиль calls» и отчёт о сдаче) — числа
исследования воспроизведены на тех же двух эталонных окнах перед тем, как эта
реализация была принята как источник профиля calls по умолчанию.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# ─── dictation: numpy-массив в памяти ──────────────────────────────────────────

def split_audio(audio, dur: float, sample_rate: int,
                 threshold_seconds: float, size_seconds: float, overlap_seconds: float):
    """Режет запись на куски с перекрытием. Короткие записи не режутся вовсе."""
    if dur <= threshold_seconds:
        return [audio], [0.0]

    size = int(sample_rate * size_seconds)
    overlap = int(sample_rate * overlap_seconds)
    step = max(size - overlap, size // 2)

    chunks, offsets = [], []
    pos = 0
    while pos < len(audio):
        end = min(pos + size, len(audio))
        chunks.append(audio[pos:end])
        offsets.append(pos / sample_rate)
        if end >= len(audio):
            break
        pos += step

    return chunks, offsets


def norm_word(w: str) -> str:
    return re.sub(r'[^\w]', '', w.lower().replace('ё', 'е'))


def drop_overlap(prev_text: str, next_text: str, max_probe: int = 25) -> str:
    """Убирает из next_text повтор, попавший в него из зоны перекрытия. Только точное совпадение."""
    prev_words = prev_text.split()
    next_words = next_text.split()
    if len(prev_words) < 3 or len(next_words) < 3:
        return next_text

    limit = min(max_probe, len(prev_words), len(next_words))
    for n in range(limit, 2, -1):
        tail = [norm_word(w) for w in prev_words[-n:]]
        head = [norm_word(w) for w in next_words[:n]]
        if tail == head and any(tail):
            return ' '.join(next_words[n:])
    return next_text


def fmt_mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def overlap_word_count(words: list, chunk_start: float, cut_before: float) -> int:
    """Сколько первых слов куска попало в зону перекрытия с предыдущим (по таймкодам)."""
    n = 0
    for w in words:
        try:
            abs_start = chunk_start + float(w.get('start', 0.0))
        except (TypeError, ValueError):
            break
        if abs_start >= cut_before:
            break
        if (w.get('word') or '').strip():
            n += 1
    return n


def join_chunks(parts: list, quality: list, offsets: list,
                 words_per_chunk: list | None = None) -> tuple[str, list]:
    """Склеивает куски, помечая потерянные меткой [не распознано MM:SS]."""
    pieces: list[str] = []
    lost_marks: list[str] = []
    prev_end: float | None = None

    for idx, text in enumerate(parts):
        if not text:
            if len(parts) > 1:
                mark = f"[не распознано {fmt_mmss(offsets[idx])}]"
                lost_marks.append(mark)
                pieces.append(mark)
            continue

        chunk_words = (words_per_chunk[idx] if words_per_chunk and idx < len(words_per_chunk)
                       else None)
        if pieces and prev_end is not None and chunk_words:
            n_dup = overlap_word_count(chunk_words, offsets[idx], prev_end)
            words_of_text = text.split()
            if 0 < n_dup < len(words_of_text):
                text = ' '.join(words_of_text[n_dup:])
        elif pieces:
            text = drop_overlap(pieces[-1], text)

        if chunk_words:
            try:
                prev_end = offsets[idx] + max(float(w.get('end', 0.0)) for w in chunk_words)
            except (TypeError, ValueError):
                prev_end = None

        if text:
            pieces.append(text)

    return ' '.join(p for p in pieces if p).strip(), lost_marks


# ─── calls: длительность файла на диске, ffmpeg режет отдельно ────────────────

MIN_TAIL_SEC = 0.35   # огрызок короче — приклеиваем к предыдущему куску


@dataclass
class ChunkPlan:
    idx: int
    own_start: float          # зона ответственности — из неё берутся слова
    own_end: float
    audio_start: float        # что реально отправляем модели (с перекрытием)
    audio_end: float


def plan_chunks(duration: float, chunk_sec: float, overlap: float,
                 shift: float = 0.0, min_tail_sec: float = MIN_TAIL_SEC) -> list[ChunkPlan]:
    """Зоны ответственности встык, аудио-окна с перекрытием в обе стороны.

    shift=0.0 — штатная сетка (как в проде transcribe-bot, `plan_chunks`).
    shift>0.0 — сетка со сдвигом: первая зона короче (0..shift), остальные —
    обычного размера начиная с shift. Используется вторым голосом при голосовании
    (профиль calls, `voting_enabled=True`) — независимая точка отсчёта нарезки ловит
    срывы декодирования, которые проваливаются РОВНО на границе штатной сетки.
    """
    plans: list[ChunkPlan] = []
    edges: list[float] = []

    shift = shift % chunk_sec if shift else 0.0
    if shift > 0.0:
        edges.append(0.0)
        t = shift
    else:
        t = 0.0

    while t < duration:
        edges.append(t)
        t += chunk_sec
    edges.append(duration)

    # хвост короче min_tail_sec приклеиваем к предыдущей зоне
    if len(edges) >= 3 and edges[-1] - edges[-2] < min_tail_sec:
        edges.pop(-2)
    # то же для искусственно короткой первой зоны сдвига, если сдвиг случайно мал
    if len(edges) >= 3 and shift > 0.0 and edges[1] - edges[0] < min_tail_sec:
        edges.pop(1)

    for i in range(len(edges) - 1):
        own_s, own_e = edges[i], edges[i + 1]
        plans.append(ChunkPlan(
            idx=i, own_start=own_s, own_end=own_e,
            audio_start=max(0.0, own_s - overlap),
            audio_end=min(duration, own_e + overlap),
        ))
    return plans


def plan_chunks_shifted(duration: float, chunk_sec: float, overlap: float,
                         fraction: float = 0.5) -> list[ChunkPlan]:
    """Удобный вход: сдвиг задаётся долей chunk_sec (по умолчанию половина шага)."""
    return plan_chunks(duration, chunk_sec, overlap, shift=chunk_sec * fraction)
