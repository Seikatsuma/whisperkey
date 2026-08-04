#!/usr/bin/env python3
"""Тесты дословности и целостности WhisperKey.

Запуск:  python3 tests/test_verbatim.py

Тесты работают с НАСТОЯЩИМИ функциями из whisperkey.py — они вытаскиваются
через ast и выполняются в изолированном пространстве имён. Импортировать модуль
целиком нельзя: он на старте открывает микрофон и грузит модель распознавания.
Так тесты гоняются где угодно, включая машину без звуковой карты.
"""
import ast
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "whisperkey.py")

# Что берём из модуля: чистая логика без сети, микрофона и модели.
WANTED_FUNCS = {
    "smart_grammar_fix", "apply_smart_sentence_ending", "strip_asr_artifacts",
    "clean_noise", "_norm_word", "_drop_overlap", "_fmt_mmss", "_join_chunks",
    "_split_audio", "_find_degenerate_segments", "_text_from_response",
    "_words_in_range",
}
WANTED_CONSTS = {
    "SAMPLE_RATE", "NARRATOR_LOOP_PATTERN", "BOH_TAIL_MARKERS", "ASR_CONTEXT_PROMPT",
    "_INCOMPLETE_ENDING_RE", "FORCE_SENTENCE_CASE", "NORMALIZE_BRAND_NAMES",
    "BRAND_NAMES", "artifacts_removed", "CHUNK_THRESHOLD_SECONDS",
    "CHUNK_SIZE_SECONDS", "CHUNK_OVERLAP_SECONDS", "DENSITY_GATE_MIN_DURATION",
    "DENSITY_GATE_MIN_WORDS_PER_SEC", "HALLUCINATION_TRIGGERS",
}


def load_module_parts():
    source = open(SRC, encoding="utf-8").read()
    tree = ast.parse(source)
    ns = {"re": re, "np": np, "print": lambda *a, **k: None}
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
    """Ни заглавная, ни точка не навязываются."""
    out = pipeline("и добавь туда")
    check("Т2 без заглавной", out.startswith("и"), f"получено {out!r}")
    check("Т2 без точки", not out.endswith("."), f"получено {out!r}")


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
    dur = 300.0
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

    check("Т4 промпт содержит пунктуацию",
          prompt.count('.') + prompt.count(',') >= 3,
          f"знаков мало: {prompt!r}")


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
