#!/usr/bin/env python3
"""GlamCam: a face-tracking wig for your webcam.

Tracks your face with MediaPipe, glues a transparent hair PNG to it (follows
position, size and head tilt), blurs your background, and sends the result to
the OBS Virtual Camera so Google Meet / Zoom / Teams can use it.

Keys (click the preview window first):
  w/a/s/d  nudge the wig up/left/down/right     - / =  shrink / grow
  n        next wig                              h      wig on/off
  b        background blur on/off                l      lighting match on/off
  x        hide your real hair on/off            f      cave mode: freeze the costume, lights off
  m        mustache on any wig (cycles)          k      floating keys window (cmd+K too)
  o        hoodie mode on/off                    g      sunglasses (cycles)
  v        hide me: just the props, on black     z      room mode: zoom out for more wall
           (in cave mode: ghost mode, your frozen avatar + props, never the camera)
  u  1-5   load a saved scene                    i  1-5  save this look as scene 1-5
  c        puppies & kittens on/off              j      send one across right now
  1-5      neon props behind you (0 clears)      t      who's in the aquarium (cycles)
  drag     move a prop with the mouse            r      put the props back where they started
  scroll   resize the prop under the mouse       [ ]    shrink / grow the last prop you grabbed
  dbl-click  a prop: in front of you / back on the wall
  in cave mode: drag / scroll your frozen costume to move or resize it
  p        save the wig fit
  q/Esc    quit
"""
import argparse
import contextlib
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

HERE = Path(__file__).resolve().parent
# Packaged as GlamCam.app / GlamCam.exe: models and built-in wigs ship inside the (read-only)
# bundle, and your own wigs + saved fits live in a per-user folder instead of the repo.
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    BUNDLE = Path(getattr(sys, "_MEIPASS", HERE))
    if sys.platform == "darwin":
        USER_DIR = Path.home() / "Library" / "Application Support" / "GlamCam"
    elif sys.platform == "win32":
        USER_DIR = Path(os.environ.get("APPDATA", Path.home())) / "GlamCam"
    else:
        USER_DIR = Path.home() / ".glamcam"
    USER_DIR.mkdir(parents=True, exist_ok=True)
else:
    BUNDLE = USER_DIR = HERE
MODEL_DIR = BUNDLE / "models"
WIG_DIRS = [BUNDLE / "wigs", USER_DIR / "my-wigs"]
PROP_DIR = BUNDLE / "props"
FIT_FILE = USER_DIR / "fit.json"
PROP_SPOTS_FILE = USER_DIR / "prop-positions.json"  # where you dragged the props to
SCENES_FILE = USER_DIR / "scenes.json"  # up to 5 saved looks (u + 1-5 to load, i + 1-5 to save)
WINDOW = "GlamCam"  # never changes, so OBS Window Capture finds it again after a restart

_MODEL_BASE = "https://storage.googleapis.com/mediapipe-models"
MODELS = {
    "face_landmarker.task": f"{_MODEL_BASE}/face_landmarker/face_landmarker/float16/latest/face_landmarker.task",
    "selfie_segmenter.tflite": f"{_MODEL_BASE}/image_segmenter/selfie_segmenter/float16/latest/selfie_segmenter.tflite",
    "hair_segmenter.tflite": f"{_MODEL_BASE}/image_segmenter/hair_segmenter/float32/latest/hair_segmenter.tflite",
}

# Face-mesh points a wig is pinned to: forehead, brows, eye corners, temples.
# Nothing from the mouth or jaw, so talking doesn't make the wig bounce.
ANCHOR_IDS = [10, 151, 9, 168, 6, 33, 133, 362, 263, 127, 356, 234, 454, 70, 300]


def model_path(name):
    path = MODEL_DIR / name
    if not path.exists():
        download_models([name])
    return str(path)


def download_models(names=MODELS):
    MODEL_DIR.mkdir(exist_ok=True)
    try:
        import certifi  # python.org builds on macOS ship without CA certificates
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    for name in names:
        dest = MODEL_DIR / name
        if dest.exists():
            continue
        print(f"Downloading {name} ...")
        try:
            with urllib.request.urlopen(MODELS[name], context=ctx) as r:
                data = r.read()
        except OSError as e:
            sys.exit(f"Could not download {MODELS[name]}\n  {e}\n"
                     f"Download it in a browser and save it as {dest}")
        dest.write_bytes(data)


@contextlib.contextmanager
def quiet_native_logs():
    """MediaPipe's C++ side prints a wall of INFO/WARNING lines at startup; mute fd 2 meanwhile."""
    try:
        if sys.stderr:
            sys.stderr.flush()
        saved = os.dup(2)
    except OSError:  # windowed app with no stderr: nothing to mute
        yield
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(devnull)
        os.close(saved)


class MicLevel:
    """How loud you are right now (0..1), from the default microphone. Drives the Cave Mode mouth.
    Optional: if there's no mic or no permission, it just stays at 0."""

    def __init__(self):
        self.level, self.stream, self.failed = 0.0, None, False

    def start(self):
        if self.stream or self.failed:
            return
        try:
            import sounddevice as sd

            def hear(indata, frames, t, status):
                rms = float(np.sqrt(np.mean(np.square(indata, dtype=np.float32))))
                loud = float(np.clip((20 * np.log10(rms + 1e-7) + 52) / 30, 0, 1))  # -52 dB silent .. -22 dB shouting
                # open fast, close a bit slower, like a real mouth
                self.level = loud if loud > self.level else 0.75 * self.level + 0.25 * loud

            self.stream = sd.InputStream(channels=1, blocksize=512, callback=hear)
            self.stream.start()
        except Exception as e:  # no mic, no PortAudio, or permission denied
            self.failed = True
            print(f"Cave Mode mouth: no microphone ({e.__class__.__name__}); it will follow your lips when lit.")

    def stop(self):
        if self.stream:
            self.stream.close()
            self.stream = None


def similarity_transform(src, dst):
    """Least-squares scale + rotation + translation mapping src points onto dst (Umeyama)."""
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s, d = src - mu_s, dst - mu_d
    u, sig, vt = np.linalg.svd(d.T @ s / len(src))
    flip = np.diag([1.0, np.sign(np.linalg.det(u @ vt))])
    rot = u @ flip @ vt
    scale = (sig * np.diag(flip)).sum() / s.var(0).sum()
    m = np.zeros((2, 3))
    m[:, :2] = scale * rot
    m[:, 2] = mu_d - m[:, :2] @ mu_s
    return m


def to_mp_image(bgr):
    return mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))


# ---------------------------------------------------------------- wigs

class Wig:
    def __init__(self, png_path):
        self.id = png_path.stem
        meta = json.loads(png_path.with_suffix(".json").read_text())
        self.name = meta.get("name", self.id)
        self.order = meta.get("order", 1000)  # built-in wigs set this; yours sort after, alphabetically
        self.image = cv2.imread(str(png_path), cv2.IMREAD_UNCHANGED)
        if self.image is None or self.image.ndim != 3 or self.image.shape[2] != 4:
            raise ValueError(f"{png_path} is not a transparent PNG")
        self.idx = [int(k) for k in meta["landmarks"]]
        if not set(self.idx) <= set(ANCHOR_IDS):
            raise ValueError("anchor landmarks must come from ANCHOR_IDS")
        self.ref = np.array(list(meta["landmarks"].values()), dtype=np.float64)
        self.kind = meta.get("kind", "wig")  # "mustache" ones can also be layered over any wig with m
        # brightness of the skin in the source photo; drawn (neon) wigs have none and keep their glow
        self.skin_luma = meta.get("skin_luma")


def skin_luma(bgr, eye_r, eye_l):
    """Median brightness of the cheeks-and-nose patch under the eyes (eye_r/eye_l = landmarks 33/263)."""
    d = np.linalg.norm(eye_l - eye_r)
    cx, cy = (eye_r + eye_l) / 2
    x0, x1 = int(cx - 0.35 * d), int(cx + 0.35 * d)
    y0, y1 = int(cy + 0.2 * d), int(cy + 0.55 * d)
    h, w = bgr.shape[:2]
    patch = bgr[max(y0, 0):min(y1, h), max(x0, 0):min(x1, w)]
    if patch.size == 0:
        return None
    return float(np.median(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)))


def load_wigs():
    wigs = []
    for d in WIG_DIRS:
        for png in sorted(d.glob("*.png")):
            if not png.with_suffix(".json").exists():
                continue
            try:
                wigs.append(Wig(png))
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                print(f"Skipping {png.name}: {e}")
    return sorted(wigs, key=lambda wg: wg.order)  # stable: ties keep folder + name order


