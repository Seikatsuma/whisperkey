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
дубля текста. Для скорости слепой удар — один вызов _blind_strike
(меню→клавиша внутри одного osascript).
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
         "restore": 0, "ax": 0, "js": 0, "activate": 0, "jsnotify": 0,
         "blind": 0}

class Kb:
    def type(self, t):
        calls["type"] += 1

ns = {
    "time": time, "print": print, "kb": Kb(), "re": re,
    "_JS_BROWSERS": ("brave", "chrome", "chromium", "edge",
                     "vivaldi", "opera", "arc"),
    "RESTORE_CLIPBOARD": True,
    "_ax_unlock_failed": set(),
    "_ax_smart_insert": lambda t, try_unlock=True: SC["ax"](t),
    "_js_insert": lambda a, t: SC["js"](a, t),
    "_js_flag_notify_once": lambda a: calls.__setitem__(
        "jsnotify", calls["jsnotify"] + 1),
    "_wait_option_release": lambda: SC["option"](),
    "_clipboard_now": lambda: b"OLD",
    "_put_clipboard": lambda d: SC["put"](d),
    "_send_paste_keystroke": lambda app="": SC["paste"](app),
    "_poll_insert": lambda t, b, timeout=1.2: SC["poll"](t, b),
    "_send_paste_via_menu": lambda app: (SC["menu"](app), "menu err"),
    "_blind_strike": lambda app: SC["blind"](app),
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
    ns["_focus_app_at_start"] = scenario.get("focus_start", "")
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
BLIND_MENU = lambda a: calls.__setitem__("blind", calls["blind"] + 1) or ("MENU", "")
BLIND_KEY = lambda a: calls.__setitem__("blind", calls["blind"] + 1) or ("KEY", "")
BLIND_ERR = lambda a: calls.__setitem__("blind", calls["blind"] + 1) or ("", "ERR")
JS_SKIP = lambda a, t: ("SKIP", "")
OPT_OK = lambda: True
NOACT = lambda n: False
UNREAD = lambda t: ("Brave Browser", "UNREADABLE", "AXTextArea", "")
UNREAD_APP = lambda t: ("WeirdApp", "UNREADABLE", "AXTextArea", "")

# 1. Читаемое поле: AX-запись принята и проверена → буфер не трогали вообще
run("ax-verified", dict(
    ax=lambda t: calls.__setitem__("ax", calls["ax"] + 1) or
        ("TextEdit", "WROTE", "AXTextArea", "старый Проверка записи раз "),
    js=JS_SKIP, option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["прямая запись", "[insert success]", "ax-write"],
    [("ax", 1), ("put", 0), ("paste", 0), ("restore", 0), ("blind", 0)])

# 2. AX-запись прошла, пользователь мгновенно отправил сообщение и очистил поле:
#    отсутствие текста при read-back не разрешает второй транспорт.
run("ax-immediate-send", dict(
    ax=lambda t: ("Claude UI", "WROTE", "AXTextArea", ""),
    js=JS_SKIP, option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: False, menu=MENU_ERR, activate=NOACT),
    ["без повторной вставки", "[insert sent]", "ax-write"],
    [("put", 0), ("paste", 0), ("menu", 0), ("type", 0), ("restore", 0)])

# 2b. AXSetValue отработал, но приложение вернуло устаревшее значение:
#     также не добиваем текст другим транспортом.
run("ax-stale-readback", dict(
    ax=lambda t: ("TextEdit", "WROTE", "AXTextArea", "старое значение"),
    js=JS_SKIP, option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: True, menu=MENU_ERR, activate=NOACT),
    ["без повторной вставки", "[insert sent]"],
    [("put", 0), ("paste", 0), ("type", 0), ("restore", 0)])

# 3. Читаемое поле отказало AX-записи (WRITEFAIL) → Cmd+V → poll True
run("writefail-paste", dict(
    ax=lambda t: ("TextEdit", "WRITEFAIL", "AXTextArea", "старый текст"),
    js=JS_SKIP, option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: True, menu=MENU_ERR, activate=NOACT),
    ["отклонило прямую запись", "[insert success]"],
    [("put", 1), ("paste", 1), ("menu", 0), ("blind", 0)])

# 4. Cmd+V прошёл, пользователь сразу отправил сообщение: пустое поле не
#    запускает повторный Cmd+V, меню или посимвольный набор.
run("paste-immediate-send", dict(
    ax=lambda t: ("Claude UI", "WRITEFAIL", "AXTextArea", "старый текст"),
    js=JS_SKIP, option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: False,
    menu=MENU_OK, activate=NOACT),
    ["без повторной вставки", "[insert sent]", "paste"],
    [("paste", 1), ("menu", 0), ("type", 0)])

# 5. Cmd+V доказанно не отправлен → разрешён один другой транспорт (меню),
#    после него повторов также нет, даже если поле уже очистилось.
run("paste-not-sent-menu-once", dict(
    ax=lambda t: ("TextEdit", "WRITEFAIL", "AXTextArea", "старый текст"),
    js=JS_SKIP, option=OPT_OK, put=PUT_OK, paste=PASTE_ERR,
    blind=BLIND_ERR, poll=lambda t, b: False,
    menu=MENU_OK, activate=NOACT),
    ["[insert sent]", "menu"],
    [("paste", 1), ("menu", 1), ("type", 0)])

# 6. Нечитаемое поле НЕ в браузере: JS пропускается → один слепой удар (MENU)
run("blind-nonbrowser", dict(
    ax=UNREAD_APP, js=JS_SKIP,
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_MENU, poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["один удар", "[insert sent]"],
    [("js", 0), ("put", 1), ("blind", 1), ("paste", 0), ("menu", 0),
     ("type", 0), ("restore", 1)])

# 7. Нечитаемое поле в браузере: JS вставил и проверил → success без буфера
run("js-verified", dict(
    ax=UNREAD,
    js=lambda a, t: calls.__setitem__("js", calls["js"] + 1) or ("VERIFIED", ""),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["через JS вкладки", "[insert success]", "js"],
    [("js", 1), ("put", 0), ("blind", 0), ("restore", 0)])

# 8. КЛЮЧЕВОЙ: нечитаемое поле, JS выключен → ОДИН удар (blind=MENU)
run("blind-single-menu", dict(
    ax=UNREAD,
    js=lambda a, t: calls.__setitem__("js", calls["js"] + 1) or ("DISABLED", "flag off"),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_MENU, poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["один удар", "[insert sent]"],
    [("put", 1), ("blind", 1), ("paste", 0), ("type", 0), ("jsnotify", 1)])

# 9. Нечитаемое поле: blind отдал KEY (меню внутри вызова отказало) → sent
run("blind-key", dict(
    ax=UNREAD, js=lambda a, t: ("FAIL", "noel"),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_KEY, poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["[insert sent]", "KEY"],
    [("blind", 1), ("paste", 0), ("type", 0)])

# 10. Нечитаемое поле: слепой удар отказал → набор (дубля быть не может —
#     ни один удар не «ушёл»)
run("blind-all-fail-type", dict(
    ax=UNREAD, js=lambda a, t: ("FAIL", "noel"),
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["[insert sent]", "type"],
    [("blind", 1), ("type", 1), ("paste", 0)])

# 11. NOFOCUS + не браузер → один удар
run("nofocus-blind", dict(
    ax=lambda t: ("Finder", "NOFOCUS", "", ""),
    js=JS_SKIP, option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_MENU, poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["[insert sent]"],
    [("put", 1), ("blind", 1), ("paste", 0), ("type", 0)])

# 12. Option зажат → буферный путь пропущен целиком, сразу набор
run("option-held", dict(
    ax=UNREAD, js=JS_SKIP,
    option=lambda: False, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["[insert sent]", "type"],
    [("put", 0), ("blind", 0), ("type", 1)])

# 13. Фокус ушёл за время ASR → вставка идёт в текущее приложение,
#     не возвращаемся к приложению начала записи.
run("focus-drift", dict(
    ax=lambda t: calls.__setitem__("ax", calls["ax"] + 1) or ("Safari", "NOFOCUS", "", ""), js=JS_SKIP,
    option=OPT_OK, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_MENU, poll=lambda t, b: None, menu=MENU_ERR,
    activate=NOACT),
    ["[insert sent]"],
    [("activate", 0), ("ax", 1), ("blind", 1)])

# 14. Всё отказало → FAILED + звук + notify, текст остаётся в буфере
run("all-fail", dict(
    ax=lambda t: ("TextEdit", "WRITEFAIL", "AXTextArea", "старый"),
    js=JS_SKIP, option=OPT_OK, put=PUT_OK, paste=PASTE_ERR,
    blind=BLIND_ERR, poll=lambda t, b: False, menu=MENU_ERR, activate=NOACT),
    ["[insert FAILED]"], [("type", 1), ("notify", 1), ("sound", 1)])

# 15. Любая ветка упала исключением → [insert error], наружу не вылетает
class Boom:
    def __call__(self, *a, **k): raise RuntimeError("boom")
run("exception", dict(
    ax=Boom(), js=Boom(), option=lambda: True,
    put=PUT_OK, paste=PASTE_OK, blind=Boom(),
    poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["[insert error]"], [("notify", 1)])

# 16. Option зажат + поле отвергло набор → набор РОВНО один раз
run("option-held-no-double-type", dict(
    ax=lambda t: ("TextEdit", "WRITEFAIL", "AXTextArea", "старый"),
    js=JS_SKIP, option=lambda: False, put=PUT_OK, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: False, menu=MENU_ERR, activate=NOACT),
    ["[insert FAILED]"], [("type", 1), ("put", 1), ("paste", 0),
                          ("notify", 1), ("sound", 1)])

# 17. Буфер не записался в нечитаемом поле → набор
run("blind-put-fail", dict(
    ax=UNREAD, js=lambda a, t: ("FAIL", "noel"),
    option=OPT_OK, put=lambda d: False, paste=PASTE_OK,
    blind=BLIND_ERR, poll=lambda t, b: None, menu=MENU_ERR, activate=NOACT),
    ["[insert sent]", "type"],
    [("blind", 0), ("type", 1)])

send_node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "_send_paste_keystroke")
key_calls = {"pid": 0, "cg": 0, "osa": 0}
key_ns = {
    "_Quartz": object(),
    "_pid_of_app": lambda app: key_calls.__setitem__("pid", key_calls["pid"] + 1) or 42,
    "_cgevent_paste": lambda pid: key_calls.__setitem__("cg", key_calls["cg"] + 1) or True,
    "_osa": lambda *a, **k: (key_calls.__setitem__("osa", key_calls["osa"] + 1) or True, ""),
}
exec(compile(ast.Module(body=[send_node], type_ignores=[]), SRC, "exec"), key_ns)
check("один Cmd+V: одно CGEvent", key_ns["_send_paste_keystroke"]("Claude UI")
      and key_calls == {"pid": 1, "cg": 1, "osa": 0}, str(key_calls))

print()
print("ВСЕ ПРОШЛИ" if FAIL == 0 else f"ПРОВАЛОВ: {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
