#!/usr/bin/env python3
"""Исполнительный прогон direct_insert по всем веткам с заглушками.

Запуск:  python3 tests/test_direct_insert.py

Вытаскивает direct_insert из whisperkey.py через AST и исполняет КАЖДУЮ ветку
с подменёнными внешними вызовами (AX, буфер, клавиши — всё мок). Ловит класс
багов «переменная не инициализирована на этой ветке» (UnboundLocalError
ax_app 19.10.26 — живая поломка, ушедшая на Mac) и неверные вердикты.
"""
import ast
import io
import os
import sys
import time
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "whisperkey.py")

tree = ast.parse(open(SRC, encoding="utf-8").read())
fn = next(n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "direct_insert")

calls = {"put": 0, "paste": 0, "menu": 0, "type": 0, "notify": 0, "sound": 0,
         "restore": 0, "axins": 0}
log = []

# Сценарий задаётся через ENV-стабы ниже.
class Kb:
    def type(self, t):
        calls["type"] += 1

ns = {
    "time": time, "print": print, "kb": Kb(),
    "_focus_app_at_start": "",
    "RESTORE_CLIPBOARD": True,
    # внешние вызовы — заглушки, поведение задаёт сценарий
    "_ax_focused_value": lambda: SC["axval"](),
    "_clipboard_now": lambda: b"OLD",
    "_ax_insert_direct": lambda t: SC["axins"](t),
    "_wait_option_release": lambda: SC["option"](),
    "_put_clipboard": lambda d: SC["put"](d),
    "_send_paste_keystroke": lambda app="": SC["paste"](app),
    "_poll_insert": lambda t, b, timeout=1.2: SC["poll"](t, b),
    "_send_paste_via_menu": lambda app: SC["menu"](app),
    "_activate_app": lambda n: False,
    "_restore_clipboard_async": lambda o, d: calls.__setitem__("restore", calls["restore"] + 1),
    "_fail_sound": lambda: calls.__setitem__("sound", calls["sound"] + 1),
    "notify": lambda a, b: calls.__setitem__("notify", calls["notify"] + 1),
}
exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), ns)
di = ns["direct_insert"]

FAIL = 0
def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")

def run(name, scenario, want_lines, checks):
    """Прогон одного сценария: scenario — dict стабов, want_lines — подстроки,
    обязанные появиться в выводе, checks — (ключ счётчика, ожидание)."""
    global SC
    calls.update({k: 0 for k in calls})
    SC = scenario
    buf = io.StringIO()
    err = None
    with redirect_stdout(buf):
        try:
            di("Проверка записи раз")
        except Exception as e:
            err = e
    out = buf.getvalue()
    check(f"{name}: без исключения", err is None, repr(err))
    for w in want_lines:
        check(f"{name}: есть «{w[:40]}»", w in out, out[-300:])
    for key, want in checks:
        check(f"{name}: {key}={want}", calls[key] == want,
              f"got {calls[key]}")

OK_VAL = lambda: ("старый текст", "TextEdit", "AXTextArea")
NO_VAL = lambda: (None, "Brave Browser", "")
PUT_OK = lambda d: calls.__setitem__("put", calls["put"] + 1) or True
PASTE_OK = lambda app="": calls.__setitem__("paste", calls["paste"] + 1) or True

# 1. Читаемое поле + AX-запись принята → успех, буфер не писали
run("ax-ok", dict(
    axval=OK_VAL,
    axins=lambda t: calls.__setitem__("axins", calls["axins"] + 1) or ("TextEdit", True, "OK"),
    option=lambda: True, put=PUT_OK, paste=PASTE_OK,
    poll=lambda t, b: True, menu=lambda a: False),
    ["прямая запись", "[insert success]"], [("put", 0), ("paste", 0), ("axins", 1)])

# 2. Нечитаемое поле (Brave): AX-запись НЕ вызываем, paste отправлен,
#    проверки нет → добивка меню + [insert sent]
run("brave-none", dict(
    axval=NO_VAL,
    axins=lambda t: calls.__setitem__("axins", calls["axins"] + 1) or ("", False, "ERR"),
    option=lambda: True, put=PUT_OK, paste=PASTE_OK,
    poll=lambda t, b: None,
    menu=lambda a: calls.__setitem__("menu", calls["menu"] + 1) or True),
    ["добивка через меню", "[insert sent]"],
    [("axins", 0), ("put", 1), ("paste", 1), ("menu", 1), ("type", 0)])

# 3. Поле читаемо, AX-запись отказана → буферный путь, верификация сразу
run("ax-set-fail", dict(
    axval=OK_VAL,
    axins=lambda t: calls.__setitem__("axins", calls["axins"] + 1) or ("TextEdit", False, "ERR:SET"),
    option=lambda: True, put=PUT_OK, paste=PASTE_OK,
    poll=lambda t, b: True, menu=lambda a: False),
    ["[insert success]"], [("axins", 1), ("put", 1), ("paste", 1)])

# 4. Поле отвергло вставку дважды → посимвольный набор → успех
run("reject-then-type", dict(
    axval=OK_VAL,
    axins=lambda t: ("TextEdit", False, "ERR:SET"),
    option=lambda: True, put=PUT_OK, paste=PASTE_OK,
    poll=lambda t, b: False if calls["type"] == 0 else True,
    menu=lambda a: False),
    ["вставка набором", "[insert success]"],
    [("put", 2), ("paste", 2), ("type", 1)])

# 5. Option зажат → буферный цикл пропущен, сразу набор
run("option-held", dict(
    axval=NO_VAL,
    axins=lambda t: ("", False, "ERR"),
    option=lambda: False, put=PUT_OK, paste=PASTE_OK,
    poll=lambda t, b: True, menu=lambda a: False),
    ["[insert success]"], [("put", 0), ("paste", 0), ("type", 1)])

# 6. Всё отказало → FAILED + notify + звук, буфер с нашим текстом
run("all-fail", dict(
    axval=OK_VAL,
    axins=lambda t: ("TextEdit", False, "ERR:SET"),
    option=lambda: True, put=PUT_OK,
    paste=lambda app="": calls.__setitem__("paste", calls["paste"] + 1) or False,
    poll=lambda t, b: False, menu=lambda a: False),
    ["[insert FAILED]"], [("paste", 2), ("type", 1), ("notify", 1), ("sound", 1)])

# 7. Любая ветка упала исключением → [insert error], наружу не вылетает
class Boom:
    def __call__(self, *a, **k): raise RuntimeError("boom")
run("exception", dict(
    axval=Boom(), axins=Boom(), option=lambda: True,
    put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: None, menu=lambda a: False),
    ["[insert error]"], [("notify", 1)])

print()
print("ВСЕ ПРОШЛИ" if FAIL == 0 else f"ПРОВАЛОВ: {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
