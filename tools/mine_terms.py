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
        "or", "of", "for", "so", "no", "yes", "webm", "wav", "http", "https"}
# Регистр: если весь токен нижний регистр и это не известный бренд — чаще обычное
# слово или транслит, а не термин. Оставляем только токены с заглавной или
# CamelCase, либо вхождения, где тот же токен видели с заглавной.


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
    ap.add_argument("--max-terms", type=int, default=60,
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
    missed = Counter()   # термин → сколько диктовок его потеряли
    seen_ok = Counter()  # термины, которые Deepgram уже пишет латиницей

    for i, (audio, txt) in enumerate(pairs, 1):
        dg_text = txt.read_text(encoding="utf-8", errors="replace")
        dg_low = dg_text.lower()
        gq = groq_transcribe(audio, key, session)
        if gq is None:
            continue
        gq_terms = latin_terms(gq)
        for tok in gq_terms:
            if tok.lower() in dg_low:
                seen_ok[tok] += 1
            else:
                missed[tok] += 1
        time.sleep(0.4)  # бережём rate limit Groq
        if i % 10 == 0:
            print(f"  {i}/{len(pairs)}")

    # Свод. Два источника кандидатов:
    #  1. missed — термины, которые Groq слышит, а Deepgram в тексте не имеет
    #     (потеря/коверка) — от min-count диктовок.
    #  2. seen_ok — частые термины, которые Deepgram пока пишет верно, но при
    #     высокой частоте стоит подстраховать бустом от эпизодических выпадений
    #     (исходная жалоба — именно выпадение уже известного «Deepgram»).
    # Уже стоящие в профильном keyterm не дублируем.
    sys.path.insert(0, str(REPO))
    try:
        from speech_engine.profiles import DICTATION
        base_terms = {t.lower() for t in DICTATION.deepgram_params.get("keyterm", [])}
    except Exception:
        base_terms = set()

    learned = sorted(
        {t for t, c in missed.items() if c >= args.min_count}
        | {t for t, c in seen_ok.items() if c >= args.min_count + 1},
        key=lambda t: -(missed.get(t, 0) + seen_ok.get(t, 0)),
    )
    learned = [t for t in learned if t.lower() not in base_terms][:args.max_terms]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"{args.archive} × groq whisper-large-v3, последние {len(pairs)} диктовок",
        "terms": learned,
        "missed_counts": dict(missed.most_common()),
        "already_fine": dict(seen_ok.most_common()),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"\nвыученных терминов: {len(learned)} → {out}")
    for t in learned:
        parts = []
        if missed.get(t):
            parts.append(f"потерян ×{missed[t]}")
        if seen_ok.get(t):
            parts.append(f"частый ×{seen_ok[t]}")
        print(f"  + {t} ({', '.join(parts)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
