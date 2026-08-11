"""Таблица известных названий и словарный перенос из второго прохода.

Перенесено дословно из WhisperKey `whisperkey.py` (функции `fix_known_terms`,
`transfer_terms`, `_transfer_norm`, константы `TERM_FIX`, `TERM_CANON`) — см.
`~/whisperkey/Whisper на MAC/agent.md`, разделы «Таблица названий» и «Названия
продуктов подставляются из второго прохода». Замеры и обоснования там же, здесь
не дублируются.

`fix_known_terms` работает всегда и без сети (чистые регулярки) — используется
и профилем dictation, и профилем calls. `transfer_terms` требует второго прохода
(`STYLE_PROMPT`) и в профиле calls не используется (calls вообще не делает второго
стилевого прохода — см. `dictation.py` против `calls.py`).
"""
from __future__ import annotations

import difflib
import re

# Белый список: ТОЛЬКО эти слова могут приехать из второго прохода в текст.
TERM_CANON = {
    "claude": "Claude", "code": "Code", "design": "Design",
    "whisperkey": "WhisperKey", "notebook": "Notebook", "lm": "LM",
    "github": "GitHub", "telegram": "Telegram", "groq": "Groq",
    "deepgram": "DeepGram", "ceo": "CEO", "ipad": "iPad",
    "ozon": "Ozon", "wildberries": "Wildberries",
}

TERM_FIX = [
    (r"\b(?:виспер\s?кей|висперкей|виспро[\s.]?кей|wispro[\s.]?ke[yй]|wisperk|whisper\s?key)\b", "WhisperKey"),
    (r"\bвиспр[оа]\b", "WhisperKey"),
    (r"\bвиспер\b", "WhisperKey"),
    (r"\b(?:jiminy|джимм?и|джемини|гемини)\b", "Gemini"),
    (r"\bкл[оа]уд\w*\b", "Claude"),
    (r"\bклод\b", "Claude"),
    (r"\badaxmarket\b", "Яндекс Маркет"),
    (r"\b(?:valberis|валберис|вайлдберриз|вайлдберис)\b", "Wildberries"),
    (r"\b(?:озон|ozon)\b", "Ozon"),
    (r"\bюджайл\w*\b", "Yougile"),
    (r"\bгитхаб\w*\b", "GitHub"),
    (r"\bдипграм\w*\b", "DeepGram"),
    (r"\bтелеграм\b", "Telegram"),
    (r"\bseo\b", "CEO"),
]

_TERM_FIX_RX: list = []   # компилируется при первом вызове


def _transfer_norm(w: str) -> str:
    return re.sub(r'[^\w]', '', w, flags=re.UNICODE).lower()


def fix_known_terms(text: str) -> tuple[str, int]:
    """Заменяет названия, записанные по звучанию, на правильные. Без сети."""
    if not text:
        return text, 0
    if not _TERM_FIX_RX:
        _TERM_FIX_RX.extend((re.compile(p, re.IGNORECASE), r) for p, r in TERM_FIX)
    n = 0
    for rx, rep in _TERM_FIX_RX:
        text, k = rx.subn(rep, text)
        n += k
    return text, n


def transfer_terms(base_text: str, vocab_text: str) -> tuple[str, int]:
    """Подставляет названия продуктов из словарного прохода. Пословно, только из TERM_CANON."""
    if not base_text or not vocab_text:
        return base_text, 0
    a, b = base_text.split(), vocab_text.split()
    na = [_transfer_norm(x) for x in a]
    nb = [_transfer_norm(x) for x in b]
    out, used = list(a), 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb, autojunk=False).get_opcodes():
        if tag != 'replace':
            continue
        for k in range(min(i2 - i1, j2 - j1)):
            canon = TERM_CANON.get(nb[j1 + k])
            if canon:
                out[i1 + k] = canon
                used += 1
    return ' '.join(out), used
