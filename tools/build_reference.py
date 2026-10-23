#!/usr/bin/env python3
"""Эталонная база терминов из реальных текстов Егора (23.10.26).

Зачем: минер mine_terms.py раньше решал судьбу кандидата по мнению второго
ASR (Groq). Егор 23.10: «неправильно отдавать всё двум транскрибаторам —
эффективнее сравнивать с реальной базой и реальными названиями». Этот скрипт
собирает такую базу: термины, как их пишет сам Егор и курируемые документы
сервера.

Источники (только чтение):
  1. Курируемые документы — ~/CLAUDE.md, память, agent.md проектов: там
     написание терминов заведомо верное → форма принимается сразу.
  2. auth.db voice_transcripts.text — продиктованные сообщения (шумно:
     это выход ASR, там бывают искажения) — считается частотой.
  3. chat-search.db chat_docs — тела чатов (набранные + продиктованные
     сообщения) — считается частотой.

Правила отбора (максимальная точность, по требованию Егора):
  - форма из курируемого документа → в базу безусловно;
  - форма из пользовательских текстов → нужно ≥3 вхождений и доминирующая
    доля ≥60% среди всех поверхностных форм того же нормализованного ключа;
  - любой вариант в ±1 символ от TERM_CANON/базового keyterm, но с другим
    написанием → это НАБЛЮДАЕМОЕ ИСКАЖЕНИЕ, а не термин: в базу не идёт,
    зато пишется в data/term_fixes.json как авто-правка (cloudui→Claude UI,
    amacrm→amoCRM), если встретилось ≥2 раз. Так система «слышит code UI —
    знает, что это Claude UI» только по реально наблюдаемым искажениям.

Выход:
  data/reference_terms.json {norm: {form, count, sources}}
  data/term_fixes.json     {wrong_norm: {fix, count}}  — читает terms.py

Запуск:  nice -n 19 ionice -c 3 python3 tools/build_reference.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_REF = REPO / "data" / "reference_terms.json"
OUT_FIX = REPO / "data" / "term_fixes.json"

AUTH_DB = "/home/claude/.cloudcli-shared/auth.db"
SEARCH_DB = "/home/claude/.cloudcli-shared/chat-search.db"
CURATED_DOCS = [
    "/home/claude/CLAUDE.md",
    "/home/claude/.claude/projects/-home-claude/memory/MEMORY.md",
    "/home/claude/speech-engine/agent.md",
    "/home/claude/whisperkey/Whisper на MAC/agent.md",
]
CURATED_GLOBS = [
    "/home/claude/.claude/projects/-home-claude/memory/*.md",
]

LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,}")
STOP = {"the", "a", "i", "ok", "okay", "to", "in", "on", "is", "it", "and",
        "or", "of", "for", "so", "no", "yes", "webm", "wav", "http", "https",
        "www", "com", "ru", "md", "py", "db", "sh", "js", "ts", "json",
        "txt", "api", "utf", "ssh", "tcp", "sudo", "not", "none", "true",
        "false", "self", "def", "if", "else", "elif", "return", "import",
        "from", "print", "str", "int", "len", "set", "dict", "list"}
MIN_USER_COUNT = 3
DOMINANT_SHARE = 0.6
MIN_FIX_COUNT = 2
# Частотная «искажённая» форма — скорее настоящее слово, чем опечатка:
# «codex» ×861 — реальный продукт, не ошибка «Code». Потолок + denylist.
MAX_FIX_COUNT = 100
FIX_DENY = {"codex", "coder", "coded", "codec", "codecs"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _dist1(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    long, short = (a, b) if len(a) > len(b) else (b, a)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))


def _similar(a: str, b: str) -> bool:
    """Слишком похоже на известный термин → искажение, не новое слово.
    dist1 ловит ±1 символ (amacrm→amocrm); для 2-символьных искажений
    (cloudui→claudeui, codeui→claudeui) — порог похожести 0.75 при len≥5."""
    if _dist1(a, b):
        return True
    if min(len(a), len(b)) < 5:
        return False
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


FILE_LIKE = re.compile(
    r"\.(?:py|sh|js|ts|tsx|php|db|md|mdx|json|txt|log|env|service|yml|yaml|"
    r"lock|pem|pub|html|css|gz|zip|session|sqlite)\b|^[0-9a-f-]{8,}$|\d{4,}",
    re.IGNORECASE)


def latin_terms(text: str) -> list[str]:
    out = []
    for m in LATIN_RE.finditer(text):
        tok = m.group(0).strip(".-_")
        if len(tok) < 2 or tok.lower() in STOP:
            continue
        out.append(tok)
    return out


def collect_texts() -> tuple[list[str], list[str]]:
    """(продиктованные тексты Егора, документы для голосования за написание).

    Частота «использует» считается ТОЛЬКО по voice_transcripts — это тексты
    его диктовок, ровно тот домен, что слышит ASR. Тела чатов (chat_docs)
    содержат ответы ассистента (англ. статусы, SQL, код) и идут только в
    счётчик форм для правильного написания — не в частоту."""
    user_texts: list[str] = []
    docs: list[str] = []
    try:
        c = sqlite3.connect(f"file:{AUTH_DB}?mode=ro", uri=True)
        user_texts = [r[0] for r in c.execute(
            "select text from voice_transcripts where text is not null") if r[0]]
        c.close()
    except sqlite3.Error as e:
        print(f"  ! {AUTH_DB}: {e}", file=sys.stderr)
    try:
        c = sqlite3.connect(f"file:{SEARCH_DB}?mode=ro", uri=True)
        docs += [r[0] for r in c.execute("select body from chat_docs") if r[0]]
        c.close()
    except sqlite3.Error as e:
        print(f"  ! {SEARCH_DB}: {e}", file=sys.stderr)
    import glob as _g
    paths = set(CURATED_DOCS)
    for pat in CURATED_GLOBS:
        paths.update(_g.glob(pat))
    for p in sorted(paths):
        try:
            docs.append(Path(p).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return user_texts, docs


def main() -> int:
    sys.path.insert(0, str(REPO))
    targets: dict[str, str] = {}   # норм → каноническая поверхностная форма
    try:
        from speech_engine.profiles import DICTATION
        from speech_engine.terms import TERM_CANON
        targets = {_norm(t): t for t in DICTATION.deepgram_params.get("keyterm", [])}
        targets.update({_norm(k): v for k, v in TERM_CANON.items()})
    except Exception as e:
        print(f"  ! canon/base не поднялись: {e}", file=sys.stderr)

    user_texts, docs = collect_texts()
    print(f"текстов пользователя: {len(user_texts)}, курируемых документов: {len(docs)}")

    # Поверхностные формы по норм-ключу; curated — отдельный доверенный счётчик.
    forms: dict[str, Counter] = {}
    curated: dict[str, Counter] = {}
    for t in user_texts:
        for tok in latin_terms(t):
            forms.setdefault(_norm(tok), Counter())[tok] += 1
    for t in docs:
        for tok in latin_terms(t):
            curated.setdefault(_norm(tok), Counter())[tok] += 1

    ref: dict[str, dict] = {}
    fixes: dict[str, dict] = {}

    for key_n in set(forms) | set(curated):
        total = sum(forms.get(key_n, {}).values())
        fc = forms.get(key_n, Counter())
        cc = curated.get(key_n, Counter())
        if cc:
            form = cc.most_common(1)[0][0]  # доверенное написание
        elif total >= MIN_USER_COUNT:
            form, top = fc.most_common(1)[0]
            if top / total < DOMINANT_SHARE:
                continue
        else:
            continue

        if key_n in targets:
            form = targets[key_n]
        else:
            # Наблюдаемое искажение известного термина → авто-правка, не термин.
            # Отбраковка — по fuzzy-сходству (ловит cloudui к claudeui), но
            # авто-правка генерируется ТОЛЬКО для ±1 символа: fuzzy ловит и
            # составные имена (yougilepy = файл, redesign = слово, designsync =
            # продукт), переписывать их в тексте нельзя. Многосимвольные
            # искажения правятся курируемыми TERM_FIX — там человек подтвердил.
            hit = next((c for c in targets if _similar(key_n, c)), None)
            if hit:
                cnt = total + sum(cc.values())
                if _dist1(key_n, hit) and MIN_FIX_COUNT <= cnt <= MAX_FIX_COUNT \
                        and len(key_n) >= 5 and key_n not in FIX_DENY:
                    fixes[key_n] = {"fix": targets[hit], "count": cnt}
                continue

        if FILE_LIKE.search(form):
            continue  # имя файла/UUID/идентификатор — не термин речи
        all_f = fc + cc
        cap = sum(v for f, v in all_f.items() if f[:1].isupper())
        ref[key_n] = {
            "form": form,
            "count": total + sum(cc.values()),
            "user_count": total,
            "cap_share": round(cap / sum(all_f.values()), 2),
            "sources": (["docs"] if cc else []) + (["user"] if total else []),
        }

    OUT_REF.parent.mkdir(parents=True, exist_ok=True)
    OUT_REF.write_text(json.dumps(
        {"updated": time.strftime("%Y-%m-%d %H:%M:%S"),
         "source": "auth.db voice_transcripts + chat-search.db + курируемые документы",
         "terms": dict(sorted(ref.items(), key=lambda kv: -kv[1]["count"]))},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUT_FIX.write_text(json.dumps(
        {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "fixes": fixes},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"эталонных терминов: {len(ref)} → {OUT_REF}")
    print(f"авто-правок искажений: {len(fixes)} → {OUT_FIX}")
    for k, v in list(fixes.items())[:15]:
        print(f"  ~ {k} → {v['fix']} (×{v['count']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
