"""Голосование профиля calls: primary (штатная сетка) + voter (сдвинутая сетка),
слияние по выравниванию текстов — НЕ по "больше слов = лучше".

Источник алгоритма: `~/vault/tasks/2026-08-09-transcribe-bot-quality-research.md`,
раздел «Кандидат 1». Наивная схема "больше слов = лучше" уже была отвергнута в
самом transcribe-bot раньше (`eval/merge_runs.py`: 89.3% против 92.1% у лучшего
одиночного прохода, протащенная галлюцинация-петля) — этот модуль её НЕ повторяет.

Прототип исследования (`vote_merge_v2.py`) существовал только в scratch-каталоге
агента-исследователя и не сохранился на диске. Реализация ниже написана заново по
словесному описанию исследования и адаптирована под `Word`-датаклассы `speech_engine`
(было — сырые JSON-словари), как и просил бриф унификации. Числа исследования
(+1.9 п.п. на окне 1, +0.4 п.п. на окне 2) ПЕРЕПРОВЕРЕНЫ этой реализацией — см.
agent.md пакета и отчёт о сдаче, а не приняты на веру.

Пороги `MIN_RUN`/`SPARSITY_RATIO` в исследовании — "быстрая прикидка, не подобранная
замером в стиле остальных констант прод-кода" (дословно из источника). Значения
по умолчанию здесь совпадают с исследованием (2 и 0.6), но помечены как кандидаты
на будущий sweep, не как измеренные пороги.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .align import align, normalize_word
from .types import Word

# ─── has_loop — порт transcribe_core.py::has_loop (детектор зацикливания) ──────
# Транскрибе-бот использует СВОЮ версию (`transcribe_core.has_loop`) как единственный
# источник истины в проде — если модуль подключается ИЗ transcribe-bot, передавайте
# её явным параметром `has_loop_fn`, чтобы не разойтись с прод-версией. Версия ниже —
# для консумеров, у которых своей нет.
MIN_WORDS_FOR_TTR = 12
LOOP_TTR = 0.55
LOOP_MIN_REPS = 4


def _loop_norm(w: str) -> str:
    return re.sub(r"[^\w-]", "", w.lower().replace('ё', 'е'), flags=re.UNICODE)


def has_loop(text: str) -> bool:
    """Порт `transcribe_core.py::has_loop`. См. там подробное обоснование порогов."""
    words = [w for w in (_loop_norm(t) for t in text.split()) if w]
    if not words:
        return False
    for n in range(1, 7):
        for i in range(len(words) - n * LOOP_MIN_REPS + 1):
            tok = words[i:i + n]
            if all(words[i + n * k: i + n * (k + 1)] == tok for k in range(1, LOOP_MIN_REPS)):
                return True
    if len(words) >= MIN_WORDS_FOR_TTR and len(set(words)) / len(words) < LOOP_TTR:
        return True
    for n in range(4, 9):
        for i in range(len(words) - 2 * n + 1):
            if words[i:i + n] == words[i + n:i + 2 * n]:
                return True
    return False


# ─── конвертация Groq words[] <-> Word ──────────────────────────────────────────

def words_from_groq(raw_words: list[dict]) -> list[Word]:
    out = []
    for w in raw_words or []:
        text = (w.get('word') or '').strip()
        if not text:
            continue
        try:
            start, end = float(w.get('start', 0.0)), float(w.get('end', 0.0))
        except (TypeError, ValueError):
            continue
        out.append(Word(text=text, start=start, end=end))
    return out


def words_to_text(words: list[Word]) -> str:
    return ' '.join(w.text for w in words)


@dataclass
class VoteStats:
    runs_found: int = 0
    runs_applied: int = 0
    words_added: int = 0
    words_removed: int = 0


def vote_merge(primary: list[Word], voter: list[Word], *,
               min_run: int = 2, sparsity_ratio: float = 0.6,
               has_loop_fn=None) -> tuple[list[Word], VoteStats]:
    """primary берётся целиком; voter подставляется ТОЛЬКО там, где:
      1. выравнивание находит у voter'а подряд идущий кусок слов (длиной >= min_run)
         без пары в primary,
      2. primary в ЭТОМ ЖЕ временнОм окне заметно беднее (<= sparsity_ratio от объёма
         voter'а в словах),
      3. текст voter'а на этом участке не зациклен (has_loop_fn).
    Обобщение прод-детектора `_rescue_interval` ("переспросить и заменить целиком,
    только если плотнее и без петли") на источник "offline-прогон другого голоса"
    вместо "повторный запрос того же движка".
    """
    check_loop = has_loop_fn or has_loop
    stats = VoteStats()

    if not voter:
        return list(primary), stats
    if not primary:
        stats.runs_found = 1
        stats.runs_applied = 1
        stats.words_added = len(voter)
        return list(voter), stats

    na = [normalize_word(w.text) for w in primary]
    nb = [normalize_word(w.text) for w in voter]
    a = align(na, nb)

    runs: list[tuple[int, int]] = []
    i, n = 0, len(voter)
    while i < n:
        if a.hyp_to_ref[i] is None:
            j = i
            while j < n and a.hyp_to_ref[j] is None:
                j += 1
            if j - i >= min_run:
                runs.append((i, j))
            i = j
        else:
            i += 1
    stats.runs_found = len(runs)

    applied: list[tuple[float, float, list[Word]]] = []
    for j1, j2 in runs:
        run_words = voter[j1:j2]
        if check_loop(words_to_text(run_words)):
            continue
        win_start, win_end = run_words[0].start, run_words[-1].end
        primary_in_window = [w for w in primary if win_start <= w.start < win_end]
        if len(primary_in_window) > sparsity_ratio * (j2 - j1):
            continue
        applied.append((win_start, win_end, run_words))

    if not applied:
        return list(primary), stats

    merged = list(primary)
    for win_start, win_end, run_words in applied:
        before = len(merged)
        merged = [w for w in merged if not (win_start <= w.start < win_end)] + list(run_words)
        stats.words_removed += before - (len(merged) - len(run_words))
        stats.words_added += len(run_words)
    merged.sort(key=lambda w: w.start)
    stats.runs_applied = len(applied)

    return merged, stats
