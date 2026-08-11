"""Выравнивание двух текстов по словам — трёхпроходный алгоритм.

Портировано дословно из `~/transcribe-bot/eval/evaluate.py` (`normalize_word`, `align`,
`Alignment`, `stem`, `similar`). Там это было "только для замеров" (см. agent.md
transcribe-bot: "измерители, не участвуют в работе бота"), но профилю calls тот же
алгоритм нужен В БОЮ — голосование (`vote.py`) находит несовпавшие участки между
основным и сдвинутым проходом ровно этим выравниванием. Вынесено сюда, чтобы не
дублировать код между измерителем и продом (вопрос, поднятый самим исследованием,
см. `2026-08-09-transcribe-bot-quality-research.md`, раздел «Что дальше»).

`~/transcribe-bot/eval/evaluate.py` можно (но не обязательно в рамках этой сдачи)
перевести на импорт отсюда вместо своей копии — не сделано в этом проходе, чтобы
не трогать измерительный инструмент без необходимости (правило "чужого не касаться
за пределами прямой задачи"); см. отчёт о сдаче, раздел "не сделано".
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

_PUNCT_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_MULTI_DASH_RE = re.compile(r"-{2,}")


def normalize_word(w: str) -> str:
    """Слово -> сравнимая форма: нижний регистр, ё->е, без пунктуации, без растяжек."""
    w = w.lower().replace("ё", "е")
    w = _PUNCT_RE.sub("", w)
    w = _MULTI_DASH_RE.sub("-", w).strip("-")
    w = re.sub(r"(.)\1{2,}", r"\1\1", w)
    return w


def stem(w: str) -> str:
    """Грубая основа для сопоставления морфологических вариантов."""
    return w[:5] if len(w) >= 6 else w


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()


@dataclass
class Alignment:
    ref_to_hyp: list  # для каждого слова ref — индекс в hyp либо None
    hyp_to_ref: list
    exact: int = 0
    fuzzy: int = 0


def align(ref: list[str], hyp: list[str]) -> Alignment:
    """ref/hyp — уже НОРМАЛИЗОВАННЫЕ слова (см. normalize_word). Трёхпроходное сопоставление:
    1) точные совпадения (difflib), 2) совпадение по основам внутри разрывов,
    3) жадный добор по символьному сходству >= 0.72 внутри остатков."""
    ref_to_hyp: list = [None] * len(ref)
    hyp_to_ref: list = [None] * len(hyp)
    a = Alignment(ref_to_hyp, hyp_to_ref)

    sm = difflib.SequenceMatcher(a=ref, b=hyp, autojunk=False)
    gaps: list[tuple[int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                ref_to_hyp[i1 + k] = j1 + k
                hyp_to_ref[j1 + k] = i1 + k
            a.exact += i2 - i1
        else:
            if i2 > i1 or j2 > j1:
                gaps.append((i1, i2, j1, j2))

    leftovers: list[tuple[int, int, int, int]] = []
    for i1, i2, j1, j2 in gaps:
        rs = [stem(w) for w in ref[i1:i2]]
        hs = [stem(w) for w in hyp[j1:j2]]
        if not rs or not hs:
            leftovers.append((i1, i2, j1, j2))
            continue
        sm2 = difflib.SequenceMatcher(a=rs, b=hs, autojunk=False)
        for tag, x1, x2, y1, y2 in sm2.get_opcodes():
            if tag == "equal":
                for k in range(x2 - x1):
                    ri, hj = i1 + x1 + k, j1 + y1 + k
                    ref_to_hyp[ri] = hj
                    hyp_to_ref[hj] = ri
                a.fuzzy += x2 - x1
            else:
                leftovers.append((i1 + x1, i1 + x2, j1 + y1, j1 + y2))

    for i1, i2, j1, j2 in leftovers:
        free_h = [j for j in range(j1, j2) if hyp_to_ref[j] is None]
        for ri in range(i1, i2):
            if ref_to_hyp[ri] is not None:
                continue
            best, best_score = None, 0.0
            for hj in free_h:
                if hyp_to_ref[hj] is not None:
                    continue
                sc = similar(ref[ri], hyp[hj])
                if sc > best_score:
                    best, best_score = hj, sc
            if best is not None and best_score >= 0.72:
                ref_to_hyp[ri] = best
                hyp_to_ref[best] = ri
                a.fuzzy += 1

    return a
