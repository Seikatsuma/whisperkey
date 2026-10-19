#!/usr/bin/env python3
"""Исполнительный прогон direct_insert по всем веткам лестницы с заглушками.

Запуск:  python3 tests/test_direct_insert.py

Вытаскивает direct_insert + _insert_verified/_norm_for_cmp/_is_browser из
whisperkey.py через AST и исполняет КАЖДУЮ ветку с подменёнными внешними
вызовами (AX, JS, буфер, клавиши — всё мок). Ловит класс багов «переменная
не инициализирована на этой ветке» (UnboundLocalError ax_app 19.10.26 —
живая поломка, ушедшая на Mac) и неверные вердикты.

Главный инвариант редизайна 19.10.26: НИКОГДА два успешных слепых удара
в одном цикле — пара «клавиша + меню» в нечитаемое поле и была источником
дубля текста.
"""
import ast
import io
import os
import re
import sys
import time
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "whisperkey.py")

tree = ast.parse(open(SRC, encoding="utf-8").read())
WANTED = {"direct_insert", "_insert_verified", "_norm_for_cmp", "_is_browser"}
fns = [n for n in tree.body
       if isinstance(n, ast.FunctionDef) and n.name in WANTED]

calls = {"put": 0, "paste": 0, "menu": 0, "type": 0, "notify": 0, "sound": 0,
         "restore": 0, "ax": 0, "axsel": 0, "js": 0, "activate": 0,
         "jsnotify": 0}

class Kb:
    def type(self, t):
        calls["type"] += 1

ns = {
    "time": time, "print": print, "kb": Kb(), "re": re,
    "_JS_BROWSERS": ("brave", "chrome", "chromium", "edge",
                     "vivaldi", "opera", "arc"),
    "_focus_app_at_start": "",
    "RESTORE_CLIPBOARD": True,
    "_ax_smart_insert": lambda t: SC["ax"](t),
    "_ax_selected_text_insert": lambda t: SC["axsel"](t),
    "_js_insert": lambda a, t: SC["js"](a, t),
    "_js_flag_notify_once": lambda a: calls.__setitem__(
        "jsnotify", calls["jsnotify"] + 1),
    "_wait_option_release": lambda: SC["option"](),
    "_clipboard_now": lambda: b"OLD",
    "_put_clipboard": lambda d: SC["put"](d),
    "_send_paste_keystroke": lambda app="": SC["paste"](app),
    "_poll_insert": lambda t, b, timeout=1.2: SC["poll"](t, b),
    "_send_paste_via_menu": lambda app: (SC["menu"](app), "menu err"),
    "_activate_app": lambda n: SC["activate"](n),
    "_restore_clipboard_async": lambda o, d: calls.__setitem__(
        "restore", calls["restore"] + 1),
    "_fail_sound": lambda: calls.__setitem__("sound", calls["sound"] + 1),
    "notify": lambda a, b: calls.__setitem__("notify", calls["notify"] + 1),
    "_js_off": set(),
}
exec(compile(ast.Module(body=fns, type_ignores=[]), SRC, "exec"), ns)
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
    """scenario — dict стабов; want_lines — подстроки вывода;
    checks — (ключ счётчика, ожидание)."""
    global SC
    calls.update({k: 0 for k in calls})
    ns["_focus_app_at_start"] = scenario.pop("focus_start", "")
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
        check(f"{name}: есть «{w[:40]}»", w in out, out[-400:])
    for key, want in checks:
        check(f"{name}: {key}={want}", calls[key] == want,
              f"got {calls[key]}")

# ─── Стабы ───────────────────────────────────────────────────────────────────
PUT_OK = lambda d: calls.__setitem__("put", calls["put"] + 1) or True
PASTE_OK = lambda app="": calls.__setitem__("paste", calls["paste"] + 1) or True
PASTE_ERR = lambda app="": calls.__setitem__("paste", calls["paste"] + 1) or False
MENU_OK = lambda app: calls.__setitem__("menu", calls["menu"] + 1) or True
MENU_ERR = lambda app: calls.__setitem__("menu", calls["menu"] + 1) or False
AXSEL_NO = lambda t: calls.__setitem__("axsel", calls["axsel"] + 1) or ("App", False)
OPT_OK = lambda: True
NOACT = lambda n: False

