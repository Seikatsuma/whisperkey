#!/usr/bin/env python3
"""Тесты проверки вставки и страховки чистки (правка 19.10.26).

Запуск:  python3 tests/test_insert_verify.py

Проверяются чистые функции whisperkey.py, вытащенные через AST (модуль целиком
не импортируется — на старте открывает микрофон). МакOS-часть (osascript, AX,
pbcopy) здесь не гоняется — нет мака; проверяется логика решений.
"""
import ast
import difflib
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "whisperkey.py")

WANTED_FUNCS = {"_norm_for_cmp", "_insert_verified", "_word_tokens",
                "_diff_removed_text", "_verify_cleanup"}
WANTED_CONSTS = {"CLEANUP_LOSS_ABS", "CLEANUP_LOSS_RATIO"}

ns = {"re": re, "difflib": difflib, "Counter": Counter}
tree = ast.parse(open(SRC, encoding="utf-8").read())
keep = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCS:
        keep.append(node)
    elif isinstance(node, ast.Assign):
        if any(t.id in WANTED_CONSTS for t in node.targets if isinstance(t, ast.Name)):
            keep.append(node)
exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)

FAIL = 0
def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")

# ── _norm_for_cmp / _insert_verified ──
check("norm снимает пунктуацию и регистр",
      ns["_norm_for_cmp"]("Привет, Мир!") == "приветмир")
check("verified: хвост найден в поле",
      ns["_insert_verified"]("Привет мир ", "старое", "старое Привет мир") is True)
check("verified: поле не изменилось → False",
      ns["_insert_verified"]("Привет мир ", "старое", "старое") is False)
check("verified: поле нечитаемо → None",
      ns["_insert_verified"]("Привет мир ", None, None) is None)
check("verified: before=None, текст виден → True",
      ns["_insert_verified"]("Привет мир ", None, "текст Привет мир") is True)
check("verified: приросло на длину текста → True",
      ns["_insert_verified"]("длинная фраза целиком", "abc", "abc" + "длинная фраза целиком") is True)
check("verified: юникод/эмодзи в тексте",
      ns["_insert_verified"]("делаем «как надо» ✅", "", "делаем «как надо» ✅") is True)
check("verified: пустой текст — False при читаемом поле",
      ns["_insert_verified"](" ", "abc", "abc") is False)

# ── _diff_removed_text / _verify_cleanup ──
check("diff: вырезанный хвост виден",
      ns["_diff_removed_text"]("живая фраза субтитры делал дима", "живая фраза")
      == "субтитры делал дима")
check("diff: ничего не вырезано",
      ns["_diff_removed_text"]("тот же текст", "тот же текст") == "")

raw = "ну вот смотри надо сделать отчёт по проекту до пятницы и прислать мне"
fin_same = "Ну вот смотри, надо сделать отчёт по проекту до пятницы и прислать мне"
t, n = ns["_verify_cleanup"](raw, fin_same, "")
check("cleanup: нет потерь → без отката", t == fin_same and n == 0)

fin_cut = "Надо сделать отчёт"   # потеряно 9 слов из 12 — выше порога
t, n = ns["_verify_cleanup"](raw, fin_cut, "")
check("cleanup: потеря куска → откат на raw", t == raw and n >= 9, f"n={n}")

fin_cut_legal = "Надо сделать отчёт"
# В проде legal_removed — ровно то, что вырезала чистка (diff raw→clean_noise).
legal = ns["_diff_removed_text"](raw, fin_cut_legal)
t, n = ns["_verify_cleanup"](raw, fin_cut_legal, legal)
check("cleanup: легальная вырезка потерей не считается", t == fin_cut_legal and n <= 0, f"n={n}")

raw_short = "субтитры делал дима"
fin_short = "субтитры делал дима"
t, n = ns["_verify_cleanup"](raw_short, fin_short, "")
check("cleanup: короткий текст без потерь", n == 0)

raw2 = "а б в г д е ж з и к л м н о п р с т у ф"  # 20 слов
fin2 = "а б в г д е ж з и к л м н о п р с т"      # потеряно 2 (<3) → без отката
t, n = ns["_verify_cleanup"](raw2, fin2, "")
check("cleanup: 2 слова из 20 — без отката", t == fin2 and n == 2, f"n={n}")

print(f"\n{'ВСЕ ПРОШЛИ' if not FAIL else f'ПРОВАЛОВ: {FAIL}'}")
sys.exit(1 if FAIL else 0)