class Critters:
    """Neon puppies and kittens that run across the bottom of the screen now and then."""

    KINDS = ["puppy-golden", "puppy-neon", "kitten-orange", "kitten-neon"]

    def __init__(self):
        self.frames = {}
        for kind in self.KINDS:
            imgs = [cv2.imread(str(PROP_DIR / "critters" / f"{kind}-{k}.png"), cv2.IMREAD_UNCHANGED) for k in range(6)]
            if all(i is not None for i in imgs):
                self.frames[kind] = imgs
        self.on = False
        self.runners = []  # [kind, start ms, direction, speed, size]
        self.next_at = None
        self.rng = np.random.default_rng()
        self._sized = {}

    def send(self, ts):
        """Send one running across right now."""
        if not self.frames:
            return
        kind = self.rng.choice(list(self.frames))
        self.runners.append([kind, ts, int(self.rng.choice([-1, 1])), float(self.rng.uniform(0.22, 0.38)),
                             float(self.rng.uniform(0.13, 0.19))])

    def _sprite(self, kind, k, size, h, facing):
        key = (kind, k, round(size, 3), h, facing)
        if key not in self._sized:
            img = self.frames[kind][k]
            z = size * h / img.shape[0]
            img = cv2.resize(img, None, fx=z, fy=z, interpolation=cv2.INTER_AREA)
            self._sized[key] = img if facing > 0 else cv2.flip(img, 1)
            if len(self._sized) > 400:
                self._sized.clear()
        return self._sized[key]

    def draw(self, img, ts, blit):
        if self.on:
            if self.next_at is None:
                self.next_at = ts + self.rng.uniform(2000, 6000)
            if ts >= self.next_at:
                self.send(ts)
                self.next_at = ts + self.rng.uniform(6000, 20000)  # every 6-20 seconds, at random
        h, w = img.shape[:2]
        alive = []
        for run in self.runners:
            kind, start, direction, speed, size = run
            t = (ts - start) / 1000
            sprite = self._sprite(kind, int(t * 12) % 6, size, h, direction)
            sw = sprite.shape[1]
            dist = t * speed * w  # speed: screen widths per second
            x = int(-sw + dist) if direction > 0 else int(w - dist)
            if dist > w + sw:
                continue  # made it across
            alive.append(run)
            blit(img, sprite, x, h - sprite.shape[0] - int(0.01 * h))
        self.runners = alive
        return img


class Prop:
    """A neon object for the wall behind you. Placed in frame fractions, drawn behind your silhouette."""

    def __init__(self, png_path):
        meta = json.loads(png_path.with_suffix(".json").read_text())
        self.id = png_path.stem
        self.name = meta.get("name", self.id)
        self.key = str(meta["key"])
        self.x, self.top, self.height = meta["x"], meta["top"], meta["height"]
        self.default_spot = (self.x, self.top, self.height)
        self.front = False  # double-click: draw in front of you instead of on the wall behind
        self.bob = meta.get("bob", 0.0)  # floats up and down by this fraction of the frame
        self.flicker = meta.get("flicker", False)  # neon-sign stutter
        self.images = {"default": self._load(png_path)}
        for name, fname in meta.get("variants", {}).items():  # e.g. the reef version of the aquarium
            self.images[name] = self._load(PROP_DIR / fname)
        # optional animated swimmers (the aquarium): sprites live in props/sprites/
        self.swim = meta.get("swimmers")
        for f in self.all_fish():
            f["images"] = [self._load(PROP_DIR / "sprites" / n) for n in f.get("frames", [f.get("sprite")])]
        self._sized = {}

    def place(self, x, top, height=None):
        self.x, self.top = x, top
        if height is not None and height != self.height:
            self.height = float(np.clip(height, 0.05, 1.5))
            self._sized.clear()  # re-scale the art (and fish) at the new size
            for f in self.all_fish():
                f.pop("sized", None)

    def resize(self, factor):
        """Grow or shrink around the prop's center."""
        old = self.height
        self.place(self.x, self.top, old * factor)
        self.top += (old - self.height) / 2

    @staticmethod
    def _load(path):
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if img is None or img.ndim != 3 or img.shape[2] != 4:
            raise ValueError(f"{path.name} is missing or not a transparent PNG")
        return img

    def all_fish(self):
        return [f for school in (self.swim or {}).get("species", {}).values() for f in school]

    def school(self, species):
        """Fish for one species, or a whole group (freshwater / saltwater community, party mode)."""
        everyone = self.swim["species"]
        group = self.swim.get("groups", {}).get(species)
        if group:
            return [f for name in group for f in everyone[name]]
        return everyone.get(species, everyone[self.swim["default"]])

    def variant(self, species):
        return (self.swim or {}).get("tank", {}).get(species, "default")

    def sized(self, frame_h, variant="default"):
        key = (frame_h, variant)
        if key not in self._sized:
            img = self.images.get(variant, self.images["default"])
            k = self.height * frame_h / img.shape[0]
            self._sized[key] = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        return self._sized[key]

    def fish_views(self, f, frame_h):
        """Every animation frame of a fish, facing right / left / up / down, sized for this frame height."""
        cache = f.setdefault("sized", {})
        if frame_h not in cache:
            frames = []
            for img in f["images"]:
                k = f["size"] * self.height * frame_h / img.shape[0]
                right = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
                frames.append({"right": right, "left": cv2.flip(right, 1),
                               "up": cv2.rotate(right, cv2.ROTATE_90_COUNTERCLOCKWISE),
                               "down": cv2.rotate(right, cv2.ROTATE_90_CLOCKWISE)})
            cache[frame_h] = frames
        return cache[frame_h]


def load_props():
    props = []
    for png in sorted(PROP_DIR.glob("*.png")):
        if not png.with_suffix(".json").exists():  # e.g. aquarium-reef.png is a variant, not a prop
            continue
        try:
            props.append(Prop(png))
        except (ValueError, KeyError, json.JSONDecodeError, OSError) as e:
            print(f"Skipping prop {png.name}: {e}")
    try:
        spots = json.loads(PROP_SPOTS_FILE.read_text()) if PROP_SPOTS_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        spots = {}
    for pr in props:
        if pr.id in spots:
            spot = spots[pr.id]
            pr.place(*spot[:3])
            pr.front = bool(spot[3]) if len(spot) > 3 else False
    return sorted(props, key=lambda pr: pr.key)


def save_prop_spots(props):
    """Remember every prop that has been dragged away from its default spot."""
    moved = {pr.id: [round(pr.x, 4), round(pr.top, 4), round(pr.height, 4), pr.front]
             for pr in props if (pr.x, pr.top, pr.height) != pr.default_spot or pr.front}
    PROP_SPOTS_FILE.write_text(json.dumps(moved, indent=1) + "\n")


def scene_snapshot(r, room):
    """Everything about your current look, as plain data."""
    return {
        "wig": r.wig.id, "mustache": r.stache.id if r.stache else None,
        "hoodie": r.hoodie.id if r.hoodie else None, "glasses": r.glasses.id if r.glasses else None,
        "props": [pr.id for pr in r.props], "aquarium": r.species,
        "spots": {pr.id: [round(pr.x, 4), round(pr.top, 4), round(pr.height, 4), pr.front] for pr in r.all_props},
        "blur": r.blur, "hide_hair": r.hide_hair, "relight": r.relight, "room": room, "critters": r.critters.on,
    }


def load_scenes():
    try:
        return json.loads(SCENES_FILE.read_text()) if SCENES_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def describe_scene(scene):
    """Short label for a saved scene, e.g. 'top-hat + hoodie, 2 props'."""
    bits = [scene.get("wig", "?")]
    bits += [scene[k] for k in ("hoodie", "glasses", "mustache") if scene.get(k)]
    n = len(scene.get("props", []))
    return " + ".join(bits) + (f", {n} prop{'s' if n != 1 else ''}" if n else "")


def save_scene(slot, r, room):
    scenes = load_scenes()
    scenes[str(slot)] = scene_snapshot(r, room)
    SCENES_FILE.write_text(json.dumps(scenes, indent=1) + "\n")


