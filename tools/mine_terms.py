#!/usr/bin/env python3
"""Минер терминов для самообучения словаря Deepgram (22.10.26).

Проблема: nova-3 на language=ru может выбросить или исковеркать редкий термин
(англ. название, продукт, фамилию). keyterm возвращает такие слова, но список
вручную не пополняется — словарь устаревает относительно речи.

Петля: архив диктовок (аудио + текст Deepgram) → тот же файл через Groq
whisper-large-v3 → латинские термины, которые Groq слышит, а Deepgram в тексте
не имеет → частотный счёт → data/learned_keyterms.json. Движок подмешивает файл
к keyterm на каждый запрос (deepgram_engine._learned_keyterms, кэш по mtime) —
словарь растёт сам, без правки кода и без перезапуска моста.

Безопасность по построению: это только подсказки ASR-модели, итоговый текст не
редактируется; ложный термин в худшем случае — лишний буст, не удаление слов.

Ключи: GROQ_API_KEY из окружения; если не задан — вытаскивается из юнита
voice-bridge.service (systemctl cat), где мост и так работает с этим ключом.

Запуск:  nice -n 19 ionice -c 3 python3 tools/mine_terms.py [--limit 60]
Результат: data/learned_keyterms.json {"terms": [...], "counts": {...}}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
ARCHIVE_DEFAULT = "/home/claude/voice-archive/claude-ui"
OUT_PATH = REPO / "data" / "learned_keyterms.json"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Латинский термин: бренд/продукт — буква, затем буквы/цифры/точки/дефисы.
# Минимум 2 символа, одиночные «a», «I» отсекаются длиной.
LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,}")
# Частые служебные англ. слова, которые реально есть в речи как русские по смыслу —
# не термины, бустить нечего.
STOP = {"the", "a", "i", "ok", "okay", "to", "in", "on", "is", "it", "and",
        "or", "of", "for", "so", "no", "yes", "webm", "wav", "http", "https",
        "talking", "sop"}

_DIGRAPHS = {"ph": "ф", "sh": "ш", "ch": "ч", "ck": "к", "oo": "у", "ee": "и",
             "th": "т", "wh": "в", "qu": "кв", "ya": "я", "yu": "ю", "yo": "ё"}
_LAT2CYR = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф",
            "g": "г", "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л",
            "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
            "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
            "y": "и", "z": "з"}


def lat_to_cyr(token: str) -> str:
    """Грубая транслитерация латиницы — нужна только чтобы отличить
    «термин потерян» от «термин записан кириллицей» (prompt → промпт:
    это выбор написания, а не потеря слова)."""
    t, out, i = token.lower(), [], 0
    while i < len(t):
        pair = t[i:i + 2]
        if pair in _DIGRAPHS:
            out.append(_DIGRAPHS[pair])
            i += 2
        else:
            out.append(_LAT2CYR.get(t[i], t[i]))
            i += 1
    return "".join(out)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _dist1(a: str, b: str) -> bool:
    """Разница ровно в один символ (замена/вставка/удаление) — достаточно
    для отлова вариантов известного термина вроде CloudUI → Claude UI."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    long, short = (a, b) if len(a) > len(b) else (b, a)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))


def _similar(a: str, b: str) -> bool:
    """Похоже на известный термин сильнее опечатки: dist1 + ratio≥0.75
    при len≥5 (cloudui→claudeui — 2 символа, dist1 не ловит)."""
    if _dist1(a, b):
        return True
    if min(len(a), len(b)) < 5:
        return False
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def groq_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "")
    if key:
        return key
    try:
        out = subprocess.run(["systemctl", "cat", "voice-bridge.service"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"Environment=GROQ_API_KEY=(\S+)", out)
        return m.group(1) if m else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def groq_transcribe(path: Path, key: str, session: requests.Session) -> str | None:
    # turbo — отдельный лимит от large и заметно быстрее; на разнице движков
    # для поиска терминов это не сказывается (нужны латинские токены, не WER).
    for attempt in range(4):
        try:
            with open(path, "rb") as f:
                r = session.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (path.name, f, "audio/webm")},
                    data={"model": "whisper-large-v3-turbo", "language": "ru",
                          "response_format": "text", "temperature": "0"},
                    timeout=(10.0, 60.0),
                )
            if r.status_code == 200:
                return r.text.strip()
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"  groq 429 — жду {wait}с", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  groq {r.status_code}: {r.text[:120]}", file=sys.stderr)
            return None
        except (OSError, requests.RequestException) as e:
            print(f"  groq ошибка: {e}", file=sys.stderr)
            time.sleep(5)
    return None


