#!/usr/bin/env python3
"""Тесты дословности и целостности WhisperKey.

Запуск:  python3 tests/test_verbatim.py

09.08.26 — перенос каскада (transcribe_deepgram, _recognize_with_whisper и всё,
от чего они зависят: гейт плотности, склейка кусков, перенос знаков/терминов/форм,
таблица названий, водяные знаки) в общий пакет ~/speech-engine/ (профиль dictation).
Тест адаптирован, а не переписан: те же 31 функция test_*, те же проверки, тот же
корпус CORPUS/ANCHORS/WATERMARKS. Изменился только СПОСОБ добычи логики —

  - что осталось в whisperkey.py (smart_grammar_fix, apply_smart_sentence_ending,
    clean_noise и десяток констант формата/буфера) — по-прежнему вытаскивается
    через ast из whisperkey.py и выполняется в изолированном пространстве имён
    (модуль целиком не импортируется: на старте открывает микрофон и грузит модель);
  - что переехало в speech_engine — импортируется НАПРАВУЮ, обычным import: пакет
    не трогает ни микрофон, ни модель при импорте, изолировать нечего. Часть
    перенесённых функций поменяла сигнатуру (принимают профиль/параметры явно
    вместо чтения модульных констант WhisperKey) — обёрнуты лямбдами на DICTATION
    ниже, чтобы вызовы в теле тестов (M["_split_audio"](audio, dur) и т.п.)
    остались прежними.

Числа и пороги не изменились — они просто теперь читаются из
speech_engine.DICTATION, а не из глобальных констант этого файла.
"""
import ast
import difflib
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "whisperkey.py")
SPEECH_ENGINE_HOME = os.path.expanduser("~/speech-engine")
if SPEECH_ENGINE_HOME not in sys.path:
    sys.path.insert(0, SPEECH_ENGINE_HOME)
import speech_engine
from speech_engine import chunking as se_chunking
from speech_engine import density_gate as se_density_gate
from speech_engine import audio as se_audio
from speech_engine import terms as se_terms
from speech_engine import transfer as se_transfer

DICTATION = speech_engine.DICTATION

# Что берём АСТ-экстракцией из whisperkey.py: то, что там реально осталось.
WANTED_FUNCS = {
    "smart_grammar_fix", "apply_smart_sentence_ending", "clean_noise",
}
WANTED_CONSTS = {
    "SAMPLE_RATE", "_INCOMPLETE_ENDING_RE", "CAPITALIZE_FIRST", "FORCE_TRAILING_DOT",
    "artifacts_removed",
}