def apply_scene(scene, r, wigs, props_by_id):
    """Put on a saved look. Returns (wig index, room zoom)."""
    by_id = {wg.id: wg for wg in wigs}
    r.set_wig(by_id.get(scene.get("wig"), r.wig))
    r.stache = by_id.get(scene.get("mustache"))
    r.hoodie = by_id.get(scene.get("hoodie"))
    r.glasses = by_id.get(scene.get("glasses"))
    for pid, spot in scene.get("spots", {}).items():
        if pid in props_by_id:
            props_by_id[pid].place(*spot[:3])
            props_by_id[pid].front = bool(spot[3]) if len(spot) > 3 else False
    r.props = sorted((props_by_id[p] for p in scene.get("props", []) if p in props_by_id), key=lambda p: p.key)
    r.species = scene.get("aquarium", r.species)
    r.blur = scene.get("blur", r.blur)
    r.hide_hair = scene.get("hide_hair", r.hide_hair)
    r.relight = scene.get("relight", r.relight)
    r.critters.on = scene.get("critters", r.critters.on)
    save_prop_spots(r.all_props)
    return wigs.index(r.wig), float(scene.get("room", 1.0))


class PropDragger:
    """Click and drag props around the GlamCam window, scroll (or [ ]) to resize; saved when you let go."""

    def __init__(self, renderer, mirrored):
        self.r, self.mirrored = renderer, mirrored
        self.grab = None  # (prop, grab offset x, grab offset y) while dragging
        self.selected = None  # the last prop you grabbed, for [ and ]
        self.size = (1, 1)

    def _hit(self, fx, fy):
        for pr in reversed(self.r.props):  # topmost first
            left, top, pw, ph = self.r.prop_boxes.get(pr.id, (0, 0, 0, 0))
            if left <= fx < left + pw and top <= fy < top + ph:
                return pr
        return None

    def _on_avatar(self, fx, fy):
        box = self.r.avatar_box() if self.r.pinned else None
        return box is not None and box[0] <= fx <= box[2] and box[1] <= fy <= box[3]

    def resize(self, factor, pr=None):
        if pr is None and self.selected == "avatar" and self.r.pinned:
            self.r.move_avatar(scale=factor)
            return
        pr = pr or (self.selected if self.selected in self.r.props else None)
        if pr:
            pr.resize(factor)
            save_prop_spots(self.r.all_props)

    def _frame_xy(self, x, y):
        w, _ = self.size
        return (w - 1 - x if self.mirrored else x), y  # the preview may be a mirror image

    def on_mouse(self, event, x, y, flags, param):
        w, h = self.size
        fx, fy = self._frame_xy(x, y)
        if event == cv2.EVENT_LBUTTONDOWN:
            if self._on_avatar(fx, fy):  # cave mode: grab the frozen costume
                self.grab = ("avatar", fx, fy)
                self.selected = "avatar"
                return
            pr = self._hit(fx, fy)
            if pr:
                self.grab = (pr, fx - pr.x * w, fy - pr.top * h)
                self.selected = pr
        elif event == cv2.EVENT_LBUTTONDBLCLK:
            pr = self._hit(fx, fy)
            if pr:
                pr.front = not pr.front
                save_prop_spots(self.r.all_props)
        elif event in (cv2.EVENT_MOUSEWHEEL, cv2.EVENT_MOUSEHWHEEL) and self._on_avatar(fx, fy):
            self.selected = "avatar"
            self.r.move_avatar(scale=1.06 if cv2.getMouseWheelDelta(flags) > 0 else 1 / 1.06)
        elif event in (cv2.EVENT_MOUSEWHEEL, cv2.EVENT_MOUSEHWHEEL):
            pr = self._hit(fx, fy)
            if pr:
                self.selected = pr
                self.resize(1.06 if cv2.getMouseWheelDelta(flags) > 0 else 1 / 1.06, pr)
        elif event == cv2.EVENT_MOUSEMOVE and self.grab and self.grab[0] == "avatar":
            _, lx, ly = self.grab
            self.r.move_avatar(fx - lx, fy - ly)
            self.grab = ("avatar", fx, fy)
        elif event == cv2.EVENT_MOUSEMOVE and self.grab:
            pr, ox, oy = self.grab
            pr.x = float(np.clip((fx - ox) / w, 0.0, 1.0))
            pr.top = float(np.clip((fy - oy) / h, -0.5, 0.95))
        elif event == cv2.EVENT_LBUTTONUP and self.grab:
            was_prop = self.grab[0] != "avatar"
            self.grab = None
            if was_prop:
                save_prop_spots(self.r.all_props)



def make_wig(photo, name, out_dir=USER_DIR / "my-wigs"):
    """Cut the hair out of a photo and save it as a wig (PNG + anchor JSON)."""
    bgr = cv2.imread(str(photo))
    if bgr is None:
        sys.exit(f"Could not read {photo}")

    with vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path("face_landmarker.task")), num_faces=1)) as lm:
        res = lm.detect(to_mp_image(bgr))
    if not res.face_landmarks:
        sys.exit("No face found in that photo. Use a clear, front-facing shot.")
    h, w = bgr.shape[:2]
    pts = np.array([[p.x * w, p.y * h] for p in res.face_landmarks[0]])

    # normalize so the eyes are ~260px apart; keeps the cleanup kernels sensible at any photo size
    k = 260 / np.linalg.norm(pts[263] - pts[33])
    bgr = cv2.resize(bgr, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC if k > 1 else cv2.INTER_AREA)
    pts *= k

    with vision.ImageSegmenter.create_from_options(vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_path=model_path("hair_segmenter.tflite")),
            output_confidence_masks=True)) as seg:
        res = seg.segment(to_mp_image(bgr))
        m = np.squeeze(res.confidence_masks[-1].numpy_view()).astype(np.float32)

    # keep the main hair blob (drops name labels and specks), soften the edges
    core = (m > 0.5).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(core)
    if n < 2:
        sys.exit("Couldn't find any hair in that photo.")
    big = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
    keep = cv2.dilate((lab == big).astype(np.uint8), np.ones((9, 9), np.uint8))
    alpha = cv2.GaussianBlur(np.clip((m - 0.25) / 0.5, 0, 1) * keep, (3, 3), 0)

    ys, xs = np.where(alpha > 0.02)
    y0, y1 = max(ys.min() - 8, 0), ys.max() + 9
    x0, x1 = max(xs.min() - 8, 0), xs.max() + 9
    rgba = np.dstack([bgr, (alpha * 255).astype(np.uint8)])[y0:y1, x0:x1]
    rgba[rgba[..., 3] == 0] = 0  # blank invisible pixels: PNGs keep their color, i.e. the face

    out_dir.mkdir(exist_ok=True)
    wig_id = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.lower())
    png = out_dir / f"{wig_id}.png"
    cv2.imwrite(str(png), rgba)
    anchors = {str(i): [round(pts[i][0] - x0, 1), round(pts[i][1] - y0, 1)] for i in ANCHOR_IDS}
    meta = {"name": name, "landmarks": anchors, "skin_luma": round(skin_luma(bgr, pts[33], pts[263]), 1)}
    png.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    return png


# ---------------------------------------------------------------- renderer

