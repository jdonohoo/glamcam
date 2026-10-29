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
FIT_FILE = USER_DIR / "fit.json"
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

    def nudge(self, dx=0, dy=0, scale=1.0):
        self.fit["dx"] += dx
        self.fit["dy"] += dy
        self.fit["scale"] = float(np.clip(self.fit["scale"] * scale, 0.3, 3.0))

    def _layer_pts(self, wig):
        return self.face[[ANCHOR_IDS.index(i) for i in wig.idx]]

    def _track(self, image, ts, w, h):
        if self.pinned and self.face is not None:
            return
        res = self.landmarker.detect_for_video(image, ts)
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
        """Background blur and/or painting out your own hair, so only the wig's hair shows."""
        if not (self.blur or self.hide_hair):
            return frame
        h, w = frame.shape[:2]
        size = (w // 4, h // 4)
        small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        hair = None
        if self.hide_hair:
            hair = self._hair(image, ts, w)
            hole = (cv2.resize(hair, size) > 0.35).astype(np.uint8) * 255
            small = cv2.inpaint(small, hole, 5, cv2.INPAINT_TELEA)  # refill hair with the colors around it
        out = frame.astype(np.float32)
        if self.blur:
            res = self.segmenter.segment_for_video(image, ts)
            m = cv2.GaussianBlur(np.squeeze(res.confidence_masks[0].numpy_view()).astype(np.float32), (0, 0), 3)
            self.mask = m if self.mask is None or self.mask.shape != m.shape else 0.5 * self.mask + 0.5 * m
            fill = cv2.resize(cv2.GaussianBlur(small, (0, 0), 6), (w, h), interpolation=cv2.INTER_LINEAR)
            a = self.mask[..., None]
            out = out * a + fill * (1 - a)
        else:
            fill = cv2.resize(cv2.GaussianBlur(small, (0, 0), 2), (w, h), interpolation=cv2.INTER_LINEAR)
        if hair is not None:
            a = hair[..., None]
            out = out * (1 - a) + fill * a
        return out.astype(np.uint8)

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
        warped = cv2.warpAffine(wig.image, m, (w, h), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        a = warped[..., 3:].astype(np.float32) / 255
        rgb = warped[..., :3].astype(np.float32)
        if gain is not None:
            rgb = np.minimum(rgb * gain, 255)
        return (rgb * a + frame * (1 - a)).astype(np.uint8)

    def process(self, frame, ts):
        h, w = frame.shape[:2]
        image = to_mp_image(frame)
        self._track(image, ts, w, h)
        self._match_lighting(frame)
        out = self._composite(frame, image, ts)
        if self.show_wig and self.face is not None:
            out = self._draw_wig(out, self.wig, self.gain)
            if self.stache is not None and self.stache is not self.wig:
                out = self._draw_wig(out, self.stache)
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


def draw_hud(img, text, y=34):
    cv2.putText(img, text, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (210, 95, 255), 2, cv2.LINE_AA)


HELP = [("w a s d", "move the wig"), ("- / =", "shrink / grow"), ("n", "next wig"), ("m", "mustache on any wig"),
        ("x", "hide my real hair"), ("f", "cave mode (freeze it)"), ("b", "background blur"),
        ("h", "wig on / off"), ("l", "lighting match"), ("p", "save the fit"), ("k  cmd+K", "this window"),
        ("q", "quit")]
KEYS_WINDOW = "GlamCam Keys"


def keys_card():
    """The keyboard shortcuts as a little neon card, for its own floating window."""
    img = np.full((96 + 38 * len(HELP), 470, 3), (40, 16, 27), np.uint8)
    cv2.rectangle(img, (6, 6), (img.shape[1] - 7, img.shape[0] - 7), (210, 95, 255), 2, cv2.LINE_AA)
    cv2.putText(img, "GlamCam keys", (26, 56), cv2.FONT_HERSHEY_DUPLEX, 1.1, (210, 95, 255), 2, cv2.LINE_AA)
    for i, (key, what) in enumerate(HELP):
        y = 104 + 38 * i
        cv2.putText(img, key, (26, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 240, 0), 2, cv2.LINE_AA)
        cv2.putText(img, what, (170, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (245, 230, 255), 1, cv2.LINE_AA)
    return img


def toggle_keys_window(state):
    """Open or close the floating shortcuts window. It's a separate window, so OBS never captures it."""
    if state.get("open"):
        cv2.destroyWindow(KEYS_WINDOW)
        state.clear()
        return
    cv2.imshow(KEYS_WINDOW, keys_card())
    try:
        cv2.setWindowProperty(KEYS_WINDOW, cv2.WND_PROP_TOPMOST, 1)  # float above everything
    except cv2.error:
        pass
    state["open"] = True


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
    h, w = frame.shape[:2]

    renderer = GlamRenderer(wigs[current], blur=not args.no_blur, smooth=args.smooth, relight=args.match_lighting,
                            hide_hair=args.hide_hair)
    cam = None if args.no_vcam or args.obs_window else open_virtual_camera(w, h, args.fps)

    print("Keys" + __doc__.split("Keys", 1)[1])
    print(f"Window: '{WINDOW}'" + ("  (clean output for OBS Window Capture)" if args.obs_window else ""))
    keys = {ord("w"): (0, -4, 1), ord("s"): (0, 4, 1), ord("a"): (-4, 0, 1), ord("d"): (4, 0, 1),
            ord("-"): (0, 0, 1 / 1.03), ord("="): (0, 0, 1.03), ord("+"): (0, 0, 1.03)}
    t0, last_ts = time.monotonic(), -1
    hud_until = 0 if args.obs_window else time.monotonic() + 4
    win_state, keys_state = {}, {}
    staches = [None] + [wg for wg in wigs if wg.kind == "mustache"]
    if args.mustache:
        renderer.stache = next((wg for wg in staches[1:] if wg.id == args.mustache), None)
    try:
        while ok:
            ts = max(int((time.monotonic() - t0) * 1000), last_ts + 1)  # must strictly increase
            last_ts = ts
            out = renderer.process(frame, ts)
            if cam:
                cam.send(out)
            # OBS mode: clean, un-mirrored frames. Otherwise a mirrored preview, like a selfie cam.
            view = out.copy() if args.obs_window else cv2.flip(out, 1)
            if time.monotonic() < hud_until:
                draw_hud(view, f"{renderer.wig.name}  (n = next wig, k = keys)")
            cv2.imshow(WINDOW, view)

            k = cv2.waitKey(1) & 0xFF
            if k in (ord("q"), 27) or window_closed(win_state):
                break
            if keys_state.get("open") and window_closed(keys_state, KEYS_WINDOW):
                keys_state.clear()  # closed with its own close button
            if k in (ord("k"), ord("K"), 11, ord("?"), ord("/")):  # k, cmd+K (arrives as k), ctrl+K (11)
                toggle_keys_window(keys_state)
            elif k in keys:
                dx, dy, sc = keys[k]
                renderer.nudge(dx if args.obs_window else -dx, dy, sc)  # mirrored preview flips left/right
            elif k == ord("n"):
                current = (current + 1) % len(wigs)
                renderer.set_wig(wigs[current])
                if not args.obs_window:
                    hud_until = time.monotonic() + 2
            elif k == ord("m"):
                renderer.stache = staches[(staches.index(renderer.stache) + 1) % len(staches)]
            elif k == ord("b"):
                renderer.blur = not renderer.blur
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
    frame = np.full((720, 1280, 3), 128, np.uint8)
    for k in range(3):
        r.process(frame, k * 33 + 1)
    r.close()
    print(f"self-test ok: {len(wigs)} wigs, models from {MODEL_DIR}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wig", default="glaaron", help="wig to start with (default: glaaron)")
    ap.add_argument("--list-wigs", action="store_true", help="show available wigs and exit")
    ap.add_argument("--make-wig", metavar="PHOTO", help="cut the hair out of a photo and save it as a new wig")
    ap.add_argument("--name", help="name for --make-wig (default: the photo's file name)")
    ap.add_argument("--download-models", action="store_true", help="fetch the AI models and exit")
    ap.add_argument("--camera", type=int, default=0, help="camera index (default 0)")
    ap.add_argument("--input", help="video file to use instead of a camera (for testing)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--no-blur", action="store_true", help="start with background blur off")
    ap.add_argument("--match-lighting", action="store_true", help="dim/brighten photo wigs to match your room")
    ap.add_argument("--hide-hair", action="store_true", help="start with your real hair painted out")
    ap.add_argument("--mustache", metavar="NAME", help="start with this mustache layered on (mustache, neon-mustache)")
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