def load_module_parts():
    source = open(SRC, encoding="utf-8").read()
    tree = ast.parse(source)
    # speech_engine нужен в ns: extracted clean_noise зовёт speech_engine.watermarks.*
    ns = {"re": re, "np": np, "difflib": difflib, "print": lambda *a, **k: None,
          "speech_engine": speech_engine}
    keep = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCS:
            keep.append(node)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n in WANTED_CONSTS for n in names):
                keep.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # artifacts_removed объявлен с аннотацией типа — это отдельный узел AST
            if node.target.id in WANTED_CONSTS and node.value is not None:
                keep.append(ast.Assign(targets=[node.target], value=node.value,
                                       lineno=node.lineno, col_offset=node.col_offset))
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)

    # Переехавшее в speech_engine — прямой импорт, подставлено под старые имена
    # теста. Сигнатуры _split_audio/_find_degenerate_segments/_text_from_response/
    # _restore_timeline поменялись (профиль передаётся явно) — оборачиваем
    # параметрами DICTATION, чтобы вызовы в тестах ниже не менялись.
    ns["_norm_word"] = se_chunking.norm_word
    ns["_drop_overlap"] = se_chunking.drop_overlap
    ns["_fmt_mmss"] = se_chunking.fmt_mmss
    ns["_join_chunks"] = se_chunking.join_chunks
    ns["_split_audio"] = lambda audio, dur: se_chunking.split_audio(
        audio, dur, ns["SAMPLE_RATE"], DICTATION.chunk_threshold_seconds,
        DICTATION.chunk_size_seconds, DICTATION.chunk_overlap_seconds)
    ns["_find_degenerate_segments"] = lambda segments: se_density_gate.find_degenerate_segments(
        segments, DICTATION.density_gate_min_duration, DICTATION.density_gate_min_words_per_sec)
    ns["_text_from_response"] = lambda result: se_density_gate.text_from_response(
        result, DICTATION.density_gate_min_duration, DICTATION.density_gate_min_words_per_sec)
    ns["_words_in_range"] = se_density_gate.words_in_range
    ns["transfer_punctuation"] = se_transfer.transfer_punctuation
    ns["_transfer_norm"] = se_terms._transfer_norm
    ns["_change_tempo"] = se_audio.change_tempo
    ns["_restore_timeline"] = lambda result: se_audio.restore_timeline(result, DICTATION.asr_tempo)
    ns["transfer_terms"] = se_terms.transfer_terms
    ns["transfer_endings"] = se_transfer.transfer_endings
    ns["fix_known_terms"] = se_terms.fix_known_terms

    # Константы, теперь профильные — под старыми именами теста.
    ns["ASR_TEMPO"] = DICTATION.asr_tempo
    ns["STYLE_PROMPT"] = DICTATION.style_prompt
    ns["TERM_CANON"] = se_terms.TERM_CANON
    ns["TERM_FIX"] = se_terms.TERM_FIX
    ns["PUNCT_TRANSFER_MIN_SECONDS"] = DICTATION.punct_transfer_min_seconds
    ns["PUNCT_TRANSFER_MAX_SECONDS"] = DICTATION.punct_transfer_max_seconds
    ns["CHUNK_THRESHOLD_SECONDS"] = DICTATION.chunk_threshold_seconds
    ns["CHUNK_SIZE_SECONDS"] = DICTATION.chunk_size_seconds
    ns["CHUNK_OVERLAP_SECONDS"] = DICTATION.chunk_overlap_seconds
    ns["DENSITY_GATE_MIN_DURATION"] = DICTATION.density_gate_min_duration
    ns["DENSITY_GATE_MIN_WORDS_PER_SEC"] = DICTATION.density_gate_min_words_per_sec
    ns["ENDING_MIN_STEM"] = se_transfer.ENDING_MIN_STEM
    ns["ASR_CONTEXT_PROMPT"] = DICTATION.groq_context_prompt
    ns["BOH_TAIL_MARKERS"] = speech_engine.watermarks.BOH_TAIL_MARKERS
    return ns


M = load_module_parts()
FAILURES = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def pipeline(text):
    """Полная цепочка обработки текста — как в process_audio."""
    M["artifacts_removed"].clear()
    out = M["clean_noise"](text)
    out = M["smart_grammar_fix"](out)
    if out and len(out) > 1:
        out = M["apply_smart_sentence_ending"](out)
    return out


# ─── Т1. Тождество на корпусе реальных фраз ───────────────────────────────────

CORPUS = [
    # обычные фразы
    "надо сделать отчёт к пятнице",
    "давай обсудим это завтра на созвоне",
    "я думаю нам стоит поменять подход",
    "он сказал что всё готово",
    "мне нужно чтобы это работало без сбоев",
    # слова, которые раньше съедались как «маркеры галлюцинаций»
    "нам нужен хороший корректор",
    "он корректор и дизайнер",
    "нужен корректор в команду",
    "это корректор цвета",
    "задача сложная, но продолжение следует",
    "поговорим об этом позже, продолжение следует",
    # одиночные слова — раньше обнулялись целиком
    "Python",
    "Cursor",
    "Конец",
    "да",
    "нет",
    "готово",
    # числа, версии, время, деньги — раньше ломались пробелом после точки
    "поставь версию 3.5 и проверь",
    "деплой на прод в 18.30",
    "сумма 1250.50 рублей",
    "это стоит 10.000 рублей",
    "текст пишем в формате 1.5 на 2.0",
    "версия 2.7.1 вышла вчера",
    # имена файлов и пути
    "проверь файл main.py там ошибка",
    "открой config.json",
    "смотри в папку src/utils",
    "залей на github.com сегодня",
    "Открой файл AAAA.txt",
    # английские вкрапления
    "надо сделать deploy на прод",
    "запусти скрипт через Python",
    "проверь API документацию",
    "я в Cursor работаю",
    # обрывы мысли — точка не должна навязываться
    "и добавь туда",
    "надо чтобы он работал как",
    "мы говорили про то что",
    # многоточия и заминки
    "ну я думаю..... что так",
    "это... как бы... понятно",
]


