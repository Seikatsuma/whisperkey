"""Плашка внизу экрана — как у Wispr Flow.

Пока программа ждёт — тонкая полупрозрачная полоска над Dock: видно, что
WhisperKey запущен. Зажал правый Option — полоска раскрывается в чёрную капсулу
с белой волной, которая качается от голоса. Отпустил — капсула шире, волна
гаснет, рядом крутится спиннер: идёт распознавание. Текст вставлен — снова
полоска. Ошибка — капсула на полторы секунды с красной обводкой.

Все размеры, цвета и тайминги взяты из самого Wispr Flow 1.6.872 (стили окна
status в app.asar, положение «снизу»), а не на глаз:
  покой 40×8, радиус 6, заливка чёрная 50%, обводка белая 50%;
  запись 73×30, обработка 98×30, ошибка 91×30, капсула #000, обводка 1px #30302f;
  волна — 10 столбиков 2px через 2px, белые, высота = 2px × громкость ×
  форма (края ниже: 1 − d²/48) × покачивание (1 → 1.2 → 1.5 → 1.1 → 1.3 → 1 за 1 с);
  спиннер — 8 чёрточек 2×5 по кругу r=6, сиреневые rgb(163,148,199), 1.1 с;
  смена формы — 0.1 с, кривая cubic-bezier(0.05, 0.6, 0.4, 0.95).

Устройство: PillModel — чистая математика (что и где рисовать в момент t), её
проверяют tests/test_pill.py и tests/pill_preview.py на любой машине. MacPill —
тонкий слой AppKit, который только рисует кадры модели. Любой сбой плашки не
должен задеть диктовку: create() при любой проблеме отдаёт NullPill, а в работе
ошибка рисования плашку выключает, но не программу.

Выключить плашку: WHISPERKEY_PILL=0 в .env — тогда всё как раньше, баннерами.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time

# ─── Параметры Wispr Flow ─────────────────────────────────────────────────────

CANVAS_W, CANVAS_H = 120, 44     # окно чуть больше самой крупной капсулы
BOTTOM_GAP = 6                   # от нижнего края рабочей области (над Dock)

MORPH_SECONDS = 0.1
MORPH_BEZIER = (0.05, 0.6, 0.4, 0.95)
ERROR_SECONDS = 1.5

BLACK = (0.0, 0.0, 0.0, 1.0)
BORDER_DARK = (0x30 / 255, 0x30 / 255, 0x2F / 255, 1.0)
RED = (0xF8 / 255, 0x71 / 255, 0x71 / 255, 1.0)

SHAPES = {
    #            ширина высота радиус  заливка               обводка               толщина
    "idle":       (40, 8,  6,   (0.0, 0.0, 0.0, 0.5), (1.0, 1.0, 1.0, 0.5), 1.0),
    "recording":  (73, 30, 15,  BLACK,                BORDER_DARK,          1.0),
    "processing": (98, 30, 15,  BLACK,                BORDER_DARK,          1.0),
    "error":      (91, 30, 15,  BLACK,                RED,                  1.5),
}
STATES = tuple(SHAPES)

BAR_COUNT = 10
BAR_W = 2.0
BAR_GAP = 2.0
BAR_RADIUS = 0.5
BAR_LIVE = (1.0, 1.0, 1.0, 1.0)      # микрофон слушает
BAR_DIM = (1.0, 1.0, 1.0, 0.4)       # волна «уснула» (обработка, ошибка)
WAVE_KEYFRAMES = ((0.0, 1.0), (0.2, 1.2), (0.4, 1.5), (0.8, 1.1), (0.9, 1.3), (1.0, 1.0))
WAVE_PERIOD = 1.0
LEVEL_GAIN = 7.0                     # --audio-scale = max(1, 7 × уровень)
LEVEL_SMOOTH = 0.78                  # сглаживание уровня на кадр (60 кадров/с)

SPIN_TICKS = 8
SPIN_R = 6.0
SPIN_LEN = 5.0
SPIN_W = 2.0
SPIN_RGB = (163 / 255, 148 / 255, 199 / 255)
SPIN_PERIOD = 1.1
SPIN_STEP = 0.1375
SPIN_ALPHA = (0.88, 0.15)
SPIN_BOX = 16.0
PROCESSING_GAP = 6.0                 # между волной и спиннером
WAVE_PAD_X = 4.0                     # внутренние поля блока волны

# Громкость микрофона → 0..1. Тишина комнаты (≈−44 дБ и тише) — точки,
# обычная речь (−30…−20 дБ) — почти весь размах, крик (−10 дБ) — предел.
# Замер на корпусе диктовок Егора (04.10.26, 40 файлов): медиана −22 дБ,
# тихие фразы −38 дБ, паузы до −45 дБ — окно и порог подобраны под это.
LEVEL_FLOOR_DB = -50.0
LEVEL_CEIL_DB = -10.0
LEVEL_GATE = 0.15                    # raw ниже ≈−44 дБ — тишина: точки, без дрожания
LEVEL_GAMMA = 0.55                   # усиление середины: тихая речь тоже заметна


def _bezier(p1x: float, p1y: float, p2x: float, p2y: float, x: float) -> float:
    """CSS cubic-bezier: по доле времени x даёт долю пути."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    def coord(t: float, a: float, b: float) -> float:
        return 3 * a * (1 - t) ** 2 * t + 3 * b * (1 - t) * t ** 2 + t ** 3

    lo, hi = 0.0, 1.0
    for _ in range(30):
        mid = (lo + hi) / 2
        if coord(mid, p1x, p2x) < x:
            lo = mid
        else:
            hi = mid
    return coord((lo + hi) / 2, p1y, p2y)


