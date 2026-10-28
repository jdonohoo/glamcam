#!/usr/bin/env python3
"""Draws the built-in neon wigs into wigs/.

Every wig is drawn around one shared "canonical face": the face-mesh anchor
points below, centered at (0, 0), eyes 260px apart. GlamCam maps those points
onto your real face, so anything drawn here lands in the same place on you.

    python tools/draw_wigs.py
"""
import colorsys
import json
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

WIGS = Path(__file__).resolve().parent.parent / "wigs"

# face-mesh landmark id -> (x, y), symmetric and level
CANON = {
    10: (0, -133.1), 151: (0, -88.5), 9: (0, -41.4), 168: (0, 11.7), 6: (0, 36.4),
    70: (-160.0, -30.1), 300: (160.0, -30.1), 33: (-130.0, 26.9), 263: (130.0, 26.9),
    133: (-47.4, 23.0), 362: (47.4, 23.0), 127: (-191.1, 22.8), 356: (191.1, 22.8),
    234: (-192.9, 64.8), 454: (192.9, 64.8),
}
W, H = 1000, 1150
OX, OY = 500, 560  # where the canonical (0, 0) sits on the canvas
SS = 2  # supersampling for smooth edges


def P(x, y):
    """canonical -> supersampled canvas coords"""
    return ((x + OX) * SS, (y + OY) * SS)


def neon(hue, sat=0.85, val=1.0, a=255):
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, sat, val)
    return (int(r * 255), int(g * 255), int(b * 255), a)


def bezier(pts, n=40):
    out = []
    for i in range(n + 1):
        t = i / n
        p = list(pts)
        while len(p) > 1:
            p = [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t) for a, b in zip(p, p[1:])]
        out.append(p[0])
    return out


def strand(draw, ctrl, width, colors):
    """Thick curve whose color fades through `colors` along its length."""
    pts = [P(*q) for q in bezier(ctrl)]
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        t = i / (len(pts) - 2) * (len(colors) - 1)
        lo = min(int(t), len(colors) - 2)
        f = t - lo
        c = tuple(int(colors[lo][k] + (colors[lo + 1][k] - colors[lo][k]) * f) for k in range(4))
        draw.line([a, b], fill=c, width=int(width * SS))
        r = width * SS / 2
        draw.ellipse([b[0] - r, b[1] - r, b[0] + r, b[1] + r], fill=c)


def canvas():
    return Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))


def face_hole(top=-112, extra=0):
    """Mask of the face area the wig must not cover (white = face)."""
    m = Image.new("L", (W * SS, H * SS), 0)
    d = ImageDraw.Draw(m)
    x0, y0 = P(-182 - extra, top)
    x1, y1 = P(182 + extra, 300)
    d.ellipse([x0, y0, x1, y1], fill=255)
    return m.filter(ImageFilter.GaussianBlur(3 * SS))


def finish(img, wig_id, name, hole=None, glow=14, glow_strength=1.0):
    if hole is not None:
        img.putalpha(ImageChops.multiply(img.getchannel("A"), ImageChops.invert(hole)))
    halo = img.filter(ImageFilter.GaussianBlur(glow * SS))
    if glow_strength != 1.0:
        halo.putalpha(halo.getchannel("A").point(lambda v: min(255, int(v * glow_strength))))
    out = Image.alpha_composite(halo, img)
    if hole is not None:  # the halo shouldn't fog up your face either
        out.putalpha(ImageChops.multiply(out.getchannel("A"), ImageChops.invert(hole)))
    out = out.resize((W, H), Image.LANCZOS)
    # blank invisible pixels so the PNG compresses well
    blank = Image.new("RGBA", out.size, (0, 0, 0, 0))
    out = Image.composite(out, blank, out.getchannel("A").point(lambda v: 255 if v else 0))
    box = out.getbbox()
    out = out.crop(box)
    out.save(WIGS / f"{wig_id}.png", optimize=True)
    anchors = {str(k): [round(x + OX - box[0], 1), round(y + OY - box[1], 1)] for k, (x, y) in CANON.items()}
    (WIGS / f"{wig_id}.json").write_text(json.dumps({"name": name, "landmarks": anchors}, indent=1))
    print(f"wigs/{wig_id}.png  {out.size}")