def test_identity():
    """Конвейер не должен менять текст, кроме нормализации пробелов."""
    for phrase in CORPUS:
        out = pipeline(phrase)
        in_words = phrase.split()
        out_words = out.split()
        check(
            f"Т1 тождество: {phrase!r}",
            in_words == out_words or _only_brand_diff(in_words, out_words),
            f"вход {len(in_words)} слов -> выход {len(out_words)}: {out!r}",
        )


def _only_brand_diff(a, b):
    """Различие допустимо, только если это нормализация написания бренда."""
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        if x.lower().strip(".,!?") != y.lower().strip(".,!?"):
            return False
    return True


# ─── Т2. Регрессионные якоря ──────────────────────────────────────────────────

ANCHORS = [
    ("Он корректор и дизайнер", 4),
    ("Нужен корректор в команду", 4),
    ("Задача сложная, но продолжение следует", 5),
    ("Python", 1),
    ("Cursor", 1),
    ("Открой файл AAAA.txt", 3),
    ("и добавь туда", 3),
    ("Ну я думаю..... что так", 5),
]


def test_anchors():
    """Кейсы, на которых прежняя версия теряла до 100% текста."""
    for phrase, expected_words in ANCHORS:
        out = pipeline(phrase)
        got = len(out.split())
        check(
            f"Т2 якорь: {phrase!r}",
            got == expected_words,
            f"ожидалось {expected_words} слов, получено {got}: {out!r}",
        )


def test_no_forced_formatting():
    """Заглавная ставится, точка — нет.

    Заглавная безопасна: слова не меняются. Точка в конце опасна — диктовка
    часто идёт в середину предложения, и навязанная точка ломает мысль.
    """
    out = pipeline("и добавь туда")
    check("Т2 заглавная поставлена", out.startswith("И"), f"получено {out!r}")
    check("Т2 без точки", not out.endswith("."), f"получено {out!r}")
    check("Т2 слова целы", len(out.split()) == 3, f"получено {out!r}")


# ─── Т3. Двусторонний гард: ничего не потеряно и ничего не добавлено ──────────

def test_two_sided_guard():
    for phrase in CORPUS:
        out = pipeline(phrase)
        src_norm = [M["_norm_word"](w) for w in phrase.split()]
        out_norm = [M["_norm_word"](w) for w in out.split()]
        inserted = [w for w in out_norm if w and w not in src_norm]
        check(
            f"Т3 без вставок: {phrase!r}",
            not inserted,
            f"добавлено {inserted}",
        )
        check(
            f"Т3 без потерь: {phrase!r}",
            len(out_norm) >= len(src_norm),
            f"было {len(src_norm)} слов, стало {len(out_norm)}",
        )


# ─── Т4. Водяные знаки вырезаются, но только они ──────────────────────────────

WATERMARKS = [
    ("Всё готово. Субтитры сделал DimaTorzok", "всё готово"),
    ("Договорились. Редактор субтитров А.Семкин", "договорились"),
    ("Хорошо. Thanks for watching", "хорошо"),
]


def test_watermarks_removed():
    for src, expected_start in WATERMARKS:
        out = pipeline(src).lower()
        check(
            f"Т4 водяной знак снят: {src!r}",
            "субтитр" not in out and "dimatorzok" not in out and "watching" not in out,
            f"осталось: {out!r}",
        )
        check(
            f"Т4 речь цела: {src!r}",
            out.startswith(expected_start.split()[0]),
            f"получено: {out!r}",
        )


def test_watermark_inside_sentence_kept():
    """Слово внутри живой фразы вырезаться не должно."""
    out = pipeline("субтитры к фильму делал наш подрядчик и это важно")
    check(
        "Т4 не режет живую речь",
        len(out.split()) == 9,
        f"получено {len(out.split())} слов: {out!r}",
    )


# ─── Т5-Т7. Нарезка, перекрытие, склейка ──────────────────────────────────────

def test_split_short_not_chunked():
    audio = np.zeros(int(M["SAMPLE_RATE"] * 60), dtype=np.float32)
    chunks, offsets = M["_split_audio"](audio, 60.0)
    check("Т5 короткая запись не режется", len(chunks) == 1, f"кусков {len(chunks)}")


