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
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

REPO = Path(__file__).resolve().parent.parent
WIGS = REPO / "wigs"

# face-mesh landmark id -> (x, y), symmetric and level
CANON = {
    10: (0, -133.1), 151: (0, -88.5), 9: (0, -41.4), 168: (0, 11.7), 6: (0, 36.4),
    70: (-160.0, -30.1), 300: (160.0, -30.1), 33: (-130.0, 26.9), 263: (130.0, 26.9),
    133: (-47.4, 23.0), 362: (47.4, 23.0), 127: (-191.1, 22.8), 356: (191.1, 22.8),
    234: (-192.9, 64.8), 454: (192.9, 64.8),
}
W, H = 1000, 1500
OX, OY = 500, 900  # where the canonical (0, 0) sits on the canvas (tall hats need headroom)
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


def finish(img, wig_id, name, order, hole=None, glow=14, glow_strength=1.0, under=None, kind="wig"):
    """Add the neon glow, cut the face hole, crop, save. `under` is composited beneath without glow."""
    if hole is not None:
        img.putalpha(ImageChops.multiply(img.getchannel("A"), ImageChops.invert(hole)))
    if glow:
        halo = img.filter(ImageFilter.GaussianBlur(glow * SS))
        if glow_strength != 1.0:
            halo.putalpha(halo.getchannel("A").point(lambda v: min(255, int(v * glow_strength))))
        out = Image.alpha_composite(halo, img)
    else:
        out = img
    if under is not None:
        out = Image.alpha_composite(under, out)
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
    (WIGS / f"{wig_id}.json").write_text(json.dumps({"name": name, "order": order, "kind": kind, "landmarks": anchors}, indent=1))
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
    finish(img, "disco-inferno", "Disco Inferno", 10, face_hole(), glow=16)


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
    finish(img, "synthwave-mullet", "Synthwave Mullet", 11, face_hole(), glow=14)


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
    finish(img, "cyber-mohawk", "Cyber Mohawk", 12, face_hole(), glow=20, glow_strength=1.3)


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
    finish(img, "the-executive", "The Executive", 13, face_hole(top=-150), glow=10)


def ellipse_poly(cx, cy, rx, ry, angle=0.0, n=60):
    """Rotated ellipse as polygon points (canvas coords)."""
    ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append(P(cx + x * ca - y * sa, cy + x * sa + y * ca))
    return pts


def sparkle(d, x, y, r, color):
    d.polygon([P(x, y - r), P(x + r * 0.22, y - r * 0.22), P(x + r, y), P(x + r * 0.22, y + r * 0.22),
               P(x, y + r), P(x - r * 0.22, y + r * 0.22), P(x - r, y), P(x - r * 0.22, y - r * 0.22)], fill=color)


def draw_top_hat(d):
    """A neon magician's top hat, worn low on the forehead. A rabbit is involved."""
    pink, cyan, gold = (255, 95, 210, 255), (0, 240, 255, 255), (255, 233, 90, 255)
    ink, ink2, sheen = (26, 10, 50, 255), (44, 18, 80, 255), (78, 36, 130, 255)
    ow = int(6 * SS)
    brim_y, top_y, bw, cw0, cw1 = -150, -575, 335, 212, 228  # brim height, crown top, half-widths
    # back half of the brim
    d.ellipse([*P(-bw, brim_y - 58), *P(bw, brim_y + 58)], fill=ink, outline=pink, width=ow)
    # the rabbit, emerging from the hat (drawn before the rim so the rim hides its feet)
    for side, ang in ((-1, -14), (1, 11)):
        cx = side * 60
        d.polygon(ellipse_poly(cx, top_y - 110, 40, 125, ang), fill=(250, 245, 255, 255))
        d.polygon(ellipse_poly(cx, top_y - 105, 19, 92, ang), fill=(255, 150, 210, 255))
    # crown, slightly flared like a proper stage hat, with a bit of shine
    d.polygon([P(-cw0, brim_y - 10), P(-cw1, top_y), P(cw1, top_y), P(cw0, brim_y - 10)], fill=ink2)
    d.polygon([P(-150, brim_y - 10), P(-160, top_y), P(-105, top_y), P(-98, brim_y - 10)], fill=sheen)
    d.line([P(-cw0, brim_y - 10), P(-cw1, top_y)], fill=pink, width=ow)
    d.line([P(cw0, brim_y - 10), P(cw1, top_y)], fill=pink, width=ow)
    # glowing band with a star buckle
    d.polygon([P(-cw0 - 1, brim_y - 24), P(-cw0 - 6, brim_y - 95), P(cw0 + 6, brim_y - 95), P(cw0 + 1, brim_y - 24)],
              fill=cyan)
    sparkle(d, 0, brim_y - 60, 44, gold)
    # crown top: the opening, then the front rim over the rabbit's feet
    d.ellipse([*P(-cw1, top_y - 44), *P(cw1, top_y + 44)], outline=pink, width=ow)
    d.chord([*P(-cw1, top_y - 44), *P(cw1, top_y + 44)], 0, 180, fill=ink2, outline=pink, width=ow)
    # front half of the brim, in front of the crown
    d.chord([*P(-bw, brim_y - 58), *P(bw, brim_y + 58)], 0, 180, fill=ink, outline=pink, width=ow)
    # stage magic
    for x, y, r, c in ((-330, -500, 36, gold), (310, -420, 26, cyan), (-300, -270, 18, pink), (330, -640, 24, gold)):
        sparkle(d, x, y, r, c)


