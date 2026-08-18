#!/usr/bin/env python3
"""Generate the USBme Photo Cast app icon.

Draws a framed 6-colour e-ink "photo" (blue sky, yellow sun, white cloud,
green + red hills, a black bird — one accent per panel colour) inside a
white mat on a dark rounded app tile, then exports:

    assets/icon.png                 1024x1024 master (also the runtime/Linux icon)
    assets/USBmePhotoCast.ico       multi-size Windows icon

Re-run this whenever the icon changes:  python assets/make_icon.py
Pure Pillow, no SVG renderer or numpy required.
"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024

# 6-colour e-ink palette (vivid, legible at small sizes)
BLUE  = (46, 127, 184)
YEL   = (242, 194, 48)
GREEN = (39, 155, 90)
RED   = (200, 65, 55)
WHITE = (249, 249, 246)
BLACK = (26, 26, 26)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def build():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- app tile: rounded square with a soft vertical gradient ---
    margin = int(S * 0.055)
    tile = (margin, margin, S - margin, S - margin)
    trad = int(S * 0.205)
    top, bot = (43, 61, 82), (23, 34, 47)
    col = Image.new("RGB", (1, S))
    for y in range(S):
        col.putpixel((0, y), lerp(top, bot, y / (S - 1)))
    grad = col.resize((S, S))
    tmask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tmask).rounded_rectangle(tile, radius=trad, fill=255)
    img.paste(grad, (0, 0), tmask)

    # --- white mat (the frame) ---
    inset = int(S * 0.15)
    screen = (tile[0] + inset, tile[1] + inset, tile[2] - inset, tile[3] - inset)
    srad = int(S * 0.085)
    d.rounded_rectangle(screen, radius=srad, fill=(248, 247, 244, 255))

    # --- the "photo" inside the mat ---
    pad = int(S * 0.032)
    cx0, cy0 = screen[0] + pad, screen[1] + pad
    cx1, cy1 = screen[2] - pad, screen[3] - pad
    cw, ch = cx1 - cx0, cy1 - cy0
    scene = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scene)

    sd.rectangle((0, 0, cw, ch), fill=BLUE)                       # sky
    sr = int(min(cw, ch) * 0.16)                                  # sun
    scx, scy = int(cw * 0.73), int(ch * 0.29)
    sd.ellipse((scx - sr, scy - sr, scx + sr, scy + sr), fill=YEL)

    def blob(fx, fy, fr):                                         # cloud
        r = int(ch * fr)
        cx, cy = int(cw * fx), int(ch * fy)
        sd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WHITE)
    blob(0.26, 0.28, 0.070)
    blob(0.35, 0.25, 0.092)
    blob(0.44, 0.29, 0.066)

    sd.polygon([(int(cw * -0.05), ch), (int(cw * 0.40), int(ch * 0.44)),
                (int(cw * 0.64), ch)], fill=GREEN)                # green hill
    sd.polygon([(int(cw * 0.44), ch), (int(cw * 0.77), int(ch * 0.54)),
                (int(cw * 1.10), ch)], fill=RED)                  # red hill

    bx, by = int(cw * 0.49), int(ch * 0.15)                       # bird
    bs, bw = int(ch * 0.055), max(3, int(ch * 0.013))
    sd.line([(bx - bs, by), (bx, int(by - bs * 0.55))], fill=BLACK, width=bw)
    sd.line([(bx, int(by - bs * 0.55)), (bx + bs, by)], fill=BLACK, width=bw)

    crad = int(S * 0.048)
    cmask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(cmask).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=crad, fill=255)
    img.paste(scene, (cx0, cy0), cmask)
    d.rounded_rectangle((cx0, cy0, cx1, cy1), radius=crad,
                        outline=(38, 53, 68, 255), width=max(2, int(S * 0.006)))
    return img


def main():
    img = build()
    png = os.path.join(HERE, "icon.png")
    ico = os.path.join(HERE, "USBmePhotoCast.ico")
    img.save(png)
    img.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", png)
    print("wrote", ico)


if __name__ == "__main__":
    main()
