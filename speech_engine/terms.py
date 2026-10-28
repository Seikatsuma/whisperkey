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
    # Словарь Егора 20.10.26 — его имена и названия, чтобы whisper/Deepgram
    # писали их точно (юджайл, qlotui, мест система и т.п. со скриншотов).
    "yougile": "YouGile", "ui": "UI", "salutespeech": "SaluteSpeech",
    "telemost": "Телемост", "wispr": "Wispr", "flow": "Flow",
    "brave": "Brave", "zoom": "Zoom", "miro": "Miro",
    "termius": "Termius", "ideogram": "Ideogram", "devin": "Devin",
    "sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku",
    "camoufox": "Camoufox", "patchright": "Patchright",
    "amocrm": "amoCRM",
}

TERM_FIX = [
    (r"\b(?:виспер\s?кей|висперкей|виспро[\s.]?кей|wispro[\s.]?ke[yй]|wisperk|whisper\s?key|wisper\s?key|visperkey)\b", "WhisperKey"),
    (r"\bвиспр[оа]\b", "WhisperKey"),
    (r"\bвиспер\b", "WhisperKey"),
    (r"\bwhisperer[-\s]?k(?:ey)?\b", "WhisperKey"),
    (r"\b(?:jiminy|джимм?и|джемини|гемини)\b", "Gemini"),
    # «клод юай» → модель слышит CloudUI/code UI (архив 20-23.10: обе формы
    # в диктовках Егора). Латинское «code UI» правим — в его контуре это всегда
    # продукт; кириллическое «код UI» остаётся дословным.
    (r"\bcloudui\b", "Claude UI"),
    (r"\bcode\s*ui\b", "Claude UI"),  # латинское слышание «Claude UI» (архив 22-23.10);
                                    # кириллическое «код UI» не трогаем — может быть дословным
    (r"\bкл[оа]уд\w*\b", "Claude"),
    (r"\bклод\b", "Claude"),
    (r"\bklot\b", "Claude"),
    (r"\bигрок\s*[qQ]\b", "Groq"),
    (r"\badaxmarket\b", "Яндекс Маркет"),
    (r"\b(?:valberis|валберис|вайлдберриз|вайлдберис)\b", "Wildberries"),
    (r"\b(?:озон|ozon)\b", "Ozon"),
    (r"\bюджайл\w*\b", "Yougile"),
    (r"\bгитхаб\w*\b", "GitHub"),
    (r"\bдипграм\w*\b", "DeepGram"),
    (r"\bтелеграм\b", "Telegram"),
    (r"\bseo\b", "CEO"),
    (r"\bокистратор(\w*)\b", r"оркестратор\1"),
    (r"\bартистратор(\w*)\b", r"оркестратор\1"),
    (r"\bмного(?:ген|гент)(ную|ная|ной|ные|ных|ное)\b", r"многоагент\1"),
    (r"\bмаскриб(\w*)\b", r"транскриб\1"),            # живой случай 04.10.26: «mascribator» в диктовке Егора
    (r"\bmascrib\w*\b", "транскрибатор"),
    # Живые сбои со скриншотов Егора 20.10.26 + его словарь.
    (r"\b(?:q[\s.]?lotui|клотуи|клод\s*ю\s*ай|claude\s*ui)\b", "Claude UI"),
    (r"\b(?:юджайл|ugile|югайл)\w*\b", "YouGile"),
    (r"\b(?:мест[\s-]систем\w*)\b", "мест-система"),
    (r"\b(?:телемост|telemost)\b", "Телемост"),
    (r"\b(?:виспр|wispr)\s*флоу\b", "Wispr Flow"),
    (r"\b(?:салют\s*спич|salute\s*speech)\b", "SaluteSpeech"),
    (r"\b(?:брейв|brave)\b", "Brave"),
    (r"\b(?:текст\s*едит|textedit)\b", "TextEdit"),
    (r"\bдевин\w*\b", "Devin"),
    (r"\b(?:мира|miro)\b", "Miro"),
    (r"\b(?:зум|zoom)\b", "Zoom"),
    (r"\b(?:термиус|termius)\b", "Termius"),
    (r"\b(?:идеограм|ideogram)\w*\b", "Ideogram"),
    (r"\b(?:соннет|sonnet)\b", "Sonnet"),
    (r"\b(?:опус|opus)\b", "Opus"),
    (r"\b(?:хайку|haiku)\b", "Haiku"),
    (r"\b(?:камуфокс|camoufox)\b", "Camoufox"),
    (r"\b(?:патчрайт|patchright)\b", "Patchright"),
    (r"\b(?:зарайск)\b", "Зарайск"),
    (r"\b(?:сан\s*школ\w*|саншкол\w*)\b", "СанШкола"),
    (r"\b(?:артрайд|artraid)\b", "Артрейд"),
    (r"\b(?:строчков|строков)\w*\b", "Строков"),
    # «де плой» — nova-3 разбивает слово пробелом (жалоба 28.10.26). Плюс
    # латинское «deploy» к его стандартной кириллической форме.
    (r"\bде\s*плой\w*\b", "деплой"),
    (r"\bдиплой\w*\b", "деплой"),
    (r"\bdeploy\b", "деплой"),
]

_TERM_FIX_RX: list = []   # компилируется при первом вызове
_LEARNED_FIX = {"mtime": 0.0, "rx": []}  # data/term_fixes.json, кэш по mtime


def _learned_fixes():
    """Авто-правки наблюдаемых искажений из tools/build_reference.py —
    cloudui→Claude UI, amacrm→amoCRM. Файл обновляется ночным сборщиком;
    читаем по mtime, без перезапуска. Правится только целое слово ≥5 симв."""
    import json as _json
    from pathlib import Path as _P
    path = _P(__file__).resolve().parent.parent / "data" / "term_fixes.json"
    try:
        mt = path.stat().st_mtime
    except OSError:
        return _LEARNED_FIX["rx"]
    if mt != _LEARNED_FIX["mtime"]:
        rx = []
        try:
            for k, v in _json.loads(path.read_text(encoding="utf-8"))["fixes"].items():
                if len(k) >= 5:
                    rx.append((re.compile(r"\b" + re.escape(k) + r"\b", re.IGNORECASE), v["fix"]))
        except (OSError, _json.JSONDecodeError, KeyError):
            pass
        _LEARNED_FIX.update(mtime=mt, rx=rx)
    return _LEARNED_FIX["rx"]


def _transfer_norm(w: str) -> str:
    return re.sub(r'[^\w]', '', w, flags=re.UNICODE).lower()


def fix_known_terms(text: str) -> tuple[str, int]:
    """Заменяет названия, записанные по звучанию, на правильные. Без сети."""
    if not text:
        return text, 0
    if not _TERM_FIX_RX:
        _TERM_FIX_RX.extend((re.compile(p, re.IGNORECASE), r) for p, r in TERM_FIX)
    n = 0
    for rx, rep in _TERM_FIX_RX + _learned_fixes():
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