def top_hat():
    """Ta-da."""
    img = canvas()
    draw_top_hat(ImageDraw.Draw(img))
    finish(img, "top-hat", "Top Hat", 2, glow=14)


def glaaron_in_canvas():
    """Glaaron's hair warped onto the canonical face (supersampled canvas)."""
    sys.path.insert(0, str(REPO))
    from glamcam import similarity_transform

    meta = json.loads((WIGS / "glaaron.json").read_text())
    src = np.array([meta["landmarks"][str(k)] for k in CANON])
    dst = np.array([P(*CANON[k]) for k in CANON])
    m = np.vstack([similarity_transform(src, dst), [0, 0, 1]])
    inv = np.linalg.inv(m)[:2].flatten()
    hair = Image.open(WIGS / "glaaron.png").convert("RGBA")
    return hair.transform((W * SS, H * SS), Image.AFFINE, tuple(inv), Image.BICUBIC)


def glaaron_magnifique():
    """Glaaron's hair, crowned. For galas, award shows, and Tuesday standups."""
    img = canvas()
    draw_top_hat(ImageDraw.Draw(img))
    finish(img, "glaaron-magnifique", "Glaaron Magnifique", 3, glow=14, under=glaaron_in_canvas())


def handlebar_path():
    """Centerline + thickness of the right half of a handlebar mustache, canonical coords.
    Sits between the base of the nose (y~145) and the top lip (y~190), curls up past the mouth corners."""
    sweep = bezier([(4, 168), (75, 198), (150, 190), (198, 150)], 40)
    pts = [(x, y, 64 - 42 * i / 40) for i, (x, y) in enumerate(sweep)]
    cx, cy = 196, 114
    for i in range(1, 46):  # the curl: around and up, tightening
        f = i / 45
        th = math.radians(80 - 290 * f)
        r = 36 - 22 * f
        pts.append((cx + r * math.cos(th), cy + r * math.sin(th), 22 - 18 * f))
    return pts


def offset_edges(path, side_scale=1.0):
    """Left/right outline points of a variable-width path (canonical coords)."""
    up, down = [], []
    for i, (x, y, t) in enumerate(path):
        x0, y0, _ = path[max(i - 1, 0)]
        x1, y1, _ = path[min(i + 1, len(path) - 1)]
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy) or 1
        nx, ny = -dy / n, dx / n
        h = t / 2 * side_scale
        up.append((x + nx * h, y + ny * h))
        down.append((x - nx * h, y - ny * h))
    return up, down


def mustache_shape():
    """Both halves as polygons (canvas coords)."""
    polys = []
    for side in (1, -1):
        up, down = offset_edges(handlebar_path())
        poly = [(side * x, y) for x, y in up + down[::-1]]
        polys.append([P(*q) for q in poly])
    return polys


def mustache_mask():
    """Both halves joined in the middle as one shape, with a small notch under the nose."""
    mask = Image.new("L", (W * SS, H * SS), 0)
    md = ImageDraw.Draw(mask)
    for poly in mustache_shape():
        md.polygon(poly, fill=255)
    md.ellipse([*P(-36, 138), *P(36, 198)], fill=255)
    md.polygon([P(-12, 130), P(12, 130), P(0, 150)], fill=0)
    return mask.filter(ImageFilter.GaussianBlur(1.2 * SS))


