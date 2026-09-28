#!/usr/bin/env python3
"""Плашка (pill.py): размеры и поведение совпадают с Wispr Flow.

Запуск:  python3 tests/test_pill.py

Проверяется чистая модель кадра — то, что Mac потом только рисует. Сам AppKit
здесь не запускается (на сервере его нет); окно проверяется глазами на Mac.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pill  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def settle(model, clock, seconds=0.5):
    clock.t += seconds
    return model.frame()


clock = Clock()
m = pill.PillModel(clock=clock)

# Размеры по состояниям — как в стилях Wispr Flow (положение «снизу»).
f = m.frame()
check("покой: полоска 40×8, радиус 6 → 4 (браузер урезает до половины высоты), без содержимого",
      f["pill"][2:] == (40, 8, 4) and not f["bars"] and not f["ticks"], str(f["pill"]))
check("покой: заливка чёрная 50%, обводка белая 50%",
      f["fill"] == (0, 0, 0, 0.5) and f["stroke"] == (1, 1, 1, 0.5))

m.set_state("recording")
f = settle(m, clock)
check("запись: капсула 73×30, радиус 15", f["pill"][2:] == (73, 30, 15), str(f["pill"]))
check("запись: 10 белых столбиков, спиннера нет",
      len(f["bars"]) == 10 and all(b[5][:3] == (1, 1, 1) and b[5][3] == 1 for b in f["bars"])
      and not f["ticks"])
xs = [b[0] for b in f["bars"]]
check("запись: шаг столбиков 4 (2 + зазор 2), волна по центру капсулы",
      all(abs((b - a) - 4) < 1e-9 for a, b in zip(xs, xs[1:]))
      and abs((xs[0] + xs[-1] + 2) / 2 - pill.CANVAS_W / 2) < 1e-9)

# Тишина → точки; голос → волна выше, края ниже середины.
quiet = max(b[3] for b in f["bars"])
m.set_level_rms(0.1)                      # −20 дБ: обычная громкая речь
f = settle(m, clock, 0.4)
loud = [b[3] for b in f["bars"]]
check("голос поднимает волну (тишина ≤ 3 px, речь > 8 px)",
      quiet <= 3 and max(loud) > 8, f"тишина {quiet:.1f}, речь {max(loud):.1f}")
check("края волны ниже середины", loud[0] < max(loud[3:7]) and loud[-1] < max(loud[3:7]))
check("волна не вылезает из капсулы",
      all(f["pill"][1] <= b[1] and b[1] + b[3] <= f["pill"][1] + f["pill"][3] for b in f["bars"]),
      str([round(b[3], 1) for b in f["bars"]]))
# Покачивание: одна и та же громкость, разные моменты — разные высоты.
a = [b[3] for b in settle(m, clock, 0.13)["bars"]]
b = [b[3] for b in settle(m, clock, 0.21)["bars"]]
check("волна качается и при ровном голосе", any(abs(x - y) > 0.3 for x, y in zip(a, b)))

m.set_state("processing")
f = settle(m, clock)
check("распознаю: капсула 98×30", f["pill"][2:] == (98, 30, 15), str(f["pill"]))
check("распознаю: волна — тусклые точки 2×2",
      all(b[3] == 2 and abs(b[5][3] - 0.4) < 1e-9 for b in f["bars"]))
check("распознаю: спиннер из 8 чёрточек внутри капсулы",
      len(f["ticks"]) == 8 and all(f["pill"][0] < min(t[0], t[2]) and max(t[0], t[2]) < f["pill"][0] + f["pill"][2]
                                    for t in f["ticks"]))
alphas1 = [t[5][3] for t in f["ticks"]]
alphas2 = [t[5][3] for t in settle(m, clock, 0.3)["ticks"]]
check("спиннер крутится (яркая чёрточка сдвигается)",
      alphas1.index(max(alphas1)) != alphas2.index(max(alphas2)))
check("уровень сброшен после записи", m._raw_level == 0.0)

# Переход не мгновенный: посередине 0.1 с — промежуточный размер.
m.set_state("idle")
clock.t += 0.03
mid = m.frame()["pill"][2]
f = settle(m, clock)
check("переход плавный (98 → 40 через промежуточный размер)", 40 < mid < 98, str(mid))
check("после перехода снова полоска 40×8", f["pill"][2:] == (40, 8, 4))
check("в покое анимация останавливается (не ест процессор)", not m.is_animating())

# Ошибка: красная обводка, сама уходит в покой через 1.5 с.
m.set_state("error")
f = settle(m, clock, 0.2)
check("ошибка: капсула с красной обводкой 1.5", f["stroke"] == pill.RED and f["stroke_w"] == 1.5)
f = settle(m, clock, 1.5)
check("ошибка гаснет сама через 1.5 с", f["state"] == "idle")
f = settle(m, clock)
check("…и возвращается к полоске", f["pill"][2:] == (40, 8, 4))

# Капсула стоит внизу окна по центру, с зазором 6 от края.
for st in pill.STATES:
    m.set_state(st)
    x, y, w, h, r = settle(m, clock, 0.2)["pill"]
    check(f"{st}: по центру и в 6 px от низа",
          abs(x + w / 2 - pill.CANVAS_W / 2) < 1e-9 and abs(pill.CANVAS_H - (y + h) - 6) < 1e-9)

check("уровень: тишина −60 дБ → 0, −10 дБ → 1",
      pill.level_from_rms(0.001) == 0.0 and pill.level_from_rms(0.3162) > 0.99)
try:
    m.set_state("что-то")
    check("неизвестное состояние отвергается", False)
except ValueError:
    check("неизвестное состояние отвергается", True)

# Не Mac или выключено — заглушка, диктовка идёт старым путём.
os.environ["WHISPERKEY_PILL"] = "0"
p = pill.create()
called = []
p.run_forever(on_fail=lambda: called.append(1))
check("WHISPERKEY_PILL=0 → заглушка, главный поток уходит в старый путь",
      not p.active and called == [1])
del os.environ["WHISPERKEY_PILL"]
check("не Mac → заглушка", sys.platform == "darwin" or not pill.create().active)

print()
print(f"Итого: {'ВСЁ В ПОРЯДКЕ' if not failures else 'ПРОВАЛОВ: ' + str(len(failures))}")
sys.exit(1 if failures else 0)