def test_split_long_has_overlap():
    # Порог нарезки — 15 минут: всё короче уходит в облако одним запросом,
    # это доказано замером (одним куском 79.8% против 72.3% у нарезки).
    dur = M["CHUNK_THRESHOLD_SECONDS"] + 120.0
    audio = np.zeros(int(M["SAMPLE_RATE"] * dur), dtype=np.float32)
    chunks, offsets = M["_split_audio"](audio, dur)
    check("Т5 длинная режется", len(chunks) > 1, f"кусков {len(chunks)}")
    if len(offsets) > 1:
        step = offsets[1] - offsets[0]
        expected = M["CHUNK_SIZE_SECONDS"] - M["CHUNK_OVERLAP_SECONDS"]
        check("Т5 шаг с перекрытием", abs(step - expected) < 0.1,
              f"шаг {step:.1f}с, ожидался {expected:.1f}с")
    covered = offsets[-1] + len(chunks[-1]) / M["SAMPLE_RATE"]
    check("Т5 покрытие до конца", covered >= dur - 0.1,
          f"покрыто {covered:.1f}с из {dur}с")


def test_overlap_dedup_exact():
    prev = "мы обсудили это вчера и решили двигаться дальше"
    nxt = "и решили двигаться дальше а теперь давай к делу"
    out = M["_drop_overlap"](prev, nxt)
    check("Т7 дубль снят", out == "а теперь давай к делу", f"получено {out!r}")


def test_overlap_dedup_keeps_when_no_match():
    """Нет точного совпадения — ничего не режем. Дубль лучше потери."""
    prev = "первая часть фразы"
    nxt = "совершенно другая вторая часть"
    out = M["_drop_overlap"](prev, nxt)
    check("Т7 без совпадения не режет", out == nxt, f"получено {out!r}")


def test_lost_chunk_is_marked():
    parts = ["первый кусок текста", None, "третий кусок текста"]
    quality = ["cloud", "lost", "cloud"]
    offsets = [0.0, 57.0, 114.0]
    text, marks = M["_join_chunks"](parts, quality, offsets)
    check("Т11 потеря помечена", len(marks) == 1, f"меток {len(marks)}")
    check("Т11 метка в тексте", "[не распознано 00:57]" in text, f"текст: {text!r}")
    check("Т11 остальное цело",
          "первый кусок текста" in text and "третий кусок текста" in text,
          f"текст: {text!r}")


# ─── Т8. Гейт плотности ───────────────────────────────────────────────────────

def test_density_gate_catches_empty():
    segments = [
        {"start": 0.0, "end": 10.0, "text": "нормальный сегмент с достаточным числом слов здесь", "no_speech_prob": 0.01},
        {"start": 10.0, "end": 23.0, "text": "", "no_speech_prob": 0.028},
        {"start": 23.0, "end": 50.0, "text": "95 95 95", "no_speech_prob": 0.581},
        {"start": 50.0, "end": 55.0, "text": "короткий", "no_speech_prob": 0.9},
    ]
    bad = M["_find_degenerate_segments"](segments)
    check("Т8 гейт ловит пустой и вырожденный", len(bad) == 2,
          f"найдено {len(bad)}: {bad}")


def test_density_gate_ignores_normal():
    segments = [{"start": 0.0, "end": 20.0,
                 "text": " ".join(["слово"] * 40), "no_speech_prob": 0.9}]
    bad = M["_find_degenerate_segments"](segments)
    check("Т8 гейт не трогает нормальный", not bad, f"ложное срабатывание: {bad}")


# ─── Т5. words[] предпочитается segments[] ────────────────────────────────────

def test_healthy_segment_keeps_punctuation():
    """Здоровый сегмент отдаётся как есть — со знаками препинания.

    Это главное отличие от сборки только из words[]: там пунктуации нет вовсе.
    """
    result = {
        "text": "неважно",
        "segments": [{"start": 0.0, "end": 5.0,
                      "text": "Да, вот так. И вот так.", "no_speech_prob": 0.0}],
        "words": [{"word": "Да", "start": 0.1, "end": 0.4},
                  {"word": "вот", "start": 0.5, "end": 0.8},
                  {"word": "так", "start": 0.9, "end": 1.2}],
    }
    text, _ = M["_text_from_response"](result)
    check("Т5 пунктуация сохранена", "," in text and "." in text, f"получено {text!r}")
    check("Т5 текст из сегмента", text == "Да, вот так. И вот так.", f"получено {text!r}")