def latin_terms(text: str) -> list[str]:
    out = []
    for m in LATIN_RE.finditer(text):
        tok = m.group(0).strip(".-_")
        if len(tok) < 2 or tok.lower() in STOP:
            continue
        out.append(tok)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=ARCHIVE_DEFAULT)
    ap.add_argument("--limit", type=int, default=60,
                    help="сколько свежих записей прогнать")
    ap.add_argument("--min-count", type=int, default=2,
                    help="термин попадает в словарь от стольких диктовок")
    ap.add_argument("--max-terms", type=int, default=46,
                    help="потолок выученного словаря (бюджет keyterm 500)")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    key = groq_key()
    if not key:
        print("GROQ_API_KEY не найден ни в окружении, ни в юните", file=sys.stderr)
        return 1

    archive = Path(args.archive)
    pairs = []
    for txt in sorted(archive.glob("*.txt"), reverse=True):
        audio = txt.with_suffix(".webm")
        if audio.exists():
            pairs.append((audio, txt))
        if len(pairs) >= args.limit:
            break
    if not pairs:
        print(f"в {archive} нет пар аудио+текст", file=sys.stderr)
        return 1
    print(f"прогоняю {len(pairs)} свежих диктовок…")

    session = requests.Session()
    # По каждому нормализованному термину — счётчики по поверхным формам:
    # «Claude UI», «CloudUI», «claudeui» — один термин, формы голосуют.
    forms: dict[str, Counter] = {}
    missed_docs: dict[str, set] = {}   # термин → номера диктовок, где его нет у DG
    seen_docs: dict[str, set] = {}     # термин → диктовки, где DG написал латиницей
    absorbed_docs: dict[str, set] = {} # термин → DG написал кириллицей (не потеря)

    for i, (audio, txt) in enumerate(pairs, 1):
        dg_text = txt.read_text(encoding="utf-8", errors="replace")
        dg_low = dg_text.lower()
        gq = groq_transcribe(audio, key, session)
        if gq is None:
            continue
        for tok in latin_terms(gq):
            key_n = _norm(tok)
            forms.setdefault(key_n, Counter())[tok] += 1
            if tok.lower() in dg_low:
                seen_docs.setdefault(key_n, set()).add(i)
            elif lat_to_cyr(tok) in dg_low:
                absorbed_docs.setdefault(key_n, set()).add(i)
            else:
                missed_docs.setdefault(key_n, set()).add(i)
        time.sleep(0.4)  # бережём rate limit Groq
        if i % 10 == 0:
            print(f"  {i}/{len(pairs)}")

    # ── Перепроверка кандидатов (защита от записи ОШИБОЧНОЙ формы — вопрос
    # Егора 23.10.26: «Claude UI записывается неправильно, и оно может
    # добавиться в словарь в неправильном виде»). Правила отбора:
    #  1. Форма по большинству: в словарь идёт только доминирующее написание
    #     (≥60% вхождений); разбросанное написание = недоверенное → кандидат.
    #  2. Канон: термин из TERM_CANON берётся в канонической форме; близкий
    #     к канону вариант (±1 символ) — не словарь, а кандидат.
    #  3. Столкновение с базовым keyterm: вариант в ±1 символ от уже стоящего
    #     термина («CloudUI» к «Claude UI») отбрасывается — иначе словарь
    #     закрепил бы ошибку.
    #  4. Кириллическое поглощение: если Deepgram писал термин кириллицей
    #     (prompt→промпт), это выбор написания, а не потеря — в словарь не идёт.
    sys.path.insert(0, str(REPO))
    base_terms: set[str] = set()
    canon: dict[str, str] = {}
    try:
        from speech_engine.profiles import DICTATION
        from speech_engine.terms import TERM_CANON
        base_terms = {_norm(t) for t in DICTATION.deepgram_params.get("keyterm", [])}
        canon = {_norm(k): v for k, v in TERM_CANON.items()}
    except Exception:
        pass

    # Эталонная база реальных терминов (Егор 23.10: «намного эффективнее
    # сравнивать с реальной базой и реальными названиями», а не с мнением
    # второго ASR). Собирается tools/build_reference.py из его текстов и
    # курируемых документов. Кандидат принимается ТОЛЬКО если его форма
    # подтверждена базой/каноном — Groq остаётся лишь детектором пропусков.
    ref_path = REPO / "data" / "reference_terms.json"
    ref: dict[str, dict] = {}
    try:
        ref = json.loads(ref_path.read_text(encoding="utf-8")).get("terms", {})
    except (OSError, json.JSONDecodeError):
        print(f"  ! нет {ref_path} — решение только по канону", file=sys.stderr)

    learned_forms: list[str] = []
    candidates: list[dict] = []

    for key_n, fc in forms.items():
        total = sum(fc.values())
        form, form_n = fc.most_common(1)[0]
        share = form_n / total
        n_missed = len(missed_docs.get(key_n, ()))
        n_seen = len(seen_docs.get(key_n, ()))
        n_abs = len(absorbed_docs.get(key_n, ()))
        entry = {"form": form, "variants": dict(fc), "missed": n_missed,
                 "seen": n_seen, "absorbed": n_abs}

        if not (n_missed >= args.min_count or n_seen >= args.min_count + 1):
            continue
        if key_n in base_terms or any(_similar(key_n, b) for b in base_terms):
            continue  # уже в словаре или искажённый вариант его термина
        if key_n in canon:
            form = canon[key_n]
        elif key_n in ref:
            form = ref[key_n]["form"]  # реальное написание из базы Егора
        elif share < 0.6:
            entry["reason"] = "разбросанное написание — нет доминирующей формы"
            candidates.append(entry)
            continue
        elif any(_similar(key_n, c) for c in canon) or any(_similar(key_n, r) for r in ref):
            entry["reason"] = "похоже на реальный термин — искажение, не слово"
            candidates.append(entry)
            continue
        else:
            entry["reason"] = "нет подтверждения в реальной базе терминов"
            candidates.append(entry)
            continue
        if n_missed and n_abs >= n_missed:
            entry["reason"] = "Deepgram пишет кириллицей — выбор написания, не потеря"
            candidates.append(entry)
            continue
        learned_forms.append(form)

    # Проактивная подгрузка (Егор 23.10: «все названия, которые я часто
    # использую, пусть постоянно подгружаются из общей базы»): термины из
    # реальной базы с частотой ≥5 идут в keyterm сами — Deepgram их тогда
    # не теряет вовсе. Канон и базовый словарь уже учтены выше/в запросе.
    for key_n, r in ref.items():
        # Только термины, которые Егор реально пишет с заглавной (брендовое
        # написание: Google, Playwright, amoCRM — cap_share≥0.6 по всем его
        # формам). Капитализированные англ. слова (Adding, Requesting — в его
        # текстах есть и строчные формы как обычные слова) и идентификаторы
        # с _ отсекаются — в keyterm общий английский может перетянуть
        # русское слово в английское.
        form = r["form"]
        if r.get("user_count", 0) < 3 or len(key_n) < 4 or "_" in form:
            continue
        # Общие слова, которые nova-3 и так знает нативно, в keyterm вредны:
        # перекос модели корёжит соседние слова и окончания (жалоба Егора
        # 26.10 «окончания стали хуже» после набухания списка). keyterm —
        # только для терминов, с которыми модель реально спотыкается.
        if key_n in {"google", "apple", "cloud", "nova", "excel", "macbook",
                     "wifi", "github", "telegram", "zoom", "miro", "brave",
                     "ipad", "ceo", "seo", "api", "textedit"}:
            continue
        if r.get("cap_share", 0) < 0.5 or not any(ch.isupper() for ch in form):
            continue
        if key_n in base_terms or key_n in canon or key_n in {_norm(t) for t in learned_forms}:
            continue
        learned_forms.append(form)

    score = {k: len(missed_docs.get(k, ())) + len(seen_docs.get(k, ()))
             for k in forms}
    # Вытеснение редких (Егор 24.10): список ограничен (~80 терминов суммарно
    # с базовым — дальше растёт риск force-match), поэтому при переполнении
    # самые редко используемые им термины уступают место более активным.
    # Мера «используемости» — user_count из реальной базы, при равенстве —
    # наблюдения минера.
    learned = sorted(set(learned_forms),
                     key=lambda t: (-ref.get(_norm(t), {}).get("user_count", 0),
                                    -score.get(_norm(t), 0)))[:args.max_terms]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"{args.archive} × groq whisper-large-v3-turbo, последние {len(pairs)} диктовок",
        "terms": learned,
        "candidates": candidates,
        "missed_counts": {k: len(v) for k, v in missed_docs.items()},
        "already_fine": {k: len(v) for k, v in seen_docs.items()},
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"\nвыученных терминов: {len(learned)} → {out}")
    for t in learned:
        k = _norm(t)
        parts = []
        if missed_docs.get(k):
            parts.append(f"потерян ×{len(missed_docs[k])}")
        if seen_docs.get(k):
            parts.append(f"частый ×{len(seen_docs[k])}")
        print(f"  + {t} ({', '.join(parts)})")
    if candidates:
        print(f"\nкандидаты, не прошедшие перепроверку ({len(candidates)}):")
        for c in candidates[:10]:
            print(f"  ? {c['form']} — {c['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
