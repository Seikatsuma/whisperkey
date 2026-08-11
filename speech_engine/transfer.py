"""Перенос пунктуации и окончаний со второго ("стилевого") прохода.

Перенесено дословно из WhisperKey `whisperkey.py` (`transfer_punctuation`,
`transfer_endings`). Используется только профилем dictation — calls второго
прохода не делает (латентность там некритична, но второй проход в WhisperKey
не про латентность, а про STYLE_PROMPT, специфичный для диктовки одного голоса;
на звонках с несколькими говорящими эта техника не проверялась и не считается
частью профиля calls).

Инвариант обеих функций: слово может поменять пунктуацию/окончание, но не может
стать другим словом и не может появиться из ниоткуда. Это проверяется прямо
в теле функций (assert через сравнение нормализованных списков слов) — см.
`~/whisperkey/Whisper на MAC/agent.md`.
"""
from __future__ import annotations

import difflib

from .terms import _transfer_norm

_TRANSFER_PUNCT = '.,!?;:—…'
ENDING_MIN_STEM = 4   # короче — слишком легко совпасть случайно


def transfer_punctuation(base_text: str, styled_text: str) -> str:
    """Слова из base_text, знаки препинания — из styled_text."""
    if not base_text or not styled_text:
        return base_text

    a, b = base_text.split(), styled_text.split()
    na = [_transfer_norm(x) for x in a]
    nb = [_transfer_norm(x) for x in b]
    out = list(a)

    raised = set()
    for i, j, n in difflib.SequenceMatcher(None, na, nb, autojunk=False).get_matching_blocks():
        for k in range(n):
            src, dst = b[j + k], out[i + k]
            if not src or not dst:
                continue
            tail = ''.join(c for c in src[len(src.rstrip(_TRANSFER_PUNCT)):]
                           if c in _TRANSFER_PUNCT)
            if tail and dst[-1] not in _TRANSFER_PUNCT:
                out[i + k] = dst + tail
            prev_styled = b[j + k - 1] if (j + k) else ''
            if (src[0].isupper() and dst[0].islower()
                    and ((j + k) == 0 or (prev_styled and prev_styled[-1] in '.!?…'))):
                out[i + k] = out[i + k][0].upper() + out[i + k][1:]
                raised.add(i + k)

    for i, t in enumerate(out):
        if not t:
            continue
        prev = out[i - 1] if i else ''
        starts = (i == 0) or (prev and prev[-1] in '.!?…')
        if starts and t[0].islower():
            out[i] = t[0].upper() + t[1:]
        elif not starts and i in raised:
            out[i] = t[0].lower() + t[1:]

    result = ' '.join(out)
    if [_transfer_norm(x) for x in result.split()] != na:
        return base_text
    return result


def transfer_endings(base_text: str, donor_text: str) -> tuple[str, int]:
    """Правит окончание слова по донорскому проходу. Слово другим стать не может."""
    if not base_text or not donor_text:
        return base_text, 0
    a, b = base_text.split(), donor_text.split()
    na = [_transfer_norm(x) for x in a]
    nb = [_transfer_norm(x) for x in b]
    out, used = list(a), 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb, autojunk=False).get_opcodes():
        if tag != 'replace':
            continue
        for k in range(min(i2 - i1, j2 - j1)):
            x, y = na[i1 + k], nb[j1 + k]
            if x == y or len(x) < ENDING_MIN_STEM or len(y) < ENDING_MIN_STEM:
                continue
            common = 0
            for c1, c2 in zip(x, y):
                if c1 != c2:
                    break
                common += 1
            if common >= ENDING_MIN_STEM and common >= min(len(x), len(y)) - 3:
                out[i1 + k] = b[j1 + k]
                used += 1
    return ' '.join(out), used