def test_degenerate_segment_recovered_from_words():
    """Сорванное окно подменяется словами из words[] — это и есть спасённый текст."""
    result = {
        "text": "неважно",
        "segments": [
            {"start": 0.0, "end": 10.0, "text": "Нормальный кусок речи, вот такой.",
             "no_speech_prob": 0.0},
            # 15 секунд, три слова-мусора: классический срыв модели
            {"start": 10.0, "end": 25.0, "text": "95 95 95", "no_speech_prob": 0.03},
        ],
        "words": [{"word": "Нормальный", "start": 0.5, "end": 1.0},
                  {"word": "кусок", "start": 1.1, "end": 1.5},
                  {"word": "речи", "start": 1.6, "end": 2.0},
                  {"word": "вот", "start": 2.1, "end": 2.4},
                  {"word": "такой", "start": 2.5, "end": 3.0},
                  {"word": "а", "start": 11.0, "end": 11.2},
                  {"word": "здесь", "start": 11.3, "end": 11.7},
                  {"word": "была", "start": 11.8, "end": 12.1},
                  {"word": "живая", "start": 12.2, "end": 12.6},
                  {"word": "речь", "start": 12.7, "end": 13.0}],
    }
    text, degenerate = M["_text_from_response"](result)
    check("Т5 срыв обнаружен", len(degenerate) == 1, f"найдено {len(degenerate)}")
    check("Т5 речь восстановлена", "здесь была живая речь" in text, f"получено {text!r}")
    check("Т5 мусор выброшен", "95 95 95" not in text, f"получено {text!r}")
    check("Т5 здоровая часть цела", "Нормальный кусок речи, вот такой." in text,
          f"получено {text!r}")


def test_falls_back_to_segments():
    result = {
        "text": "запасной",
        "segments": [{"start": 0.0, "end": 5.0, "text": "из сегментов", "no_speech_prob": 0.0}],
        "words": [],
    }
    text, _ = M["_text_from_response"](result)
    check("Т5 запасной путь", text == "из сегментов", f"получено {text!r}")


def test_prompt_has_no_topic():
    """Промпт задаёт только пунктуацию. Тема и имена в нём протекают в текст.

    Проверяется отсутствие терминов и слов с заглавной буквы в середине —
    именно они подставлялись моделью вместо неразобранной речи.
    """
    prompt = M["ASR_CONTEXT_PROMPT"]
    forbidden = ["it", "программирован", "технич", "деловая", "фонд", "задачи"]
    low = prompt.lower()
    hits = [w for w in forbidden if w in low]
    check("Т4 промпт без темы", not hits, f"найдено {hits} в {prompt!r}")

    # Имя собственное = заглавная буква не в начале предложения
    proper = re.findall(r'(?<![.!?]\s)(?<!^)\b[А-ЯA-Z][а-яa-z]{2,}', prompt)
    check("Т4 промпт без имён собственных", not proper, f"найдено {proper}")

    # Промпт пуст намеренно: на короткой диктовке он глушит речь.
    # Замер 06.08.26 на 8-секундных клипах: 80 слов с промптом против 112 без,
    # причём одна фраза схлопнулась в «Да.» — слово из самого промпта.
    check("Т4 промпт пуст", prompt == "",
          f"промпт задан: {prompt!r} — на короткой диктовке это теряет текст")


def test_punctuation_transfer_keeps_words():
    """Перенос знаков со второго прохода не смеет тронуть ни одного слова.

    Второй проход идёт С промптом — значит его текст содержит слова из промпта,
    подставленные вместо неуслышанной речи. Ровно они и не должны просочиться.
    """
    tp = M["transfer_punctuation"]
    norm = lambda t: [re.sub(r'[^\w]', '', w, flags=re.UNICODE).lower() for w in t.split()]

    cases = [
        ("привет как дела у тебя", "Привет, как дела? У тебя"),
        ("я говорю про виспер кей и другое", "Я говорю про, да, конечно. И другое."),
        ("совсем другой текст здесь", "Так, ну, в общем, смотри. Значит, вот."),
        ("одно слово", "Одно слово."),
        ("текст без пары", ""),
        ("", "Что-то."),
        ("слово", "слово"),
    ]
    for base, styled in cases:
        out = tp(base, styled)
        check(f"Т9 слова целы: {base[:24]!r}", norm(out) == norm(base),
              f"{base!r} + {styled!r} → {out!r}")

    out = tp("я говорю про виспер кей и другое", "Я говорю про, да, конечно. И другое.")
    check("Т9 слово промпта не просочилось", "конечно" not in out.lower(), out)


