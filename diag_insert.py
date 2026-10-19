#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика транспортов вставки WhisperKey — ЗАПУСКАТЬ НА MAC.

    python3 diag_insert.py

Замеряет делом, что реально работает на этой машине: читаемость поля через
Accessibility до/после AXEnhancedUserInterface, запись AXValue/AXSelectedText,
JavaScript из Apple Events в браузере, меню «Вставить», pyobjc-ускорение.
Результат — какой транспорт ставить основным для нечитаемых полей.

Сценарий: скрипт просит кликнуть в поле ввода и нажать Enter — замеры идут
по тому полю, куда ты кликнул. Прогони дважды: раз с полем в TextEdit,
раз с полем в Brave (например, адресная строка или поле на странице).
Вставляет метку «⟪wk-дигностика⟫» — после прогона сотри её руками.
"""
import subprocess
import sys
import time

MARK = "⟪wk-дигностика⟫"


def osa(script, *args, timeout=5):
    t0 = time.time()
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script, *args],
                           capture_output=True, timeout=timeout, text=True)
        return time.time() - t0, r.returncode, (r.stdout or "").strip(), \
            (r.stderr or "").strip()
    except Exception as e:
        return time.time() - t0, -1, "", f"{type(e).__name__}: {e}"


def frontmost():
    _, rc, out, _ = osa('tell application "System Events" to get name of '
                        'first application process whose frontmost is true')
    return out if rc == 0 else "?"


def probe_focus():
    """(app, роль, читаемо?, значение-хвост, сек)."""
    script = '''tell application "System Events"
    set fp to first application process whose frontmost is true
    set appName to name of fp
    try
        set el to value of attribute "AXFocusedUIElement" of fp
    on error
        return appName & linefeed & "NOFOCUS" & linefeed & "" & linefeed & ""
    end try
    set r to ""
    try
        set r to role of el
    end try
    try
        set v to value of attribute "AXValue" of el
        if v is missing value then
            return appName & linefeed & r & linefeed & "NO" & linefeed & ""
        end if
        set vt to v as text
        if (count of vt) > 40 then set vt to text -40 thru -1 of vt
        return appName & linefeed & r & linefeed & "YES" & linefeed & vt
    on error
        return appName & linefeed & r & linefeed & "NO" & linefeed & ""
    end try
end tell'''
    dt, rc, out, err = osa(script)
    p = out.split("\n")
    return (p[0] if len(p) > 0 else "?", p[1] if len(p) > 1 else "?",
            len(p) > 2 and p[2] == "YES", p[3] if len(p) > 3 else "", dt)


def unlock_enhanced(app):
    safe = app.replace("\\", "\\\\").replace('"', '\\"')
    dt, rc, out, err = osa(
        f'tell application "System Events" to tell process "{safe}" to '
        'set value of attribute "AXEnhancedUserInterface" to true')
    return rc == 0, dt, err


def ax_write_probe(text):
    dt, rc, out, err = osa('''on run argv
    set ins to item 1 of argv
    tell application "System Events"
        set fp to first application process whose frontmost is true
        try
            set el to value of attribute "AXFocusedUIElement" of fp
        on error
            return "NOFOCUS"
        end try
        try
            set cur to value of attribute "AXValue" of el
        on error
            return "NOVALUE"
        end try
        set curText to cur as text
        try
            set value of attribute "AXValue" of el to curText & ins
        on error
            return "SETFAIL"
        end try
        try
            set v2 to value of attribute "AXValue" of el
            if (v2 as text) contains ins then return "VERIFIED"
            return "WROTE-NOVER"
        on error
            return "WROTE-NOREAD"
        end try
    end tell
end run''', text)
    return dt, out, err


def seltext_probe(text):
    dt, rc, out, err = osa('''on run argv
    set ins to item 1 of argv
    tell application "System Events"
        set fp to first application process whose frontmost is true
        try
            set el to value of attribute "AXFocusedUIElement" of fp
        on error
            return "NOFOCUS"
        end try
        try
            set value of attribute "AXSelectedText" of el to ins
            return "OK"
        on error errMsg
            return "ERR:" & errMsg
        end try
    end tell
end run''', text)
    return dt, out, err


def js_probe(app):
    """Неразрушающая проба: читает document.activeElement, ничего не вставляет."""
    js = ('(function(){var el=document.activeElement;if(!el)return "NOEL";'
          'return el.tagName+":"+(el.isContentEditable===true?"ce":'
          '(el.tagName==="INPUT"||el.tagName==="TEXTAREA")?"field":"other");})()')
    safe = app.replace("\\", "\\\\").replace('"', '\\"')
    js_q = '"' + js.replace("\\", "\\\\").replace('"', '\\"') + '"'
    script = (f'tell application "{safe}" to execute '
              f"front window's active tab javascript {js_q}")
    return osa(script)


def pyobjc_probe():
    """Есть ли in-process Accessibility API (экономия ~0.1с на вызов)."""
    t0 = time.time()
    try:
        import ApplicationServices as A
        dt = time.time() - t0
        return True, dt, "AXUIElementCopyAttributeValue" in dir(A)
    except Exception as e:
        return False, time.time() - t0, f"{type(e).__name__}: {e}"


def menu_probe(app):
    safe = app.replace("\\", "\\\\").replace('"', '\\"')
    dt, rc, out, err = osa('''on run argv
    set proc to item 1 of argv
    tell application "System Events"
        tell process proc
            repeat with m in {"Edit", "Правка"}
                try
                    set mb to menu bar item m of menu bar 1
                    try
                        get menu item "Paste" of menu 1 of mb
                        return "PASTE-EXISTS"
                    end try
                    try
                        get menu item "Вставить" of menu 1 of mb
                        return "PASTE-EXISTS"
                    end try
                end try
            end repeat
        end tell
    end tell
    return "NO-PASTE-ITEM"
end run''', app)
    return dt, out, err


def main():
    print("=== diag_insert — замер транспортов вставки WhisperKey ===\n")

    # 0. Базовая цена osascript
    ts = [osa('return 1')[0] for _ in range(5)]
    print(f"0. osascript, пустой вызов: {min(ts)*1000:.0f}–{max(ts)*1000:.0f} мс "
          f"(медиана {sorted(ts)[2]*1000:.0f})")

    ok, dt, detail = pyobjc_probe()
    print(f"   pyobjc ApplicationServices: {'есть' if ok else 'НЕТ'} "
          f"({detail}, импорт {dt*1000:.0f} мс)")

    app0 = frontmost()
    print(f"\nСейчас frontmost: {app0}")
    input("Кликни в текстовое поле, которое проверяем (TextEdit / поле в Brave), "
          "вернись сюда и нажми Enter… ")

    # 1. Читаемость ДО разблокировки
    app, role, readable, tail, dt = probe_focus()
    print(f"\n1. frontmost={app} роль={role} читаемо={readable} "
          f"({dt*1000:.0f} мс) хвост=«{tail}»")

    # 2. Разблокировка AXEnhancedUserInterface + перечитывание
    if not readable:
        ok, dt, err = unlock_enhanced(app)
        app2, role2, readable2, tail2, dt2 = probe_focus()
        print(f"2. AXEnhancedUserInterface: set={'ok' if ok else err} "
              f"({dt*1000:.0f} мс) → читаемо={readable2} ({dt2*1000:.0f} мс)")
        readable = readable2
    else:
        print("2. AXEnhancedUserInterface: не нужна — поле и так читается")

    # 3. Запись AXValue + чтение назад
    dt, out, err = ax_write_probe(" " + MARK)
    print(f"3. Запись AXValue: {out} ({dt*1000:.0f} мс)")

    # 4. Запись AXSelectedText
    dt, out, err = seltext_probe(MARK)
    print(f"4. Запись AXSelectedText: {out[:100]} ({dt*1000:.0f} мс)")

    # 5. JS-путь в браузере
    low = app.lower()
    if any(b in low for b in ("brave", "chrome", "chromium", "edge",
                              "vivaldi", "opera", "arc")):
        dt, rc, out, err = js_probe(app)
        if rc == 0:
            print(f"5. JS from Apple Events: РАБОТАЕТ ({dt*1000:.0f} мс) "
                  f"activeElement={out}")
            print("   → вставка в этот браузер может быть ПРОВЕРЯЕМОЙ")
        else:
            print(f"5. JS from Apple Events: НЕДОСТУПЕН ({dt*1000:.0f} мс)")
            print(f"   ошибка: {err[:160]}")
            print("   → включить: в браузере меню «Вид → Разработчик → "
                  "Allow JavaScript from Apple Events», потом перепрогнать")
    else:
        print(f"5. JS-путь: {app} — не браузер из списка, пропуск")

    # 6. Меню «Вставить» существует?
    dt, out, err = menu_probe(app)
    print(f"6. Меню Edit→Paste у {app}: {out} ({dt*1000:.0f} мс)")

    # 7. Буфер
    t0 = time.time()
    subprocess.run(["pbcopy"], input=b"probe", capture_output=True)
    t1 = time.time()
    subprocess.run(["pbpaste"], capture_output=True)
    print(f"7. pbcopy {1000*(t1-t0):.0f} мс, pbpaste "
          f"{1000*(time.time()-t1):.0f} мс")

    print("\nСотри метки «⟪wk-дигностика⟫» из поля. Готово.")


if __name__ == "__main__":
    if sys.platform != "darwin":
        print("Этот скрипт — для Mac, здесь он ничего не замерит.")
        sys.exit(1)
    main()