def disco_inferno():
    """A rainbow afro with a comb stuck in it, because of course."""
    rnd = random.Random(7)
    img = canvas()
    d = ImageDraw.Draw(img)
    lobes = [(0, -150, 330), (-255, 40, 165), (255, 40, 165), (-170, -260, 180), (170, -260, 180)]

    def inside(x, y):
        return any((x - cx) ** 2 + (y - cy) ** 2 < r * r for cx, cy, r in lobes)

    for cx, cy, r in lobes:
        d.ellipse([*P(cx - r, cy - r), *P(cx + r, cy + r)], fill=(40, 10, 70, 255))
    curls = []
    while len(curls) < 900:
        x, y = rnd.uniform(-440, 440), rnd.uniform(-500, 220)
        if inside(x, y):
            curls.append((x, y))
    curls.sort(key=lambda p: p[1])
    for x, y in curls:
        r = rnd.uniform(14, 26)
        hue = (math.atan2(y + 150, x) / (2 * math.pi) + 0.08 * rnd.random()) % 1
        d.ellipse([*P(x - r, y - r), *P(x + r, y + r)], fill=neon(hue, 0.9, 0.35),
                  outline=neon(hue, 0.75, 1.0), width=int(4 * SS))
    # the pick
    d.rounded_rectangle([*P(205, -545), *P(330, -490)], radius=14 * SS, fill=(255, 245, 120, 255))
    for i in range(6):
        x = 215 + i * 21
        d.rounded_rectangle([*P(x, -495), *P(x + 9, -395)], radius=4 * SS, fill=(255, 245, 120, 255))
    finish(img, "disco-inferno", "Disco Inferno", face_hole(), glow=16)


def synthwave_mullet():
    """Business in the front, party in the back, vaporwave in the soul."""
    rnd = random.Random(3)
    img = canvas()
    d = ImageDraw.Draw(img)
    pink, violet, cyan = (255, 80, 200, 255), (125, 86, 244, 255), (0, 240, 255, 255)
    # the party: long strands from behind the ears down past the shoulders
    for side in (-1, 1):
        for i in range(44):
            t = i / 43
            x0 = side * (165 + 75 * t)
            y0 = -150 + 230 * t
            drop = 380 + 160 * rnd.random()
            flick = side * (50 + 110 * rnd.random())
            strand(d, [(x0, y0), (x0 + side * (70 + 60 * t), y0 + drop * 0.35),
                       (x0 + side * 30, y0 + drop * 0.8), (x0 + flick, y0 + drop)],
                   rnd.uniform(16, 24), [pink, violet, cyan])
    # the business: a short, tidy, swept-back dome
    d.ellipse([*P(-238, -305), *P(238, 60)], fill=(150, 40, 170, 255))
    for i in range(70):
        x = -230 + 460 * i / 69
        y = -100 + 0.0022 * x * x  # hairline curves down at the temples
        ex = x * 0.5
        ey = -60 - 245 * math.sqrt(max(0.0, 1 - (ex / 250) ** 2))  # ends follow the dome
        strand(d, [(x, y), (x * 1.02, y - 90), (ex * 1.3, ey + 30), (ex, ey)],
               rnd.uniform(10, 15), [pink, (255, 120, 230, 255), violet])
    finish(img, "synthwave-mullet", "Synthwave Mullet", face_hole(), glow=14)


def cyber_mohawk():
    """Twelve inches of pure attitude. Doors may need to be taller."""
    img = canvas()
    d = ImageDraw.Draw(img)
    n = 9
    for i in range(n):
        a = math.radians(-34 + 68 * i / (n - 1))
        length = 450 - 110 * abs(i - (n - 1) / 2) / ((n - 1) / 2)
        base = (math.sin(a) * 70, -175)
        tip = (base[0] + math.sin(a) * length, base[1] - math.cos(a) * length)
        perp = (math.cos(a) * 34, math.sin(a) * 34)
        hue = 0.33 + 0.17 * i / (n - 1)
        d.polygon([P(base[0] - perp[0], base[1] - perp[1]), P(*tip), P(base[0] + perp[0], base[1] + perp[1])],
                  fill=neon(hue, 0.9, 0.55), outline=neon(hue, 0.6, 1.0), width=int(5 * SS))
    finish(img, "cyber-mohawk", "Cyber Mohawk", face_hole(), glow=20, glow_strength=1.3)


def the_executive():
    """Seven heroic hairs doing the work of seven thousand."""
    img = canvas()
    d = ImageDraw.Draw(img)
    gold, amber = (255, 225, 90, 255), (255, 150, 40, 255)
    for i in range(9):
        y = -150 - i * 13
        strand(d, [(-215, 40 - i * 8), (-235, y - 120), (40, y - 190 + i * 6), (220, y - 30)],
               11, [amber, gold, gold])
    # the lone rebel that never stays down
    strand(d, [(-30, -255), (-20, -360), (60, -390), (45, -330)], 9, [gold, gold])
    finish(img, "the-executive", "The Executive", face_hole(top=-150), glow=10)


if __name__ == "__main__":
    WIGS.mkdir(exist_ok=True)
    disco_inferno()
    synthwave_mullet()
    cyber_mohawk()
    the_executive()
