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


def latin_terms(text: str) -> list[str]:
    out = []
    for m in LATIN_RE.finditer(text):
        tok = m.group(0).strip(".-_")
        if len(tok) < 2 or tok.lower() in STOP:
            continue
        out.append(tok)
    return out


def collect_texts() -> tuple[list[str], list[str]]:
    """(тексты пользователя, курируемые документы)."""
    user_texts: list[str] = []
    for db, query in (
        (AUTH_DB, "select text from voice_transcripts where text is not null"),
        (SEARCH_DB, "select body from chat_docs"),
    ):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            user_texts += [r[0] for r in c.execute(query) if r[0]]
            c.close()
        except sqlite3.Error as e:
            print(f"  ! {db}: {e}", file=sys.stderr)
    docs: list[str] = []
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
    base_terms: set[str] = set()
    canon: dict[str, str] = {}
    try:
        from speech_engine.profiles import DICTATION
        from speech_engine.terms import TERM_CANON
        base_terms = {_norm(t) for t in DICTATION.deepgram_params.get("keyterm", [])}
        canon = {_norm(k): v for k, v in TERM_CANON.items()}
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
    canon_norms = set(canon) | base_terms

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

        if key_n in canon:
            form = canon[key_n]
        else:
            # Наблюдаемое искажение известного термина → авто-правка, не термин.
            hit = next((c for c in canon_norms if _dist1(key_n, c)), None)
            if hit and hit in canon:
                cnt = total + sum(cc.values())
                if MIN_FIX_COUNT <= cnt <= MAX_FIX_COUNT \
                        and len(key_n) >= 5 and key_n not in FIX_DENY:
                    fixes[key_n] = {"fix": canon[hit], "count": cnt}
                continue
            elif hit:
                continue  # вариант базового термина — не новое слово

        ref[key_n] = {
            "form": form,
            "count": total + sum(cc.values()),
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
