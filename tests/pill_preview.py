#!/usr/bin/env python3
"""Картинки плашки без Mac: рисует кадры PillModel так же, как их рисует
MacPill (те же прямоугольники, скругления, цвета), в ×2 как на Retina.

Запуск:  python3 tests/pill_preview.py [папка]   (по умолчанию /tmp/pill_preview)
Выход:   states.png — покой / запись / распознаю / ошибка на тёмном и светлом фоне;
         recording.gif — две секунды записи с «голосом».
Нужен Pillow (на Mac не нужен — это только для просмотра).
"""
import math
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pill  # noqa: E402

S = 4          # рисуем в ×4 и сжимаем до ×2 — сглаживание краёв, как у macOS


def _rgba(c, bg):
    return tuple(int(round(v * 255)) for v in c[:3]) + (int(round(c[3] * 255)),)


def render(frame, bg=(30, 30, 34)):
    W, H = pill.CANVAS_W * S, pill.CANVAS_H * S
    base = Image.new("RGBA", (W, H), bg + (255,))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    def over(draw_fn, color):
        tmp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_fn(ImageDraw.Draw(tmp), _rgba(color, bg))
        layer.alpha_composite(tmp)

    x, y, w, h, r = frame["pill"]
    over(lambda d, c: d.rounded_rectangle([x * S, y * S, (x + w) * S - 1, (y + h) * S - 1],
                                          radius=r * S, fill=c), frame["fill"])
    sw = frame["stroke_w"]
    if sw > 0:
        over(lambda d, c: d.rounded_rectangle([x * S, y * S, (x + w) * S - 1, (y + h) * S - 1],
                                              radius=r * S, outline=c, width=max(1, round(sw * S))),
             frame["stroke"])
    for bx, by, bw, bh, br, c in frame["bars"]:
        over(lambda d, col: d.rounded_rectangle([bx * S, by * S, (bx + bw) * S - 1, (by + bh) * S - 1],
                                                radius=min(br, bh / 2) * S, fill=col), c)
    for x1, y1, x2, y2, lw, c in frame["ticks"]:
        def tick(d, col):
            d.line([x1 * S, y1 * S, x2 * S, y2 * S], fill=col, width=round(lw * S))
            rr = lw * S / 2
            for px, py in ((x1, y1), (x2, y2)):
                d.ellipse([px * S - rr, py * S - rr, px * S + rr, py * S + rr], fill=col)
        over(tick, c)

    base.alpha_composite(layer)
    return base.resize((pill.CANVAS_W * 2, pill.CANVAS_H * 2), Image.LANCZOS)


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def state_frame(state, level_rms=0.0, at=0.37):
    clock = Clock()
    m = pill.PillModel(clock=clock)
    m.set_state(state)
    if level_rms:
        m.set_level_rms(level_rms)
    for _ in range(int(at * 60)):          # прокрутить сглаживание как в жизни
        clock.t += 1 / 60
        f = m.frame()
    return f


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pill_preview"
    os.makedirs(out, exist_ok=True)
    cases = [("покой", "idle", 0), ("запись, тихо", "recording", 0.004),
             ("запись, голос", "recording", 0.06), ("распознаю", "processing", 0),
             ("ошибка", "error", 0)]
    tiles = []
    for bg in ((30, 30, 34), (236, 236, 240)):
        row = [render(state_frame(st, lv), bg) for _, st, lv in cases]
        tiles.append(row)
    tw, th = tiles[0][0].size
    sheet = Image.new("RGBA", (tw * len(cases), th * 2), (0, 0, 0, 255))
    for j, row in enumerate(tiles):
        for i, t in enumerate(row):
            sheet.paste(t, (i * tw, j * th))
    sheet.save(os.path.join(out, "states.png"))

    # Две секунды записи: голос нарастает и стихает, как в фразе.
    clock = Clock()
    m = pill.PillModel(clock=clock)
    frames = []
    m.set_state("recording")
    for k in range(120):
        clock.t += 1 / 60
        env = math.sin(math.pi * k / 120) ** 2
        m.set_level_rms(0.002 + 0.08 * env * (0.7 + 0.3 * math.sin(k * 0.9)))
        f = m.frame()
        if k % 2 == 0:
            frames.append(render(f).convert("P", palette=Image.ADAPTIVE))
    frames[0].save(os.path.join(out, "recording.gif"), save_all=True,
                   append_images=frames[1:], duration=33, loop=0)
    print(out)


if __name__ == "__main__":
    main()