class GlamRenderer:
    def __init__(self, wig, blur=True, smooth=0.5, relight=False, hide_hair=False):
        self.fits = json.loads(FIT_FILE.read_text()) if FIT_FILE.exists() else {}
        self.set_wig(wig)
        self.stache = None  # optional mustache layered on top of the wig
        self.hoodie = None  # optional hoodie under everything (hides your hair)
        self.glasses = None  # optional shades on top of everything
        self.blur = blur
        self.show_wig = True
        self.relight = relight  # off by default: the photo-lit glow is half the joke
        self.smooth = smooth
        self.face = None  # tracked ANCHOR_IDS points, shared by every layer
        self.mask = None
        self.lost_frames = 0
        self.hide_hair = hide_hair  # paint out your real hair so it can't peek out from under the wig
        self.hair_seg = None  # loaded on first use
        self.hair_mask = None
        self.pinned = False  # cave mode: freeze the costume in place, then lurk in the dark
        self.hide_me = False  # v: remove the human, black background, just the props
        self.lips = 0.0  # how open your mouth is (camera)
        self.mic = MicLevel()  # how loud you are (microphone), started on first Cave Mode
        self.mouth = 0.0
        self.props = []  # neon objects on the wall behind you
        self.critters = Critters()  # c: puppies and kittens running across the bottom
        self.all_props = []  # every prop, on the wall or not (for saving positions)
        self.prop_boxes = {}  # where each prop was last drawn, for click-and-drag
        self.species = "discus"  # who lives in the aquarium prop

        with quiet_native_logs():
            self._load_models()

    def _load_models(self):
        self.landmarker = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path("face_landmarker.task")),
            running_mode=vision.RunningMode.VIDEO, num_faces=1))
        self.segmenter = vision.ImageSegmenter.create_from_options(vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_path=model_path("selfie_segmenter.tflite")),
            running_mode=vision.RunningMode.VIDEO, output_confidence_masks=True))

    def close(self):
        self.mic.stop()
        self.landmarker.close()
        self.segmenter.close()
        if self.hair_seg:
            self.hair_seg.close()

    def set_wig(self, wig):
        self.wig = wig
        self.gain = None
        self.fit = self.fits.setdefault(wig.id, {"dx": 0.0, "dy": 0.0, "scale": 1.0})

    def save_fits(self):
        FIT_FILE.write_text(json.dumps(self.fits, indent=1))

    def move_avatar(self, dx=0.0, dy=0.0, scale=1.0):
        """Cave mode: slide / resize the frozen costume (in frame pixels)."""
        if self.face is None:
            return
        c = self.face.mean(0)
        self.face = (self.face - c) * scale + c + (dx, dy)

    def avatar_box(self):
        """Rough box around the costume (hood, hat and all), for grabbing it with the mouse."""
        if self.face is None:
            return None
        c = self.face.mean(0)
        e = np.linalg.norm(self.face[ANCHOR_IDS.index(263)] - self.face[ANCHOR_IDS.index(33)])
        return c[0] - 1.6 * e, c[1] - 2.0 * e, c[0] + 1.6 * e, c[1] + 2.4 * e

    def nudge(self, dx=0, dy=0, scale=1.0):
        self.fit["dx"] += dx
        self.fit["dy"] += dy
        self.fit["scale"] = float(np.clip(self.fit["scale"] * scale, 0.3, 3.0))

    def _layer_pts(self, wig):
        return self.face[[ANCHOR_IDS.index(i) for i in wig.idx]]

    def _track(self, image, ts, w, h):
        res = self.landmarker.detect_for_video(image, ts)
        if res.face_landmarks:  # how open your real mouth is, for the Cave Mode mouth
            lm = res.face_landmarks[0]
            gap = np.hypot(lm[13].x - lm[14].x, (lm[13].y - lm[14].y) * h / w)
            eyes = np.hypot(lm[33].x - lm[263].x, (lm[33].y - lm[263].y) * h / w)
            self.lips = float(np.clip((gap / max(eyes, 1e-6) - 0.03) / 0.25, 0, 1))
        else:
            self.lips = 0.0
        if self.pinned and self.face is not None:  # cave mode: the costume stays frozen
            return
        if not res.face_landmarks:
            self.lost_frames += 1
            if self.lost_frames > 10:  # hide the wig if the face is gone for ~1/3 s
                self.face = None
            return
        self.lost_frames = 0
        lm = res.face_landmarks[0]
        pts = np.array([[lm[i].x * w, lm[i].y * h] for i in ANCHOR_IDS])
        # exponential smoothing kills jitter; big jumps snap so fast moves don't lag
        if self.face is None or np.abs(pts - self.face).max() > 0.08 * w:
            self.face = pts
        else:
            self.face += (1 - self.smooth) * (pts - self.face)

    def _match_lighting(self, frame):
        """Dim/brighten a photo wig so it's lit like your face instead of like the original photo."""
        if self.pinned:  # keep the lighting it had when pinned; the room may be dark now
            return
        if not self.relight or self.wig.skin_luma is None or self.face is None:
            self.gain = None
            return
        live = skin_luma(frame, self.face[ANCHOR_IDS.index(33)], self.face[ANCHOR_IDS.index(263)])
        if live is None:
            return
        g = float(np.clip(live / max(self.wig.skin_luma, 1.0), 0.3, 1.3))
        self.gain = g if self.gain is None else 0.9 * self.gain + 0.1 * g  # ease in, no flicker

    def _hair(self, image, ts, w):
        """Confidence mask of your real hair, grown a little to catch the wispy edges."""
        if self.hair_seg is None:
            with quiet_native_logs():
                self.hair_seg = vision.ImageSegmenter.create_from_options(vision.ImageSegmenterOptions(
                    base_options=BaseOptions(model_asset_path=model_path("hair_segmenter.tflite")),
                    running_mode=vision.RunningMode.VIDEO, output_confidence_masks=True))
        res = self.hair_seg.segment_for_video(image, ts)
        m = np.squeeze(res.confidence_masks[-1].numpy_view()).astype(np.float32)
        m = np.clip((m - 0.45) / 0.3, 0, 1)  # only confident hair; faces can score ~0.4
        k = max(3, (w // 160) | 1)
        m = cv2.GaussianBlur(cv2.dilate(m, np.ones((k, k), np.uint8)), (0, 0), 2)
        if self.face is not None:  # never paint out the face itself
            m *= 1 - self._face_oval(m.shape)
        self.hair_mask = m if self.hair_mask is None or self.hair_mask.shape != m.shape else 0.5 * self.hair_mask + 0.5 * m
        return self.hair_mask

    def _face_oval(self, shape):
        """Soft mask of the face (brows to chin), from the tracked temple and forehead points."""
        f = {i: self.face[n] for n, i in enumerate(ANCHOR_IDS)}
        left, right, top = f[234], f[454], f[10]
        width = np.linalg.norm(right - left)
        mid = (left + right) / 2
        axis = (right - left) / width
        down = np.array([-axis[1], axis[0]])  # perpendicular to the eye line, pointing at the chin
        if np.dot(down, mid - top) < 0:
            down = -down
        ry = 0.78 * width
        center = mid + down * (0.78 * width - np.linalg.norm(mid - top))
        oval = np.zeros(shape, np.float32)
        cv2.ellipse(oval, (int(center[0]), int(center[1])), (int(0.46 * width), int(ry)),
                    float(np.degrees(np.arctan2(axis[1], axis[0]))), 0, 360, 1.0, -1, cv2.LINE_AA)
        return cv2.GaussianBlur(oval, (0, 0), max(2.0, width / 40))

    def _composite(self, frame, image, ts):
        """Background blur, painting out your own hair, and the props behind you. Blends run in OpenCV."""
        h, w = frame.shape[:2]
        if self.hide_me:  # just the props, on black
            out = np.zeros_like(frame)
            return self._draw_props(out, ts, self.props) if self.props else out
        if not (self.blur or self.hide_hair or self.props):
            return frame
        size = (w // 4, h // 4)
        small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        hair = None
        if self.hide_hair:
            hair = self._hair(image, ts, w)
            hole = (cv2.resize(hair, size) > 0.35).astype(np.uint8) * 255
            small = cv2.inpaint(small, hole, 5, cv2.INPAINT_TELEA)  # refill hair with the colors around it
        out = frame
        if self.blur or self.props:  # where you are, so props can go behind you
            res = self.segmenter.segment_for_video(image, ts)
            m = cv2.GaussianBlur(np.squeeze(res.confidence_masks[0].numpy_view()).astype(np.float32), (0, 0), 3)
            self.mask = m if self.mask is None or self.mask.shape != m.shape else 0.5 * self.mask + 0.5 * m
            self.mask = np.ascontiguousarray(self.mask, dtype=np.float32)
        if self.blur:
            fill = cv2.resize(cv2.GaussianBlur(small, (0, 0), 6), (w, h), interpolation=cv2.INTER_LINEAR)
            out = cv2.blendLinear(out, fill, self.mask, 1 - self.mask)
        else:
            fill = cv2.resize(cv2.GaussianBlur(small, (0, 0), 2), (w, h), interpolation=cv2.INTER_LINEAR)
        if hair is not None:
            hair = np.ascontiguousarray(hair, dtype=np.float32)
            out = cv2.blendLinear(out, fill, 1 - hair, hair)
        if self.props:
            behind = [pr for pr in self.props if not pr.front]
            if behind:  # on the wall: you stay in front of them
                out = cv2.blendLinear(out, self._draw_props(out.copy(), ts, behind), self.mask, 1 - self.mask)
            front = [pr for pr in self.props if pr.front]
            if front:  # in front of you
                out = self._draw_props(out.copy() if out is frame else out, ts, front)
        return out

    @staticmethod
    def _blit(img, sprite, left, top, clip=None, alpha=1.0):
        """Alpha-blend an RGBA sprite onto img at (left, top), optionally clipped to (x0, y0, x1, y1)."""
        h, w = img.shape[:2]
        cx0, cy0, cx1, cy1 = clip or (0, 0, w, h)
        sh, sw = sprite.shape[:2]
        x0, y0 = max(left, cx0, 0), max(top, cy0, 0)
        x1, y1 = min(left + sw, cx1, w), min(top + sh, cy1, h)
        if x0 >= x1 or y0 >= y1:
            return
        crop = sprite[y0 - top:y1 - top, x0 - left:x1 - left].astype(np.float32)
        a = crop[..., 3:] / 255 * alpha
        img[y0:y1, x0:x1] = crop[..., :3] * a + img[y0:y1, x0:x1] * (1 - a)

    def _draw_props(self, img, ts, props):
        h, w = img.shape[:2]
        t = ts / 1000
        for pr in props:
            p = pr.sized(h, pr.variant(self.species) if pr.swim else "default")
            ph, pw = p.shape[:2]
            top = int((pr.top + pr.bob * np.sin(t * 1.7)) * h)
            left = int(pr.x * w - pw / 2)
            self.prop_boxes[pr.id] = (left, top, pw, ph)
            stutter = pr.flicker and (ts // 70 * 2654435761) % 100 < 4  # the occasional neon stutter
            self._blit(img, p, left, top, alpha=0.3 if stutter else 1.0)
            if pr.swim:
                self._swim(img, pr, left, top, pw, ph, t)
        return img

    def _swim(self, img, pr, left, top, pw, ph, t):
        """Fish cruise back and forth inside the tank, turning at the glass; bubbles rise."""
        ax0, ay0, ax1, ay1 = pr.swim["area"]
        area = (left + int(ax0 * pw), top + int(ay0 * ph), left + int(ax1 * pw), top + int(ay1 * ph))
        aw, ah = area[2] - area[0], area[3] - area[1]
        for i, f in enumerate(pr.school(self.species)):
            frames = pr.fish_views(f, img.shape[0])
            views = frames[int(t * f.get("fps", 0) + i) % len(frames)]  # flapping mantas flip through frames
            mode = f.get("mode", "swim")
            pos = (t * f["speed"] * 2 + f["phase"]) % 2  # 0..1 heading one way, 1..2 heading back
            if mode == "crawl":  # plecos: stop, scoot, stop, scoot
                k = pos * 4
                pos = (np.floor(k) + np.clip((k % 1 - 0.5) * 2, 0, 1)) / 4
            u = pos if pos < 1 else 2 - pos
            if mode == "glass":  # suckered onto the side glass, inching up and down
                sprite = views["up"] if pos < 1 else views["down"]
                fh, fw = sprite.shape[:2]
                x, y = area[2] - fw - 4, area[3] - fh - int(u * max(ah - fh, 1))
            else:
                sprite = views["right"] if pos < 1 else views["left"]
                fh, fw = sprite.shape[:2]
                x = area[0] + int(u * max(aw - fw, 1))
                y = area[1] + int(f["y"] * max(ah - fh, 1) + np.sin(t * 1.1 + i * 2.1) * f.get("bob", 0.05) * ah)
            self._blit(img, sprite, x, y, clip=area)
        for i in range(pr.swim.get("bubbles", 0)):  # little rising rings near the kelp and the chest
            bx = area[0] + int(aw * (0.06 + 0.88 * ((i * 0.37) % 1)) + 4 * np.sin(t * 3 + i))
            by = area[3] - int(((t * 0.18 + i * 0.29) % 1) * ah)
            r = max(2, int(ah * (0.008 + 0.006 * (i % 3))))
            cv2.circle(img, (bx, by), r, (255, 250, 210), max(1, r // 3), cv2.LINE_AA)

    def _draw_wig(self, frame, wig=None, gain=None):
        wig = wig or self.wig
        fit = self.fits.setdefault(wig.id, {"dx": 0.0, "dy": 0.0, "scale": 1.0})
        h, w = frame.shape[:2]
        # user fit: scale about the anchor centroid, then offset (in wig-image pixels)
        c = wig.ref.mean(0)
        s = fit["scale"]
        tweak = np.array([[s, 0, (1 - s) * c[0] + fit["dx"]],
                          [0, s, (1 - s) * c[1] + fit["dy"]],
                          [0, 0, 1]])
        m = similarity_transform(wig.ref, self._layer_pts(wig)) @ tweak
        # only warp and blend the rectangle the layer actually covers (big win at 1080p)
        ih, iw = wig.image.shape[:2]
        corners = np.array([[0, 0, 1], [iw, 0, 1], [0, ih, 1], [iw, ih, 1]], np.float64) @ m.T
        x0, y0 = np.maximum(np.floor(corners.min(0)).astype(int), 0)
        x1, y1 = np.minimum(np.ceil(corners.max(0)).astype(int), [w, h])
        if x0 >= x1 or y0 >= y1:
            return frame
        m_roi = m.copy()
        m_roi[:, 2] -= (x0, y0)
        warped = cv2.warpAffine(wig.image, m_roi, (int(x1 - x0), int(y1 - y0)), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        a = warped[..., 3:].astype(np.float32) / 255
        rgb = warped[..., :3].astype(np.float32)
        if gain is not None:
            rgb = np.minimum(rgb * gain, 255)
        roi = frame[y0:y1, x0:x1]
        frame[y0:y1, x0:x1] = (rgb * a + roi * (1 - a)).astype(np.uint8)
        return frame

    def process(self, frame, ts):
        h, w = frame.shape[:2]
        image = to_mp_image(frame)
        self._track(image, ts, w, h)
        self._match_lighting(frame)
        out = self._composite(frame, image, ts)
        if out is frame:
            out = frame.copy()  # layers draw in place; never scribble on the camera frame
        ghost = self.hide_me and self.pinned  # cave mode + hide me: only the avatar and props, never the camera
        if self.show_wig and self.face is not None and (ghost or not self.hide_me):
            drawn = set()
            for layer in (self.hoodie, self.wig, self.stache, self.glasses):  # bottom to top
                if layer is None or layer.id in drawn:
                    continue
                drawn.add(layer.id)
                out = self._draw_wig(out, layer, self.gain if layer is self.wig else None)
        if self.pinned and self.face is not None:
            out = self._cave_eyes(out, frame, ts)
        if self.critters.on or self.critters.runners:  # they run in front of everything
            out = self.critters.draw(out, ts, self._blit)
        return out

    def _cave_eyes(self, out, frame, ts):
        """Cave mode: neon-blue eyes (that blink) and a neon mouth (that talks) fade in as the room goes dark."""
        h, w = frame.shape[:2]
        if self.hide_me:  # ghost mode: it's always dark in here, whatever your actual lights are doing
            dark = 1.0
        else:
            luma = float(cv2.cvtColor(cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA),
                                      cv2.COLOR_BGR2GRAY).mean())
            dark = float(np.clip((70 - luma) / 45, 0, 1))
        if dark <= 0:
            return out
        self.mic.start()
        talk = max(self.mic.level, self.lips)
        self.mouth = talk if talk > self.mouth else 0.6 * self.mouth + 0.4 * talk
        f = {i: self.face[n] for n, i in enumerate(ANCHOR_IDS)}
        e_vec = f[263] - f[33]
        e = float(np.linalg.norm(e_vec))
        across = e_vec / max(e, 1e-6)
        down = np.array([-across[1], across[0]])
        if np.dot(down, f[168] - f[10]) < 0:
            down = -down
        tilt = float(np.degrees(np.arctan2(across[1], across[0])))
        # work in a box around the face only (cheap at 1080p)
        c = self.face.mean(0)
        x0, y0 = int(max(c[0] - 1.5 * e, 0)), int(max(c[1] - 1.2 * e, 0))
        x1, y1 = int(min(c[0] + 1.5 * e, w)), int(min(c[1] + 1.6 * e, h))
        if x0 >= x1 or y0 >= y1:
            return out
        off = np.array([x0, y0])
        glow = np.zeros((y1 - y0, x1 - x0, 3), np.float32)
        core = np.zeros_like(glow)
        pt = lambda v: (int(v[0] - off[0]), int(v[1] - off[1]))
        r = max(3, int(0.055 * e))
        if ts % 4200 >= 150:  # eyes, unless mid-blink
            for eye in ((f[33] + f[133]) / 2, (f[362] + f[263]) / 2):
                cv2.ellipse(glow, pt(eye), (r * 2, r), tilt, 0, 360, (255, 190, 30), -1, cv2.LINE_AA)
                cv2.ellipse(core, pt(eye), (int(r * 1.2), max(2, r // 2)), tilt, 0, 360, (255, 245, 210), -1, cv2.LINE_AA)
        # the mouth: a closed smile line that opens (downward) when you talk
        mw = int(0.3 * e)
        mh = int(2 + self.mouth * 0.2 * e)
        lip = (f[33] + f[263]) / 2 + down * 0.74 * e  # top lip: just under where a mustache sits
        mouth = lip + down * (mh if self.mouth >= 0.08 else 0)  # opens downward, like a jaw
        lw = max(2, int(e / 45))
        if self.mouth < 0.08:
            cv2.ellipse(glow, pt(mouth), (mw, max(3, int(0.06 * e))), tilt, 15, 165, (210, 95, 255), lw * 2, cv2.LINE_AA)
            cv2.ellipse(core, pt(mouth), (mw, max(3, int(0.06 * e))), tilt, 15, 165, (240, 210, 255), lw, cv2.LINE_AA)
        else:
            cv2.ellipse(glow, pt(mouth), (mw, mh), tilt, 0, 360, (210, 95, 255), lw * 2, cv2.LINE_AA)
            cv2.ellipse(core, pt(mouth), (mw, mh), tilt, 0, 360, (240, 210, 255), lw, cv2.LINE_AA)
            cv2.ellipse(glow, pt(mouth + down * mh * 0.35), (int(mw * 0.55), max(2, int(mh * 0.45))), tilt, 0, 360,
                        (60, 40, 255), -1, cv2.LINE_AA)  # a little neon tongue, obviously
        halo = cv2.GaussianBlur(glow, (0, 0), r * 1.6) * 2.2
        roi = out[y0:y1, x0:x1].astype(np.float32)
        out[y0:y1, x0:x1] = np.clip(roi + (halo + glow * 0.8 + core) * dark, 0, 255).astype(np.uint8)
        return out


# ---------------------------------------------------------------- app

def open_capture(args):
    if args.input:
        cap = cv2.VideoCapture(args.input)
    elif sys.platform == "darwin":
        cap = cv2.VideoCapture(args.camera, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            # first run: OpenCV fires the macOS permission pop-up and fails right away.
            # Stay alive so the pop-up doesn't vanish, and retry once Allow is clicked.
            print("Waiting for camera access. If macOS asks, click Allow. (Ctrl+C to give up)")
            deadline = time.monotonic() + 30
            while not cap.isOpened() and time.monotonic() < deadline:
                time.sleep(1)
                cap = cv2.VideoCapture(args.camera, cv2.CAP_AVFOUNDATION)
    elif sys.platform == "win32":
        cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)  # opens much faster than the MSMF default
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # needed for 720p30 on most USB cams
    else:
        cap = cv2.VideoCapture(args.camera)
    if not args.input:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        cap.set(cv2.CAP_PROP_FPS, args.fps)
    if not cap.isOpened():
        if sys.platform == "darwin" and not args.input:
            sys.exit(f"Could not open camera {args.camera}.\n"
                     "  - Run GlamCam from Terminal or iTerm2 on the Mac itself. SSH sessions and SSH apps\n"
                     "    (Termius etc.) can't get camera permission and never show the pop-up.\n"
                     "  - Your terminal app must be ON in System Settings > Privacy & Security > Camera.\n"
                     "    If you just turned it on, quit the terminal app (Cmd+Q), reopen it, and try again.\n"
                     f"  - Already allowed? Try --camera 1 (or 2); camera {args.camera} may be your iPhone or busy elsewhere.")
        hint = "Settings > Privacy & security > Camera"
        sys.exit(f"Could not open camera {args.camera}. Try --camera 1 (or 2), close other apps "
                 f"using the camera, and check camera access in {hint}.")
    return cap


def open_virtual_camera(w, h, fps):
    import pyvirtualcam
    try:
        cam = pyvirtualcam.Camera(width=w, height=h, fps=fps, fmt=pyvirtualcam.PixelFormat.BGR)
    except RuntimeError as e:
        if sys.platform == "darwin":
            steps = ("  1. Put OBS in /Applications, open it, click 'Start Virtual Camera'\n"
                     "  2. Allow the blocked extension: System Settings > General > Login Items & Extensions >\n"
                     "     Camera Extensions > OBS (macOS 13-14: Privacy & Security > Allow)\n"
                     "  3. Click 'Stop Virtual Camera', quit OBS (restart the Mac if it's still not found)\n"
                     "  Check with: systemextensionsctl list | grep -i obs   (want: activated enabled)")
        else:
            steps = "  Install OBS Studio, click 'Start Virtual Camera' once, then stop it and close OBS."
        sys.exit(f"Virtual camera unavailable: {e}\n\nOne-time OBS setup:\n{steps}\n\n"
                 "OBS itself must not be running its virtual camera while GlamCam is.\n"
                 "Just want to see the wig? ./run.sh --no-vcam")
    print(f"Broadcasting {w}x{h} to: {cam.device}")
    return cam


def make_room(frame, zoom):
    """Room mode: shrink the camera picture toward the bottom-center and fill the new space around it with a
    soft, blurred stretch of your room, so there's more wall for props. zoom 1.0 = off."""
    if zoom <= 1.0:
        return frame
    h, w = frame.shape[:2]
    sw, sh = int(w / zoom), int(h / zoom)
    small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
    wall = cv2.resize(cv2.GaussianBlur(cv2.resize(frame, (w // 8, h // 8), interpolation=cv2.INTER_AREA), (0, 0), 3),
                      (w, h), interpolation=cv2.INTER_LINEAR)
    x0, y0 = (w - sw) // 2, h - sh  # sit on the bottom edge, like you stepped back from the camera
    feather = np.zeros((h, w), np.float32)
    pad = max(4, sw // 30)
    feather[y0 + pad:, x0 + pad:x0 + sw - pad] = 1.0
    feather = np.ascontiguousarray(cv2.GaussianBlur(feather, (0, 0), pad / 2))
    placed = wall.copy()
    placed[y0:, x0:x0 + sw] = small
    return cv2.blendLinear(placed, wall, feather, 1 - feather)


def draw_hud(img, text, y=34):
    cv2.putText(img, text, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (210, 95, 255), 2, cv2.LINE_AA)


HELP = [("w a s d", "move the wig"), ("- / =", "shrink / grow"), ("n", "next wig"), ("m", "mustache on any wig"),
        ("o", "hoodie mode"), ("g", "sunglasses"), ("x", "hide my real hair"), ("f", "cave mode"),
        ("v", "hide me (+ f: ghost)"), ("z", "room mode (zoom out)"), ("b", "background blur"),
        ("1 - 5", "neon props (0 = none)"), ("mouse", "drag / scroll props"), ("dbl-click", "prop in front of you"),
        ("r", "reset props"), ("t", "aquarium fish"), ("c / j", "critters / send one"),
        ("u / i 1-5", "load / save scene"), ("h", "wig on / off"), ("l", "lighting match"), ("p", "save the fit"),
        ("k  cmd+K", "this window"), ("q", "quit")]
KEYS_WINDOW = "GlamCam Keys"


class KeysPanel:
    """The floating 'GlamCam Keys' window: every shortcut, plus a clickable scene loader.
    It's its own window, so nothing in it ever shows up on camera or in OBS."""

    W, ROW, TOP, COLS = 820, 30, 92, 2

    def __init__(self):
        self.open = False
        self.state = {}  # for window_closed()
        self.status = "Click Load or Save, or press u / i then 1-5."
        self.pending = None  # "load" / "save" while waiting for 1-5 from the keyboard
        self.clicked = None  # ("load" | "save", slot) for the main loop to act on
        self.buttons = []  # (x0, y0, x1, y1, action, slot)
        self.dirty = True

    def toggle(self):
        if self.open:
            cv2.destroyWindow(KEYS_WINDOW)
            self.open, self.state = False, {}
            return
        cv2.namedWindow(KEYS_WINDOW)
        cv2.setMouseCallback(KEYS_WINDOW, self.on_mouse)
        self.open, self.dirty = True, True
        self.show()
        try:
            cv2.setWindowProperty(KEYS_WINDOW, cv2.WND_PROP_TOPMOST, 1)  # float above everything
        except cv2.error:
            pass

    def say(self, text):
        print(text)
        self.status, self.dirty = text, True

    def on_mouse(self, event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        for x0, y0, x1, y1, action, slot in self.buttons:
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.clicked = (action, slot)

    def show(self):
        if not self.open:
            return
        if self.open and window_closed(self.state, KEYS_WINDOW):
            self.open, self.state = False, {}  # closed with its own close button
            return
        if not self.dirty:
            return
        self.dirty = False
        scenes = load_scenes()
        pink, yellow, text, dim = (210, 95, 255), (0, 240, 255), (245, 230, 255), (150, 120, 170)
        per_col = -(-len(HELP) // self.COLS)
        keys_h = self.TOP + self.ROW * (per_col - 1)
        img = np.full((keys_h + 324, self.W, 3), (40, 16, 27), np.uint8)
        cv2.rectangle(img, (6, 6), (self.W - 7, img.shape[0] - 7), pink, 2, cv2.LINE_AA)
        cv2.putText(img, "GlamCam keys", (24, 56), cv2.FONT_HERSHEY_DUPLEX, 1.05, pink, 2, cv2.LINE_AA)
        for i, (key, what) in enumerate(HELP):
            col, r = divmod(i, per_col)
            x, y = 24 + col * (self.W // self.COLS), self.TOP + self.ROW * r
            cv2.putText(img, key, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (0, 240, 255)[::-1], 2, cv2.LINE_AA)
            cv2.putText(img, what, (x + 132, y), cv2.FONT_HERSHEY_SIMPLEX, 0.56, text, 1, cv2.LINE_AA)
        # scene loader
        y = keys_h + 18
        cv2.line(img, (20, y), (self.W - 20, y), pink, 1, cv2.LINE_AA)
        cv2.putText(img, "Scenes", (24, y + 42), cv2.FONT_HERSHEY_DUPLEX, 0.9, pink, 2, cv2.LINE_AA)
        self.buttons = []
        for i in range(5):
            slot = str(i + 1)
            row = y + 66 + 40 * i
            scene = scenes.get(slot)
            label = describe_scene(scene) if scene else "(empty)"
            cv2.putText(img, slot, (28, row + 22), cv2.FONT_HERSHEY_DUPLEX, 0.75, yellow[::-1], 2, cv2.LINE_AA)
            cv2.putText(img, label[:50], (58, row + 21), cv2.FONT_HERSHEY_SIMPLEX, 0.52, text if scene else dim, 1,
                        cv2.LINE_AA)
            for bx, action, color in ((self.W - 190, "load", (255, 240, 0)), (self.W - 100, "save", pink)):
                live = action == "save" or scene
                cv2.rectangle(img, (bx, row), (bx + 80, row + 30), color if live else dim, 2, cv2.LINE_AA)
                cv2.putText(img, action.title(), (bx + 14, row + 21), cv2.FONT_HERSHEY_SIMPLEX, 0.58,
                            color if live else dim, 1 if not live else 2, cv2.LINE_AA)
                self.buttons.append((bx, row, bx + 80, row + 30, action, slot))
        msg = (f"{'Save' if self.pending == 'save' else 'Load'} scene: press 1-5" if self.pending else self.status)
        cv2.putText(img, msg[:80], (24, img.shape[0] - 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 240, 255)[::-1] if self.pending else text, 1, cv2.LINE_AA)
        cv2.imshow(KEYS_WINDOW, img)


def window_closed(state, name=WINDOW):
    """True once the user clicks the window's close button. Some backends can't report
    visibility (they return -1), so only trust it after it has reported visible once."""
    try:
        visible = cv2.getWindowProperty(name, cv2.WND_PROP_VISIBLE)
    except cv2.error:
        visible = -1
    if visible >= 1:
        state["seen"] = True
    return state.get("seen", False) and visible < 1


def show_message(text):
    """In the packaged app there is no terminal, so errors and results get a window of their own."""
    import textwrap
    lines = []
    for para in str(text).splitlines() or [""]:
        lines += textwrap.wrap(para, 78) or [""]
    img = np.full((80 + 30 * len(lines), 1000, 3), (40, 16, 27), np.uint8)
    for i, ln in enumerate(lines):
        cv2.putText(img, ln, (24, 44 + 30 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (245, 230, 255), 1, cv2.LINE_AA)
    cv2.putText(img, "press any key to close", (24, img.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (210, 95, 255), 1, cv2.LINE_AA)
    state = {}
    while True:
        cv2.imshow(WINDOW, img)
        if cv2.waitKey(100) != -1 or window_closed(state):
            break
    cv2.destroyAllWindows()


def run(args):
    wigs = load_wigs()
    if not wigs:
        sys.exit("No wigs found in wigs/ or my-wigs/.")
    current = next((i for i, wg in enumerate(wigs) if wg.id == args.wig), None)
    if current is None:
        sys.exit(f"No wig called '{args.wig}'. Try one of: {', '.join(wg.id for wg in wigs)}")

    cap = open_capture(args)
    ok, frame = cap.read()
    if not ok:
        sys.exit("Camera opened but returned no frames.")
    w, h = args.width, args.height  # always broadcast at the size you asked for, even if the camera can't
    cam_h, cam_w = frame.shape[:2]
    if (cam_w, cam_h) != (w, h):
        print(f"Camera gives {cam_w}x{cam_h}; scaling to {w}x{h}.")
    room = args.room if args.room in (1.0, 1.25, 1.5) else max(1.0, args.room)

    renderer = GlamRenderer(wigs[current], blur=not args.no_blur, smooth=args.smooth, relight=args.match_lighting,
                            hide_hair=args.hide_hair)
    cam = None if args.no_vcam or args.obs_window else open_virtual_camera(w, h, args.fps)

    print("Keys" + __doc__.split("Keys", 1)[1])
    print(f"Window: '{WINDOW}'" + ("  (clean output for OBS Window Capture)" if args.obs_window else ""))
    keys = {ord("w"): (0, -4, 1), ord("s"): (0, 4, 1), ord("a"): (-4, 0, 1), ord("d"): (4, 0, 1),
            ord("-"): (0, 0, 1 / 1.03), ord("="): (0, 0, 1.03), ord("+"): (0, 0, 1.03)}
    t0, last_ts = time.monotonic(), -1
    hud_until = 0 if args.obs_window else time.monotonic() + 4
    win_state = {}
    panel = KeysPanel()
    scene_prompt_until = 0

    def do_scene(action, slot):
        nonlocal current, room
        if action == "save":
            save_scene(slot, renderer, room)
            panel.say(f"Saved scene {slot}")
        else:
            scene = load_scenes().get(slot)
            if scene:
                current, room = apply_scene(scene, renderer, wigs, {p.id: p for p in renderer.all_props})
                panel.say(f"Loaded scene {slot}")
            else:
                panel.say(f"Scene {slot} is empty: click Save to fill it")
        panel.pending, panel.dirty = None, True
    staches = [None] + [wg for wg in wigs if wg.kind == "mustache"]
    hoodies = [None] + [wg for wg in wigs if wg.kind == "hoodie"]
    shades = [None] + [wg for wg in wigs if wg.kind == "glasses"]
    renderer.hide_me = args.hide_me
    if args.hoodie:
        renderer.hoodie = hoodies[1] if len(hoodies) > 1 else None
    if args.glasses:
        renderer.glasses = next((wg for wg in shades[1:] if wg.id == args.glasses), None)
    props = {pr.key: pr for pr in load_props()}
    renderer.all_props = list(props.values())
    dragger = PropDragger(renderer, mirrored=not args.obs_window)
    cv2.namedWindow(WINDOW)
    cv2.setMouseCallback(WINDOW, dragger.on_mouse)
    tank = next((pr for pr in props.values() if pr.swim), None)
    species = tank.swim["order"] if tank else ["discus"]
    if args.aquarium == "all":  # older name for party mode
        args.aquarium = "party"
    if args.aquarium in species:
        renderer.species = args.aquarium
    for pid in filter(None, (args.props or "").split(",")):
        pr = next((p for p in props.values() if p.id == pid.strip()), None)
        if pr:
            renderer.props.append(pr)
    if args.mustache:
        renderer.stache = next((wg for wg in staches[1:] if wg.id == args.mustache), None)
    renderer.critters.on = args.critters
    if args.scene:
        scene = load_scenes().get(str(args.scene))
        if scene:
            current, room = apply_scene(scene, renderer, wigs, {p.id: p for p in renderer.all_props})
        else:
            print(f"Scene {args.scene} is empty. Set things up, then press i and {args.scene} to save it.")
    try:
        while ok:
            ts = max(int((time.monotonic() - t0) * 1000), last_ts + 1)  # must strictly increase
            last_ts = ts
            if frame.shape[1] != w or frame.shape[0] != h:
                frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)
            out = renderer.process(make_room(frame, room), ts)
            if cam:
                cam.send(out)
            # OBS mode: clean, un-mirrored frames. Otherwise a mirrored preview, like a selfie cam.
            view = out.copy() if args.obs_window else cv2.flip(out, 1)
            if time.monotonic() < hud_until:
                draw_hud(view, f"{renderer.wig.name}  (n = next wig, k = keys)")
            if panel.pending and time.monotonic() > scene_prompt_until:
                panel.pending, panel.dirty = None, True
            if panel.clicked:  # a Load / Save button in the keys window
                do_scene(*panel.clicked)
                panel.clicked = None
            panel.show()  # scene messages live in the keys window, never on camera
            dragger.size = (view.shape[1], view.shape[0])
            cv2.imshow(WINDOW, view)

            k = cv2.waitKey(1) & 0xFF
            if k in (ord("q"), 27) or window_closed(win_state):
                break
            if k in (ord("k"), ord("K"), 11, ord("?"), ord("/")):  # k, cmd+K (arrives as k), ctrl+K (11)
                panel.toggle()
            elif k in keys:
                dx, dy, sc = keys[k]
                renderer.nudge(dx if args.obs_window else -dx, dy, sc)  # mirrored preview flips left/right
            elif k == ord("n"):
                current = (current + 1) % len(wigs)
                renderer.set_wig(wigs[current])
                if not args.obs_window:
                    hud_until = time.monotonic() + 2
            elif panel.pending and ord("1") <= k <= ord("5"):
                do_scene(panel.pending, chr(k))
            elif k == ord("0"):
                renderer.props = []
            elif k == ord("r"):  # every prop back to its default spot and size
                for pr in renderer.all_props:
                    pr.place(*pr.default_spot)
                    pr.front = False
                save_prop_spots(renderer.all_props)
            elif k in (ord("["), ord("]")):
                dragger.resize(1.08 if k == ord("]") else 1 / 1.08)
            elif chr(k) in props:
                pr = props[chr(k)]
                renderer.props = [p for p in renderer.props if p is not pr] if pr in renderer.props \
                    else sorted(renderer.props + [pr], key=lambda p: p.key)
            elif k == ord("t"):  # who's in the tank
                renderer.species = species[(species.index(renderer.species) + 1) % len(species)]
                print(f"Aquarium: {tank.swim['names'].get(renderer.species, renderer.species) if tank else renderer.species}")
                if tank and tank not in renderer.props:
                    renderer.props = sorted(renderer.props + [tank], key=lambda p: p.key)
            elif k == ord("m"):
                renderer.stache = staches[(staches.index(renderer.stache) + 1) % len(staches)]
            elif k == ord("o"):
                renderer.hoodie = hoodies[(hoodies.index(renderer.hoodie) + 1) % len(hoodies)]
            elif k == ord("g"):
                renderer.glasses = shades[(shades.index(renderer.glasses) + 1) % len(shades)]
            elif k == ord("b"):
                renderer.blur = not renderer.blur
            elif k in (ord("u"), ord("i"), ord("U")):  # scenes: u then 1-5 loads, i (or shift+u) then 1-5 saves
                panel.pending, panel.dirty = ("load" if k == ord("u") else "save"), True
                scene_prompt_until = time.monotonic() + 3
                if not panel.open:
                    print(f"{panel.pending.title()} scene: press 1-5")
            elif k == ord("c"):
                renderer.critters.on = not renderer.critters.on
                renderer.critters.next_at = None
            elif k in (ord("j"), ord("C")):  # send a critter across right now
                if not renderer.critters.frames:
                    print("No critter sprites found in props/critters/")
                renderer.critters.send(ts)
            elif k == ord("v"):
                renderer.hide_me = not renderer.hide_me
            elif k == ord("z"):  # room mode: 1x -> 1.25x -> 1.5x -> 1x
                room = {1.0: 1.25, 1.25: 1.5}.get(room, 1.0)
            elif k == ord("x"):
                renderer.hide_hair = not renderer.hide_hair
            elif k == ord("f"):
                renderer.pinned = not renderer.pinned
                print("Cave mode ON: costume frozen. Kill the lights." if renderer.pinned
                      else "Cave mode off: tracking your face again.")
            elif k == ord("l"):
                renderer.relight = not renderer.relight
            elif k == ord("h"):
                renderer.show_wig = not renderer.show_wig
            elif k == ord("p"):
                renderer.save_fits()
                print(f"Saved fit for {renderer.wig.name}")
                if not args.obs_window:
                    hud_until = time.monotonic() + 1
            ok, frame = cap.read()
            if not ok and args.input:  # loop test videos
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
    finally:
        cap.release()
        if cam:
            cam.close()
        renderer.close()
        cv2.destroyAllWindows()


def self_test():
    """Load everything and push a few frames through; used by the build to check the packaged app."""
    wigs = load_wigs()
    if not wigs:
        raise RuntimeError("no wigs bundled")
    r = GlamRenderer(wigs[0], relight=True, hide_hair=True)
    r.stache = next((wg for wg in wigs if wg.kind == "mustache"), None)
    r.hoodie = next((wg for wg in wigs if wg.kind == "hoodie"), None)
    r.glasses = next((wg for wg in wigs if wg.kind == "glasses"), None)
    r.species = "party"
    r.critters.on = True
    r.critters.send(1)
    if len(r.critters.frames) != len(Critters.KINDS):
        raise RuntimeError("critter sprites missing")
    r.props = load_props()
    frame = np.full((720, 1280, 3), 128, np.uint8)
    for k in range(3):
        r.process(frame, k * 33 + 1)
    r.close()
    print(f"self-test ok: {len(wigs)} wigs, {len(r.props)} props, {len(r.critters.frames)} critters, models from {MODEL_DIR}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wig", default="glaaron", help="wig to start with (default: glaaron)")
    ap.add_argument("--list-wigs", action="store_true", help="show available wigs and exit")
    ap.add_argument("--make-wig", metavar="PHOTO", help="cut the hair out of a photo and save it as a new wig")
    ap.add_argument("--name", help="name for --make-wig (default: the photo's file name)")
    ap.add_argument("--download-models", action="store_true", help="fetch the AI models and exit")
    ap.add_argument("--camera", type=int, default=0, help="camera index (default 0)")
    ap.add_argument("--input", help="video file to use instead of a camera (for testing)")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--no-blur", action="store_true", help="start with background blur off")
    ap.add_argument("--match-lighting", action="store_true", help="dim/brighten photo wigs to match your room")
    ap.add_argument("--hide-hair", action="store_true", help="start with your real hair painted out")
    ap.add_argument("--mustache", metavar="NAME", help="start with this mustache layered on (mustache, neon-mustache)")
    ap.add_argument("--scene", type=int, choices=range(1, 6), help="start with saved scene 1-5")
    ap.add_argument("--critters", action="store_true", help="start with puppies and kittens running around")
    ap.add_argument("--room", type=float, default=1.0, help="room mode: zoom out for more wall (1.25, 1.5)")
    ap.add_argument("--hide-me", action="store_true", help="start hidden: just the props, on black")
    ap.add_argument("--hoodie", action="store_true", help="start in hoodie mode")
    ap.add_argument("--glasses", metavar="NAME", help="start with sunglasses on: shades, neon-shades")
    ap.add_argument("--props", metavar="IDS", help="neon props to start with, comma-separated (see props/)")
    ap.add_argument("--aquarium", metavar="FISH", help="who lives in the aquarium: discus, angelfish, stingray, pleco, freshwater, "
                                                        "manta, clownfish, lionfish, saltwater, party")
    ap.add_argument("--smooth", type=float, default=0.5, help="0 = raw tracking, 0.9 = very smooth/laggy")
    ap.add_argument("--no-vcam", action="store_true", help="preview only, don't send to the virtual camera")
    ap.add_argument("--obs-window", action="store_true",
                    help="un-mirrored window with no overlay text, for OBS window capture (implies --no-vcam)"
                         " [default for the packaged app]")
    ap.add_argument("--vcam", action="store_true",
                    help="packaged app: send straight to the OBS Virtual Camera instead of the OBS window")
    ap.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    # macOS passes -psn_* when an app is double-clicked on some versions; ignore unknown args
    args, _ = ap.parse_known_args()
    if FROZEN and not args.vcam:
        args.obs_window = True

    if args.self_test:
        try:
            self_test()
        except Exception as e:  # no dialogs: this runs unattended in CI
            print(f"self-test FAILED: {e!r}")
            sys.exit(1)
    elif args.download_models:
        download_models()
        print("Models ready.")
    elif args.list_wigs:
        for wg in load_wigs():
            print(f"  {wg.id:<20} {wg.name}")
    elif args.make_wig:
        with quiet_native_logs():
            png = make_wig(args.make_wig, args.name or Path(args.make_wig).stem)
        msg = f"New wig saved: {png}\nTry it on:  --wig {png.stem}"
        print(msg)
        if FROZEN:
            show_message(msg)
    else:
        try:
            run(args)
        except SystemExit as e:
            if FROZEN and isinstance(e.code, str):  # no terminal to read it in: show it in the window
                show_message(e.code)
            raise


if __name__ == "__main__":
    main()