def test_punctuation_transfer_adds_marks():
    """Знаки и заглавные переносятся там, где слова совпали."""
    tp = M["transfer_punctuation"]
    out = tp("привет как дела у тебя", "Привет, как дела? У тебя")
    check("Т9 знаки перенесены", out == "Привет, как дела? У тебя", out)

    # Заглавная не должна приезжать в середину предложения: в оформленной версии
    # слово начинало фразу, а в полной перед ним точки нет.
    out = tp("я говорю про виспер кей и другое", "Я говорю про, да, конечно. И другое.")
    parts = out.split()
    mid = [w for i, w in enumerate(parts)
           if i and w[:1].isupper() and parts[i - 1][-1] not in '.!?…']
    check("Т9 нет заглавной в середине фразы", not mid, f"{mid} в {out!r}")


def test_style_prompt_is_second_pass_only():
    """STYLE_PROMPT живёт отдельно от основного запроса и не касается слов."""
    check("Т9 основной промпт пуст", M["ASR_CONTEXT_PROMPT"] == "",
          "промпт в основном запросе теряет речь")
    check("Т9 STYLE_PROMPT задан", bool(M["STYLE_PROMPT"]),
          "без него второй проход бессмыслен")
    # Короткие диктовки whisper оформляет сам, второй запрос там лишний.
    check("Т9 порог не ниже 8с", M["PUNCT_TRANSFER_MIN_SECONDS"] >= 8.0,
          str(M["PUNCT_TRANSFER_MIN_SECONDS"]))
    # 16 кГц/16 бит — 32 КБ на секунду, предел Groq 25 МБ ≈ 780 с.
    check("Т9 потолок ниже предела 25 МБ", M["PUNCT_TRANSFER_MAX_SECONDS"] <= 780.0,
          str(M["PUNCT_TRANSFER_MAX_SECONDS"]))


def test_tempo_constant_is_sane():
    """Темп — единственная обработка звука, и она обязана быть незаметной."""
    t = M["ASR_TEMPO"]
    check("Т10 темп задан числом", isinstance(t, float), repr(t))
    # Замерялся коридор 0.97–1.03. Уводить дальше без нового замера нельзя:
    # сильный сдвиг темпа меняет звучание речи и рискует качеством.
    check("Т10 темп в замеренном коридоре", 0.95 <= t <= 1.10, str(t))


def test_tempo_keeps_signal_intact():
    """Смена темпа не режет и не добавляет звук — только пересчитывает сетку."""
    ct = M["_change_tempo"]
    t = M["ASR_TEMPO"]
    a = np.sin(np.linspace(0, 50, M["SAMPLE_RATE"])).astype(np.float32)   # 1 секунда

    b = ct(a, t)
    check("Т10 длина изменилась по темпу", abs(len(b) / len(a) - 1 / t) < 0.001,
          f"{len(b)}/{len(a)} = {len(b)/len(a):.4f}, ожидалось {1/t:.4f}")
    check("Т10 тип не потерян", b.dtype == np.float32, str(b.dtype))
    check("Т10 громкость не выросла", float(abs(b).max()) <= float(abs(a).max()) + 1e-6,
          f"{abs(b).max()} против {abs(a).max()}")

    # Темп 1.0 обязан быть тождеством: это способ вернуть прежнее поведение.
    same = ct(a, 1.0)
    check("Т10 темп 1.0 ничего не меняет", len(same) == len(a), f"{len(same)} против {len(a)}")

    # Вырожденные входы не должны ронять диктовку.
    for n in (0, 1, 2):
        try:
            out = ct(np.zeros(n, dtype=np.float32), t)
            ok = len(out) <= max(n, 1)
        except Exception as e:
            ok = False; out = e
        check(f"Т10 вход {n} отсчётов не роняет", ok, str(out))