def _ease_in_out(x: float) -> float:
    return _bezier(0.42, 0.0, 0.58, 1.0, x)


def _mix(a, b, k: float):
    if isinstance(a, tuple):
        return tuple(x + (y - x) * k for x, y in zip(a, b))
    return a + (b - a) * k


def wave_multiplier(phase: float) -> float:
    """Покачивание столбика: ключевые кадры Wispr с ease-in-out между ними."""
    phase %= 1.0
    for (t0, v0), (t1, v1) in zip(WAVE_KEYFRAMES, WAVE_KEYFRAMES[1:]):
        if phase <= t1:
            k = _ease_in_out((phase - t0) / (t1 - t0))
            return v0 + (v1 - v0) * k
    return WAVE_KEYFRAMES[-1][1]


def level_from_rms(rms: float) -> float:
    if rms <= 0.0:
        return 0.0
    db = 20.0 * math.log10(rms)
    raw = min(1.0, max(0.0, (db - LEVEL_FLOOR_DB) / (LEVEL_CEIL_DB - LEVEL_FLOOR_DB)))
    gated = max(0.0, (raw - LEVEL_GATE) / (1.0 - LEVEL_GATE))
    return gated ** LEVEL_GAMMA


class PillModel:
    """Состояние плашки и расчёт кадра. Потокобезопасно: set_* зовут из потоков
    клавиатуры, микрофона и распознавания, frame() — из потока рисования."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        now = clock()
        self.state = "idle"
        self._since = now
        self._from = self._shape("idle")
        self._raw_level = 0.0
        self._level = 0.0
        self._last_frame = now

    @staticmethod
    def _shape(state: str) -> dict:
        w, h, r, fill, stroke, sw = SHAPES[state]
        return {"w": w, "h": h, "r": r, "fill": fill, "stroke": stroke, "sw": sw}

    def set_state(self, state: str) -> None:
        if state not in SHAPES:
            raise ValueError(state)
        with self._lock:
            if state == self.state:
                return
            now = self._clock()
            self._from = self._current_shape(now)
            self.state = state
            self._since = now
            if state != "recording":
                self._raw_level = 0.0

    def set_level_rms(self, rms: float) -> None:
        """Громкость очередного куска звука (из потока микрофона)."""
        level = level_from_rms(rms)
        with self._lock:
            self._raw_level = level

    def _morph(self, now: float) -> float:
        x = (now - self._since) / MORPH_SECONDS
        return _bezier(*MORPH_BEZIER, x)

    def _current_shape(self, now: float) -> dict:
        k = self._morph(now)
        to = self._shape(self.state)
        return {key: _mix(self._from[key], to[key], k) for key in to}

    def is_animating(self, now: float | None = None) -> bool:
        with self._lock:
            now = self._clock() if now is None else now
            if self.state != "idle":
                return True
            return now - self._since < MORPH_SECONDS + 0.05

    def frame(self, now: float | None = None) -> dict:
        """Всё, что нужно нарисовать сейчас, в точках; начало — левый верхний
        угол окна CANVAS_W × CANVAS_H, ось y вниз (как в CSS)."""
        with self._lock:
            now = self._clock() if now is None else now
            if self.state == "error" and now - self._since >= ERROR_SECONDS:
                self._from = self._current_shape(now)
                self.state = "idle"
                self._since = now

            # Сглаживание уровня привязано ко времени, а не к числу кадров:
            # 0.85 на кадр при 60 кадрах/с, как в Wispr.
            dt = max(0.0, min(0.2, now - self._last_frame))
            self._last_frame = now
            keep = LEVEL_SMOOTH ** (dt * 60.0)
            self._level = self._level * keep + self._raw_level * (1.0 - keep)

            shape = self._current_shape(now)
            state = self.state
            reveal = self._morph(now)
            level = self._level

        w, h = shape["w"], shape["h"]
        x = (CANVAS_W - w) / 2.0
        y = CANVAS_H - BOTTOM_GAP - h
        out = {
            "state": state,
            "pill": (x, y, w, h, min(shape["r"], h / 2.0)),
            "fill": shape["fill"],
            "stroke": shape["stroke"],
            "stroke_w": shape["sw"],
            "bars": [],
            "ticks": [],
        }
        if state == "idle":
            return out

        cy = y + h / 2.0
        wave_w = BAR_COUNT * BAR_W + (BAR_COUNT - 1) * BAR_GAP
        if state == "processing":
            block = (wave_w + 2 * WAVE_PAD_X) + PROCESSING_GAP + SPIN_BOX
            wave_x = CANVAS_W / 2.0 - block / 2.0 + WAVE_PAD_X
            spin_cx = CANVAS_W / 2.0 + block / 2.0 - SPIN_BOX / 2.0
        else:
            wave_x = CANVAS_W / 2.0 - wave_w / 2.0
            spin_cx = None

        live = state == "recording"
        audio_scale = max(1.0, LEVEL_GAIN * level) if live else 1.0
        center = (BAR_COUNT - 1) / 2.0
        half = math.ceil(BAR_COUNT / 2)
        for i in range(BAR_COUNT):
            d = abs(center - i)
            shape_k = max(0.0, 1.0 - d * d / 48.0)
            if live:
                offset = i if i < half else i - BAR_COUNT     # задержки Wispr: 0.1 с × смещение
                mult = wave_multiplier((now - self._since - 0.1 * offset) / WAVE_PERIOD)
                bh = BAR_W * audio_scale * shape_k * mult
                rgba = BAR_LIVE
            else:
                bh = BAR_W                                     # без анимации — точки 2×2
                rgba = BAR_DIM
            bx = wave_x + i * (BAR_W + BAR_GAP)
            out["bars"].append((bx, cy - bh / 2.0, BAR_W, bh, BAR_RADIUS,
                                rgba[:3] + (rgba[3] * reveal,)))

        if spin_cx is not None:
            t = now - self._since
            for i in range(SPIN_TICKS):
                ang = math.radians(45.0 * i)
                dx, dy = math.sin(ang), -math.cos(ang)
                r0 = SPIN_R - SPIN_LEN / 2.0 + SPIN_W / 2.0
                r1 = SPIN_R + SPIN_LEN / 2.0 - SPIN_W / 2.0
                phase = ((t + (SPIN_TICKS - 1 - i) * SPIN_STEP) % SPIN_PERIOD) / SPIN_PERIOD
                a = SPIN_ALPHA[0] + (SPIN_ALPHA[1] - SPIN_ALPHA[0]) * phase
                out["ticks"].append((spin_cx + dx * r0, cy + dy * r0,
                                     spin_cx + dx * r1, cy + dy * r1,
                                     SPIN_W, SPIN_RGB + (a * reveal,)))
        return out


# ─── Без плашки ───────────────────────────────────────────────────────────────

class NullPill:
    """Плашки нет (не Mac, выключена, AppKit не загрузился) — всё как раньше."""
    active = False

    def set_state(self, state: str) -> None:
        pass

    def set_level_rms(self, rms: float) -> None:
        pass

    def run_forever(self, on_fail) -> None:
        on_fail()


# ─── Mac: окно поверх всех, не перехватывает ни фокус, ни мышь ───────────────

class MacPill:
    active = True

    def __init__(self):
        self.model = PillModel()
        self._view = None
        self._panel = None
        self._timer = None
        self._broken = False
        self._appkit = None

    # Эти два зовут из любых потоков: модель под замком, окно трогаем только
    # из главного потока через callAfter.
    def set_state(self, state: str) -> None:
        if self._broken:
            return
        self.model.set_state(state)
        if self._appkit is not None:
            self._appkit["callAfter"](self._wake, state == "recording")

    def set_level_rms(self, rms: float) -> None:
        self.model.set_level_rms(rms)

    def run_forever(self, on_fail) -> None:
        """Главный поток: цикл событий macOS. Ctrl+C завершает процесс целиком
        (AppKit выходит сам, finally в main() может не успеть — это не страшно:
        перехват клавиатуры система снимает вместе с процессом).
        Не поднялось окно — сразу on_fail() (старый путь: listener.join)."""
        try:
            self._build()
        except Exception as e:
            print(f"[pill] плашка не запустилась, работаю без неё: {type(e).__name__}: {e}")
            self._broken = True
            on_fail()
            return
        self._appkit["runEventLoop"](installInterrupt=True)

    def _build(self) -> None:
        import objc
        from AppKit import (NSApplication, NSBezierPath, NSColor, NSEvent,
                            NSMakeRect, NSPanel, NSRunLoop, NSScreen, NSTimer, NSView)
        from Foundation import NSDefaultRunLoopMode
        from PyObjCTools import AppHelper

        try:
            from AppKit import NSRunLoopCommonModes as common_modes
        except ImportError:
            try:
                from Foundation import NSRunLoopCommonModes as common_modes
            except ImportError:
                common_modes = NSDefaultRunLoopMode

        owner = self

        class PillView(NSView):
            def isFlipped(self):
                return True

            def drawRect_(self, rect):
                try:
                    owner._draw(owner.model.frame())
                except Exception as e:
                    owner._fail(e)

            def tick_(self, timer):
                try:
                    self.setNeedsDisplay_(True)
                    if not owner.model.is_animating():
                        owner._stop_timer()
                except Exception as e:
                    owner._fail(e)

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(1)          # Accessory: без значка в Dock и меню

        style = 0 | (1 << 7)                 # Borderless | NonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, CANVAS_W, CANVAS_H), style, 2, False)   # 2 = Buffered
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)   # клики проходят сквозь плашку
        panel.setLevel_(25)                  # NSStatusWindowLevel: поверх окон
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        # Все рабочие столы, поверх полноэкранных, не в Cmd+` и Exposé.
        panel.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 6) | (1 << 8))

        view = PillView.alloc().initWithFrame_(NSMakeRect(0, 0, CANVAS_W, CANVAS_H))
        panel.setContentView_(view)

        self._objc = {"NSBezierPath": NSBezierPath, "NSColor": NSColor,
                      "NSMakeRect": NSMakeRect, "NSScreen": NSScreen, "NSEvent": NSEvent,
                      "NSTimer": NSTimer, "NSRunLoop": NSRunLoop, "modes": common_modes}
        self._view = view
        self._panel = panel
        self._place()
        panel.orderFrontRegardless()
        self._appkit = {"callAfter": AppHelper.callAfter,
                        "runEventLoop": AppHelper.runEventLoop}
        self._wake(False)

    def _place(self) -> None:
        """По центру снизу того экрана, где сейчас мышь, над Dock."""
        o = self._objc
        mouse = o["NSEvent"].mouseLocation()
        target = None
        for screen in o["NSScreen"].screens():
            f = screen.frame()
            if (f.origin.x <= mouse.x <= f.origin.x + f.size.width
                    and f.origin.y <= mouse.y <= f.origin.y + f.size.height):
                target = screen
                break
        target = target or o["NSScreen"].mainScreen()
        vf = target.visibleFrame()
        x = vf.origin.x + (vf.size.width - CANVAS_W) / 2.0
        y = vf.origin.y
        self._panel.setFrameOrigin_((round(x), round(y)))

    def _wake(self, replace: bool) -> None:
        if self._broken or self._view is None:
            return
        try:
            if replace:
                self._place()
            self._view.setNeedsDisplay_(True)
            if self._timer is None and self.model.is_animating():
                o = self._objc
                self._timer = o["NSTimer"].timerWithTimeInterval_target_selector_userInfo_repeats_(
                    1.0 / 60.0, self._view, "tick:", None, True)
                o["NSRunLoop"].currentRunLoop().addTimer_forMode_(self._timer, o["modes"])
        except Exception as e:
            self._fail(e)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _fail(self, e: Exception) -> None:
        if self._broken:
            return
        self._broken = True
        print(f"[pill] сбой рисования, плашка выключена до перезапуска: {type(e).__name__}: {e}")
        try:
            self._stop_timer()
            if self._panel is not None:
                self._panel.orderOut_(None)
        except Exception:
            pass

    def _draw(self, f: dict) -> None:
        o = self._objc
        color = lambda c: o["NSColor"].colorWithSRGBRed_green_blue_alpha_(*c)
        rect = o["NSMakeRect"]
        path_rr = o["NSBezierPath"].bezierPathWithRoundedRect_xRadius_yRadius_

        x, y, w, h, r = f["pill"]
        color(f["fill"]).set()
        path_rr(rect(x, y, w, h), r, r).fill()
        sw = f["stroke_w"]
        if sw > 0:
            p = path_rr(rect(x + sw / 2, y + sw / 2, w - sw, h - sw),
                        max(0.0, r - sw / 2), max(0.0, r - sw / 2))
            p.setLineWidth_(sw)
            color(f["stroke"]).set()
            p.stroke()

        for bx, by, bw, bh, br, c in f["bars"]:
            color(c).set()
            path_rr(rect(bx, by, bw, bh), min(br, bh / 2), min(br, bw / 2)).fill()

        for x1, y1, x2, y2, lw, c in f["ticks"]:
            p = o["NSBezierPath"].bezierPath()
            p.moveToPoint_((x1, y1))
            p.lineToPoint_((x2, y2))
            p.setLineWidth_(lw)
            p.setLineCapStyle_(1)            # скруглённые концы
            color(c).set()
            p.stroke()


def create():
    """Плашка для этой машины: на Mac — настоящая, иначе заглушка."""
    if os.environ.get("WHISPERKEY_PILL", "1").strip().lower() in ("0", "no", "off", "false"):
        return NullPill()
    if sys.platform != "darwin":
        return NullPill()
    try:
        import AppKit  # noqa: F401 — приезжает вместе с pynput (pyobjc)
        from PyObjCTools import AppHelper  # noqa: F401
    except Exception as e:
        print(f"[pill] AppKit недоступен, плашки не будет: {e}")
        return NullPill()
    return MacPill()