def mustache_brown():
    """Strand-by-strand: dark base, then hundreds of hairs following the curve."""
    rnd = random.Random(11)
    mask = mustache_mask()
    hair = Image.new("RGBA", (W * SS, H * SS), (62, 36, 20, 255))
    hd = ImageDraw.Draw(hair)
    path = handlebar_path()
    for _ in range(320):
        f = rnd.uniform(-1.0, 1.0)
        shade = rnd.uniform(0.75, 1.35)
        col = (int(min(255, 112 * shade)), int(min(255, 70 * shade)), int(min(255, 38 * shade)), 255)
        start = rnd.randint(0, 20)
        for side in (1, -1):
            line = []
            for i, (x, y, t) in enumerate(path[start:]):
                x0, y0, _ = path[max(start + i - 1, 0)]
                x1, y1, _ = path[min(start + i + 1, len(path) - 1)]
                n = math.hypot(x1 - x0, y1 - y0) or 1
                nx, ny = -(y1 - y0) / n, (x1 - x0) / n
                line.append(P(side * (x + nx * f * t / 2), y + ny * f * t / 2))
            hd.line(line, fill=col, width=int(1.5 * SS))
    # a glossy highlight along the top
    for side in (1, -1):
        up, _ = offset_edges(path, 0.45)
        hd.line([P(side * x, y) for x, y in up[:70]], fill=(196, 140, 88, 200), width=int(3 * SS))
    hair.putalpha(mask)
    return hair


def mustache_neon():
    """The same handlebar, bent out of glass tubing."""
    mask = mustache_mask()
    grown = mask.filter(ImageFilter.MaxFilter(2 * int(3 * SS) + 1))
    shrunk = mask.filter(ImageFilter.MinFilter(2 * int(3 * SS) + 1))
    ring = ImageChops.subtract(grown, shrunk)
    img = Image.new("RGBA", (W * SS, H * SS), (40, 12, 60, 0))
    img.putalpha(mask)
    img.alpha_composite(Image.composite(Image.new("RGBA", img.size, (255, 95, 210, 255)),
                                        Image.new("RGBA", img.size, (0, 0, 0, 0)), ring))
    d = ImageDraw.Draw(img)
    path = handlebar_path()
    spine = [P(-x, y) for x, y, t in path[::-1]] + [P(x, y) for x, y, t in path]
    d.line(spine, fill=(0, 240, 255, 255), width=int(4 * SS), joint="curve")
    return img


def stache_brown():
    """Distinguished. Twirlable. Not legally binding."""
    finish(mustache_brown(), "mustache", "The Mustache", 5, glow=0, kind="mustache")


def stache_neon():
    """Visible from space, or at least from the back of the all-hands."""
    finish(mustache_neon(), "neon-mustache", "Neon Mustache", 6, glow=12, glow_strength=1.3, kind="mustache")


def ultimate_glaaron():
    """Hair, hat, mustache. There is nothing left to add."""
    under = Image.alpha_composite(glaaron_in_canvas(), mustache_brown())
    img = canvas()
    draw_top_hat(ImageDraw.Draw(img))
    finish(img, "ultimate-glaaron", "The Ultimate Glaaron", 4, glow=14, under=under)


def draw_jeff_ears(d, neon_style):
    """Two round ears on a headband that hugs the top of the head."""
    band_box = [*P(-222, -282), *P(222, 190)]  # ellipse following the skull; we draw its top arc
    centers = [(-178, -338), (178, -338)]
    r = 104
    if neon_style:
        pink, cyan = (255, 95, 210, 255), (0, 240, 255, 255)
        d.arc(band_box, 198, 342, fill=cyan, width=int(16 * SS))
        for cx, cy in centers:
            d.ellipse([*P(cx - r, cy - r), *P(cx + r, cy + r)], fill=(40, 12, 60, 255), outline=pink,
                      width=int(9 * SS))
            d.ellipse([*P(cx - r + 26, cy - r + 26), *P(cx + r - 26, cy + r - 26)], outline=cyan,
                      width=int(4 * SS))
    else:
        d.arc(band_box, 198, 342, fill=(14, 14, 18, 255), width=int(20 * SS))
        for cx, cy in centers:
            d.ellipse([*P(cx - r, cy - r), *P(cx + r, cy + r)], fill=(12, 12, 16, 255))
            # glossy plastic highlight
            d.arc([*P(cx - r + 18, cy - r + 18), *P(cx + r - 18, cy + r - 18)], 200, 260,
                  fill=(120, 120, 135, 255), width=int(10 * SS))


def jeff_ears():
    """Classic black round ears on a headband. Jeff Mode."""
    img = canvas()
    draw_jeff_ears(ImageDraw.Draw(img), neon_style=False)
    finish(img, "jeff-ears", "Jeff Ears", 7, glow=0)


def neon_jeff_ears():
    """Jeff Mode, plugged in."""
    img = canvas()
    draw_jeff_ears(ImageDraw.Draw(img), neon_style=True)
    finish(img, "neon-jeff-ears", "Neon Jeff Ears", 8, glow=14, glow_strength=1.2)