def test_timeline_restored_after_tempo():
    """Таймкоды обязаны вернуться к реальному времени.

    Гейт плотности, подстановка из words[] и снятие перекрытий считают
    в реальных секундах. Если оставить сжатую шкалу, длинные записи
    склеятся со сдвигом.
    """
    rt = M["_restore_timeline"]
    t = M["ASR_TEMPO"]
    res = {"segments": [{"start": 0.0, "end": 10.0, "text": "x"}],
           "words": [{"word": "а", "start": 1.0, "end": 2.0}]}
    rt(res)
    check("Т10 сегмент растянут обратно",
          abs(res["segments"][0]["end"] - 10.0 * t) < 1e-6, str(res["segments"][0]))
    check("Т10 слово растянуто обратно",
          abs(res["words"][0]["end"] - 2.0 * t) < 1e-6, str(res["words"][0]))

    # Отсутствующие и нечисловые поля не должны ронять разбор ответа.
    odd = {"segments": [{"text": "нет времени"}, {"start": None, "end": "x"}], "words": None}
    try:
        rt(odd); ok = True
    except Exception as e:
        ok = False; odd = e
    check("Т10 кривой ответ не роняет", ok, str(odd))


def test_eval_samples_keep_original_audio():
    """В корпус эталонов пишется исходный звук, а не ускоренный.

    Иначе следующий замер будет сделан по уже обработанному материалу
    и перестанет отражать то, что слышит микрофон.
    """
    src = open(SRC, encoding="utf-8").read()
    marker = "def _write_raw_recording_wav"
    if marker not in src:
        # Win-версия корпус не собирает — проверять нечего.
        check("Т10 запись корпуса без ускорения", True, "сбора корпуса нет в этой версии")
        return
    body = src[src.index(marker):]
    body = body[:body.index("\ndef ")]
    check("Т10 запись корпуса без ускорения",
          "_change_tempo" not in body and "ASR_TEMPO" not in body, body[:200])


def test_term_transfer_whitelist_only():
    """Из словарного прохода в текст попадают ТОЛЬКО слова белого списка.

    Промпт со словарём заставляет модель писать названия латиницей, но он же
    подставляет свои слова вместо неуслышанных. Белый список — единственное,
    что отделяет одно от другого.
    """
    tt = M["transfer_terms"]
    out, n = tt("открой клади код и зайди в гроб", "открой Claude Code и зайди в Groq")
    check("Т11 названия подставлены", out == "открой Claude Code и зайди в Groq", out)
    check("Т11 счётчик верен", n == 3, str(n))

    # Слово не из списка не имеет права просочиться.
    out, n = tt("я говорю про это", "я говорю про совершенно другое")
    check("Т11 постороннее слово не прошло", out == "я говорю про это", out)
    check("Т11 подстановок нет", n == 0, str(n))

    # Длина текста не меняется никогда: замена ровно одно слово на одно.
    for base, vocab in [("а б в г д", "а Claude в Groq д"),
                        ("одно", "Claude"), ("", "Claude Code"), ("текст", "")]:
        out, _ = tt(base, vocab)
        check(f"Т11 длина сохранена: {base[:18]!r}",
              len(out.split()) == len(base.split()), f"{base!r} -> {out!r}")


def test_style_prompt_carries_vocabulary():
    """Словарь едет во втором проходе — отдельного запроса он не стоит."""
    p = M["STYLE_PROMPT"]
    for term in ("Claude Code", "WhisperKey", "GitHub", "Groq"):
        check(f"Т11 в промпте есть {term}", term in p, p[:120])
    # Образец пунктуации обязан остаться: второй проход отвечает и за знаки.
    check("Т11 образец знаков на месте", "." in p and "," in p, p[:60])
    # Белый список не должен содержать обычных русских слов: подстановка
    # «кода» вместо «код» в живой речи была бы прямой порчей текста.
    plain = {"код", "дизайн", "агент", "промпт", "токен", "скилл"}
    bad = plain & set(M["TERM_CANON"])
    check("Т11 в списке нет обычных слов", not bad, f"найдено {bad}")


def test_ending_transfer_cannot_swap_words():
    """Слово может сменить окончание, но не может стать другим словом.

    Whisper слышит команду верно и ошибается только формой: «сохрани» пишет
    как «сохраняет». Это чинится по совпадению основы — и ровно это условие
    не даёт подставить постороннее слово вместо услышанного.
    """
    te = M["transfer_endings"]

    out, n = te("он сохраняет это и реализует", "он сохрани это и реализуй")
    check("Т12 формы команд исправлены", out == "он сохрани это и реализуй", out)
    check("Т12 счётчик верен", n == 2, str(n))

    # Разные слова с одинаковым началом короче основы меняться не должны.
    for base, donor in [("длинными фразами говорю", "тебе фразами говорю"),
                        ("кот сел", "пёс сел"),
                        ("работа идёт", "радость идёт")]:
        out, n = te(base, donor)
        check(f"Т12 подмена слова не прошла: {base[:20]!r}", out == base and n == 0,
              f"{base!r} -> {out!r}")

    # Длина текста не меняется никогда: замена одно слово на одно.
    for base, donor in [("а б в г", "а б в г"), ("сохраняет", "сохрани"),
                        ("", "сохрани"), ("сохраняет", "")]:
        out, _ = te(base, donor)
        check(f"Т12 длина сохранена: {base[:16]!r}",
              len(out.split()) == len(base.split()), f"{base!r} -> {out!r}")