# 1. Читаемое поле: AX-запись принята и проверена → буфер не трогали вообще
run("ax-verified", dict(
    ax=lambda t: calls.__setitem__("ax", calls["ax"] + 1) or
        ("TextEdit", "WROTE", "AXTextArea", "старый Проверка записи раз "),
    axsel=AXSEL_NO, js=lambda a, t: ("SKIP", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: None,
    menu=MENU_ERR, activate=NOACT),
    ["прямая запись", "[insert success]", "ax-write"],
    [("ax", 1), ("put", 0), ("paste", 0), ("restore", 0)])

# 2. AX-запись принята, но поле переписало (verify не нашёл хвост) →
#    буферный путь с реальными вердиктами
run("ax-rewrote", dict(
    ax=lambda t: ("TextEdit", "WROTE", "AXTextArea", "переписано приложением"),
    axsel=AXSEL_NO, js=lambda a, t: ("SKIP", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: True,
    menu=MENU_ERR, activate=NOACT),
    ["переписало вставку", "[insert success]", "paste"],
    [("put", 1), ("paste", 1), ("menu", 0), ("restore", 1)])

# 3. Читаемое поле отказало AX-записи (WRITEFAIL) → Cmd+V → poll True
run("writefail-paste", dict(
    ax=lambda t: ("TextEdit", "WRITEFAIL", "AXTextArea", "старый текст"),
    axsel=AXSEL_NO, js=lambda a, t: ("SKIP", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: True,
    menu=MENU_ERR, activate=NOACT),
    ["отклонило прямую запись", "[insert success]"],
    [("put", 1), ("paste", 1), ("menu", 0)])

# 4. Поле дважды отвергло Cmd+V → меню → poll True
run("reject-menu", dict(
    ax=lambda t: ("TextEdit", "WRITEFAIL", "AXTextArea", "старый текст"),
    axsel=AXSEL_NO, js=lambda a, t: ("SKIP", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b:
        True if calls["menu"] else False,
    menu=MENU_OK, activate=NOACT),
    ["[insert success]", "menu"],
    [("paste", 2), ("menu", 1), ("type", 0)])

# 5. Поле отвергло всё → посимвольный набор → poll True
run("reject-type", dict(
    ax=lambda t: ("TextEdit", "WRITEFAIL", "AXTextArea", "старый текст"),
    axsel=AXSEL_NO, js=lambda a, t: ("SKIP", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b:
        True if calls["type"] else False,
    menu=MENU_ERR, activate=NOACT),
    ["вставка набором", "[insert success]"],
    [("paste", 2), ("menu", 1), ("type", 1)])

# 6. Нечитаемое поле: запись в AXSelectedText принята → sent, буфер не трогали
run("axsel-ok", dict(
    ax=lambda t: ("WeirdApp", "UNREADABLE", "AXTextArea", ""),
    axsel=lambda t: calls.__setitem__("axsel", calls["axsel"] + 1) or ("WeirdApp", True),
    js=lambda a, t: ("SKIP", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: None,
    menu=MENU_ERR, activate=NOACT),
    ["AXSelectedText принята", "[insert sent]"],
    [("axsel", 1), ("put", 0), ("paste", 0), ("menu", 0), ("type", 0)])

# 7. Нечитаемое поле в браузере: JS вставил и проверил → success без буфера
run("js-verified", dict(
    ax=lambda t: ("Brave Browser", "UNREADABLE", "AXTextArea", ""),
    axsel=AXSEL_NO,
    js=lambda a, t: calls.__setitem__("js", calls["js"] + 1) or ("VERIFIED", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: None,
    menu=MENU_ERR, activate=NOACT),
    ["через JS вкладки", "[insert success]", "js"],
    [("js", 1), ("put", 0), ("menu", 0), ("restore", 0)])

# 8. КЛЮЧЕВОЙ: нечитаемое поле, JS выключен → ОДИН удар меню, Cmd+V НЕ уходит
run("blind-single-menu", dict(
    ax=lambda t: ("Brave Browser", "UNREADABLE", "AXTextArea", ""),
    axsel=AXSEL_NO,
    js=lambda a, t: calls.__setitem__("js", calls["js"] + 1) or ("DISABLED", "flag off"),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: None,
    menu=MENU_OK, activate=NOACT),
    ["вставка через меню", "[insert sent]"],
    [("put", 1), ("menu", 1), ("paste", 0), ("type", 0), ("jsnotify", 1)])

# 9. Нечитаемое поле: меню доказало отказ → ОДИН Cmd+V → sent
run("blind-menu-err-key", dict(
    ax=lambda t: ("Brave Browser", "UNREADABLE", "AXTextArea", ""),
    axsel=AXSEL_NO, js=lambda a, t: ("FAIL", "noel"),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: None,
    menu=MENU_ERR, activate=NOACT),
    ["[insert sent]", "key"],
    [("menu", 1), ("paste", 1), ("type", 0)])

# 10. Нечитаемое поле: оба транспорта сообщили отказ → набор (безопасно:
#     ни один удар не «ушёл» — дубля быть не может)
run("blind-all-fail-type", dict(
    ax=lambda t: ("Brave Browser", "UNREADABLE", "AXTextArea", ""),
    axsel=AXSEL_NO, js=lambda a, t: ("FAIL", "noel"),
    option=OPT_OK, put=PUT_OK, paste=PASTE_ERR, poll=lambda t, b: None,
    menu=MENU_ERR, activate=NOACT),
    ["[insert sent]", "type"],
    [("menu", 1), ("paste", 1), ("type", 1)])

# 11. NOFOCUS + не браузер: axsel не нашёл элемента → один удар меню
run("nofocus-menu", dict(
    ax=lambda t: ("Finder", "NOFOCUS", "", ""),
    axsel=AXSEL_NO, js=lambda a, t: ("SKIP", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: None,
    menu=MENU_OK, activate=NOACT),
    ["[insert sent]"],
    [("put", 1), ("menu", 1), ("paste", 0), ("type", 0)])

# 12. Option зажат → буферный путь пропущен целиком, сразу набор
run("option-held", dict(
    ax=lambda t: ("Brave Browser", "UNREADABLE", "", ""),
    axsel=AXSEL_NO, js=lambda a, t: ("SKIP", ""),
    option=lambda: False, put=PUT_OK, paste=PASTE_OK,
    poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["[insert sent]", "type"],
    [("put", 0), ("paste", 0), ("menu", 0), ("type", 1)])

# 13. Фокус ушёл за время ASR → возврат в исходное приложение, повтор ступени
ax_calls = {"n": 0}
def _ax_drift(t):
    ax_calls["n"] += 1
    if ax_calls["n"] == 1:
        return ("Safari", "NOFOCUS", "", "")
    return ("TextEdit", "WROTE", "AXTextArea", "x Проверка записи раз ")
run("focus-drift", dict(
    ax=_ax_drift, axsel=AXSEL_NO, js=lambda a, t: ("SKIP", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: None,
    menu=MENU_ERR,
    activate=lambda n: calls.__setitem__("activate", calls["activate"] + 1) or True,
    focus_start="TextEdit"),
    ["фокус за время распознавания", "[insert success]"],
    [("activate", 1)])

# 14. Всё отказало → FAILED + звук + notify, текст остаётся в буфере
run("all-fail", dict(
    ax=lambda t: ("TextEdit", "WRITEFAIL", "AXTextArea", "старый"),
    axsel=AXSEL_NO, js=lambda a, t: ("SKIP", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_ERR, poll=lambda t, b: False,
    menu=MENU_ERR, activate=NOACT),
    ["[insert FAILED]"], [("type", 1), ("notify", 1), ("sound", 1)])

# 15. Любая ветка упала исключением → [insert error], наружу не вылетает
class Boom:
    def __call__(self, *a, **k): raise RuntimeError("boom")
run("exception", dict(
    ax=Boom(), axsel=Boom(), js=Boom(), option=lambda: True,
    put=PUT_OK, paste=PASTE_OK, poll=lambda t, b: None,
    menu=MENU_ERR, activate=NOACT),
    ["[insert error]"], [("notify", 1)])

# 16. Буфер не записался в нечитаемом поле → набор
run("blind-put-fail", dict(
    ax=lambda t: ("Brave Browser", "UNREADABLE", "", ""),
    axsel=AXSEL_NO, js=lambda a, t: ("FAIL", "noel"),
    option=OPT_OK, put=lambda d: False, paste=PASTE_OK,
    poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["[insert sent]", "type"],
    [("put", 0), ("paste", 0), ("type", 1)])

print()
print("ВСЕ ПРОШЛИ" if FAIL == 0 else f"ПРОВАЛОВ: {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