def hoodie():
    """A sleek black athletic hoodie with a neon Onboard logo. The hood hides your hair."""
    img = canvas()
    d = ImageDraw.Draw(img)
    black, edge, seam = (16, 16, 20, 255), (44, 44, 52, 255), (34, 34, 40, 255)
    # body and shoulders, then the hood around the head
    d.polygon([P(-235, 230), P(235, 230), P(470, 600), P(-470, 600)], fill=black)
    d.ellipse([*P(-470, 430), *P(470, 760)], fill=black)
    d.ellipse([*P(-305, -360), *P(305, 330)], fill=black)
    # soft fabric shading: a lighter sheen down one side of the hood
    sheen = Image.new("L", img.size, 0)
    ImageDraw.Draw(sheen).ellipse([*P(-290, -330), *P(-120, 250)], fill=46)
    sheen = sheen.filter(ImageFilter.GaussianBlur(40 * SS))
    img.paste(Image.new("RGBA", img.size, (80, 80, 92, 255)), (0, 0), ImageChops.multiply(sheen, img.getchannel("A")))
    d = ImageDraw.Draw(img)
    d.line([P(0, -358), P(0, -170)], fill=seam, width=int(4 * SS))  # center seam over the top of the hood
    # the face opening with a rolled edge
    d.ellipse([*P(-202, -172), *P(202, 318)], fill=edge)
    hole = Image.new("L", img.size, 0)
    ImageDraw.Draw(hole).ellipse([*P(-186, -152), *P(186, 300)], fill=255)
    hole = hole.filter(ImageFilter.GaussianBlur(2 * SS))
    img.putalpha(ImageChops.multiply(img.getchannel("A"), ImageChops.invert(hole)))
    d = ImageDraw.Draw(img)
    # drawstrings with metal tips
    for side in (-1, 1):
        strand(d, [(side * 58, 300), (side * 66, 360), (side * 60, 420), (side * 70, 470)], 7,
               [(200, 200, 210, 255), (230, 230, 238, 255)])
        d.rounded_rectangle([*P(side * 70 - 7, 468), *P(side * 70 + 7, 500)], radius=3 * SS, fill=(170, 170, 185, 255))
    # neon Onboard logo on the chest
    logo = Image.open(REPO / "tools" / "onboard-logo.png").convert("RGBA")
    lw = int(300 * SS)
    logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
    cx, cy = P(0, 520)
    img.alpha_composite(logo, (int(cx - logo.width / 2), int(cy - logo.height / 2)))
    finish(img, "hoodie", "Hoodie", 9, glow=0, kind="hoodie")


def shades(neon_style):
    """Wayfarer-ish sunglasses sitting on the canonical eyes."""
    img = canvas()
    d = ImageDraw.Draw(img)
    lens = [(-194, -30), (-18, -30), (-22, 34), (-44, 82), (-100, 94), (-158, 86), (-188, 54)]
    frame = (0, 240, 255, 255) if neon_style else (12, 12, 16, 255)
    glass = (60, 18, 90, 240) if neon_style else (20, 20, 26, 252)
    for side in (-1, 1):
        pts = [P(side * x, y) for x, y in lens]
        d.polygon(pts, fill=glass)
        d.line(pts + [pts[0]], fill=frame, width=int((7 if neon_style else 11) * SS), joint="curve")
        # reflections
        glare = (255, 95, 210, 150) if neon_style else (255, 255, 255, 70)
        d.line([P(side * 156, 66), P(side * 116, -6)], fill=glare, width=int(11 * SS))
        d.line([P(side * 122, 74), P(side * 94, 22)], fill=glare, width=int(6 * SS))
        d.line([P(side * 194, -22), P(side * 212, -12)], fill=frame, width=int(10 * SS))  # arms heading back
    d.line([P(-20, -8), P(0, -14), P(20, -8)], fill=frame, width=int(9 * SS), joint="curve")  # bridge
    if neon_style:
        finish(img, "neon-shades", "Neon Shades", 8.6, glow=10, glow_strength=1.2, kind="glasses")
    else:
        finish(img, "shades", "Shades", 8.3, glow=0, kind="glasses")


if __name__ == "__main__":
    WIGS.mkdir(exist_ok=True)
    disco_inferno()
    synthwave_mullet()
    cyber_mohawk()
    the_executive()
    top_hat()
    glaaron_magnifique()
    stache_brown()
    stache_neon()
    ultimate_glaaron()
    jeff_ears()
    neon_jeff_ears()
    shades(neon_style=False)
    shades(neon_style=True)
    hoodie()