def test_style_prompt_carries_imperatives():
    """Второй проход несёт образцы команд — из-за них модель и выбирает форму."""
    p = M["STYLE_PROMPT"]
    found = [w for w in ("Сохрани", "Раздели", "Выстрой", "Реализуй", "Проверь") if w in p]
    check("Т12 образцы команд в промпте", len(found) >= 4, f"нашлось {found}")
    check("Т12 порог основы разумный", 3 <= M["ENDING_MIN_STEM"] <= 6,
          str(M["ENDING_MIN_STEM"]))


def test_term_table_cannot_touch_real_words():
    """Таблица названий не имеет права трогать живую русскую речь.

    «код», «клад», «рок» — обычные слова, и подстановка на них была бы прямой
    порчей текста. Именно поэтому их нет в таблице, и это проверяется здесь,
    а не оставляется на внимательность того, кто будет её пополнять.
    """
    fx = M["fix_known_terms"]
    for phrase in ["сам код тупит", "папку из VS код", "нашёл клад",
                   "это рок музыка", "кода не хватает", "он клал на стол",
                   "русский рок играет", "код дизайн"]:
        out, n = fx(phrase)
        check(f"Т13 не тронуто: {phrase!r}", out == phrase and n == 0, f"{phrase!r} -> {out!r}")

    # В самой таблице тоже не должно оказаться обычных слов.
    plain = {"код", "клад", "рок", "кода", "клада", "дизайн", "агент", "токен"}
    bad = [p for p, _ in M["TERM_FIX"]
           for w in plain if re.fullmatch(r"\\\\b" + w + r"\\\\b", p)]
    check("Т13 в таблице нет обычных слов", not bad, f"найдено {bad}")


def test_term_table_fixes_known_names():
    """Названия, записанные по звучанию, приводятся к правильному виду."""
    fx = M["fix_known_terms"]
    cases = [("докрути виспер", "WhisperKey"), ("виспро.кей работает", "WhisperKey"),
             ("как seo to seo", "CEO"), ("джимми 2.5", "Gemini"),
             ("выложи на гитхаб", "GitHub"), ("клоуда открой", "Claude"),
             ("возьми опыт Whisperer-K, прямо", "WhisperKey")]
    for phrase, want in cases:
        out, n = fx(phrase)
        check(f"Т13 исправлено: {phrase!r}", want in out and n > 0, f"{phrase!r} -> {out!r}")

    # Найдено сканом живого корпуса 11.08.26 (несловарная запись WhisperKey) — само
    # слово "whisperer" без суффикса -k/-key трогать нельзя, мало ли что оно значит.
    out, n = fx("horse whisperer story")
    check("Т13 голый whisperer не тронут", out == "horse whisperer story" and n == 0,
          f"-> {out!r}")

    # Пустой вход не роняет.
    for empty in ("", None):
        out, n = fx(empty)
        check(f"Т13 пустой вход: {empty!r}", n == 0, str(out))


def test_markers_have_no_plain_words():
    """В списке водяных знаков не должно быть обычных слов языка."""
    forbidden = {"корректор", "продолжение следует", "конец"}
    found = [m for m in M["BOH_TAIL_MARKERS"] if m.lower() in forbidden]
    check("Т2 нет обычных слов в маркерах", not found, f"найдено: {found}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAILURES.append(f"{t.__name__}: упал с {type(e).__name__}: {e}")

    print()
    print("=" * 70)
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} из {CHECKS} проверок")
        print("=" * 70)
        for f in FAILURES:
            print(f"  ✗ {f}")
        print()
        return 1
    print(f"ВСЁ ПРОЙДЕНО: {CHECKS} проверок, {len(tests)} тестов")
    print("=" * 70)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
