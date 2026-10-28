#!/usr/bin/env python3
"""GlamCam: a face-tracking wig for your webcam.

Tracks your face with MediaPipe, glues a transparent hair PNG to it (follows
position, size and head tilt), blurs your background, and sends the result to
the OBS Virtual Camera so Google Meet / Zoom / Teams can use it.

Keys (click the preview window first):
  w/a/s/d  nudge the wig up/left/down/right     - / =  shrink / grow
  n        next wig                              h      wig on/off
  b        background blur on/off                p      save the wig fit
  q/Esc    quit
"""
import argparse
import json
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
MODEL_DIR = HERE / "models"
WIG_DIRS = [HERE / "wigs", HERE / "my-wigs"]
FIT_FILE = HERE / "fit.json"

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
        self.image = cv2.imread(str(png_path), cv2.IMREAD_UNCHANGED)
        if self.image is None or self.image.ndim != 3 or self.image.shape[2] != 4:
            raise ValueError(f"{png_path} is not a transparent PNG")
        self.idx = [int(k) for k in meta["landmarks"]]
        self.ref = np.array(list(meta["landmarks"].values()), dtype=np.float64)


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
    return wigs


def make_wig(photo, name, out_dir=HERE / "my-wigs"):
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
    png.with_suffix(".json").write_text(json.dumps({"name": name, "landmarks": anchors}, indent=1))
    return png


# ---------------------------------------------------------------- renderer

class GlamRenderer:
    def __init__(self, wig, blur=True, smooth=0.5):
        self.fits = json.loads(FIT_FILE.read_text()) if FIT_FILE.exists() else {}
        self.set_wig(wig)
        self.blur = blur
        self.show_wig = True
        self.smooth = smooth
        self.pts = None
        self.mask = None
        self.lost_frames = 0

        self.landmarker = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path("face_landmarker.task")),
            running_mode=vision.RunningMode.VIDEO, num_faces=1))
        self.segmenter = vision.ImageSegmenter.create_from_options(vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_path=model_path("selfie_segmenter.tflite")),
            running_mode=vision.RunningMode.VIDEO, output_confidence_masks=True))

    def close(self):
        self.landmarker.close()
        self.segmenter.close()

    def set_wig(self, wig):
        self.wig = wig
        self.pts = None  # anchor sets differ between wigs
        self.fit = self.fits.setdefault(wig.id, {"dx": 0.0, "dy": 0.0, "scale": 1.0})

    def save_fits(self):
        FIT_FILE.write_text(json.dumps(self.fits, indent=1))

    def nudge(self, dx=0, dy=0, scale=1.0):
        self.fit["dx"] += dx
        self.fit["dy"] += dy
        self.fit["scale"] = float(np.clip(self.fit["scale"] * scale, 0.3, 3.0))

    def _track(self, image, ts, w, h):
        res = self.landmarker.detect_for_video(image, ts)
        if not res.face_landmarks:
            self.lost_frames += 1
            if self.lost_frames > 10:  # hide the wig if the face is gone for ~1/3 s
                self.pts = None
            return
        self.lost_frames = 0
        lm = res.face_landmarks[0]
        pts = np.array([[lm[i].x * w, lm[i].y * h] for i in self.wig.idx])
        # exponential smoothing kills jitter; big jumps snap so fast moves don't lag
        if self.pts is None or np.abs(pts - self.pts).max() > 0.08 * w:
            self.pts = pts
        else:
            self.pts += (1 - self.smooth) * (pts - self.pts)

    def _blur_background(self, frame, image, ts):
        res = self.segmenter.segment_for_video(image, ts)
        m = np.squeeze(res.confidence_masks[0].numpy_view()).astype(np.float32)
        m = cv2.GaussianBlur(m, (0, 0), 3)
        self.mask = m if self.mask is None or self.mask.shape != m.shape else 0.5 * self.mask + 0.5 * m
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (w // 4, h // 4), interpolation=cv2.INTER_AREA)
        bg = cv2.resize(cv2.GaussianBlur(small, (0, 0), 6), (w, h), interpolation=cv2.INTER_LINEAR)
        a = self.mask[..., None]
        return (frame * a + bg * (1 - a)).astype(np.uint8)

    def _draw_wig(self, frame):
        h, w = frame.shape[:2]
        # user fit: scale about the anchor centroid, then offset (in wig-image pixels)
        c = self.wig.ref.mean(0)
        s = self.fit["scale"]
        tweak = np.array([[s, 0, (1 - s) * c[0] + self.fit["dx"]],
                          [0, s, (1 - s) * c[1] + self.fit["dy"]],
                          [0, 0, 1]])
        m = similarity_transform(self.wig.ref, self.pts) @ tweak
        warped = cv2.warpAffine(self.wig.image, m, (w, h), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        a = warped[..., 3:].astype(np.float32) / 255
        return (warped[..., :3] * a + frame * (1 - a)).astype(np.uint8)

    def process(self, frame, ts):
        h, w = frame.shape[:2]
        image = to_mp_image(frame)
        self._track(image, ts, w, h)
        out = self._blur_background(frame, image, ts) if self.blur else frame
        if self.show_wig and self.pts is not None:
            out = self._draw_wig(out)
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
        sys.exit(f"Virtual camera unavailable: {e}\n"
                 "Install OBS Studio, click 'Start Virtual Camera' once, then stop it and quit OBS "
                 "before starting GlamCam. (Or run with --no-vcam to just preview.)")
    print(f"Broadcasting {w}x{h} to: {cam.device}")
    return cam


def draw_hud(img, text):
    cv2.putText(img, text, (16, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (16, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (210, 95, 255), 2, cv2.LINE_AA)


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

    renderer = GlamRenderer(wigs[current], blur=not args.no_blur, smooth=args.smooth)
    cam = None if args.no_vcam else open_virtual_camera(w, h, args.fps)

    print("Keys" + __doc__.split("Keys", 1)[1])
    keys = {ord("w"): (0, -4, 1), ord("s"): (0, 4, 1), ord("a"): (-4, 0, 1), ord("d"): (4, 0, 1),
            ord("-"): (0, 0, 1 / 1.03), ord("="): (0, 0, 1.03), ord("+"): (0, 0, 1.03)}
    t0, last_ts, hud_until = time.monotonic(), -1, time.monotonic() + 4
    try:
        while ok:
            ts = max(int((time.monotonic() - t0) * 1000), last_ts + 1)  # must strictly increase
            last_ts = ts
            out = renderer.process(frame, ts)
            if cam:
                cam.send(out)
            preview = cv2.flip(out, 1)
            if time.monotonic() < hud_until:
                draw_hud(preview, f"{renderer.wig.name}  (n = next wig)")
            cv2.imshow("GlamCam preview (mirrored, Meet sees it un-mirrored)", preview)

            k = cv2.waitKey(1) & 0xFF
            if k in (ord("q"), 27):
                break
            if k in keys:
                dx, dy, sc = keys[k]
                renderer.nudge(-dx, dy, sc)  # preview is mirrored, so flip left/right
            elif k == ord("n"):
                current = (current + 1) % len(wigs)
                renderer.set_wig(wigs[current])
                hud_until = time.monotonic() + 2
            elif k == ord("b"):
                renderer.blur = not renderer.blur
            elif k == ord("h"):
                renderer.show_wig = not renderer.show_wig
            elif k == ord("p"):
                renderer.save_fits()
                print(f"Saved fit for {renderer.wig.name}")
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
    ap.add_argument("--smooth", type=float, default=0.5, help="0 = raw tracking, 0.9 = very smooth/laggy")
    ap.add_argument("--no-vcam", action="store_true", help="preview only, don't send to the virtual camera")
    args = ap.parse_args()

    if args.download_models:
        download_models()
        print("Models ready.")
    elif args.list_wigs:
        for wg in load_wigs():
            print(f"  {wg.id:<20} {wg.name}")
    elif args.make_wig:
        png = make_wig(args.make_wig, args.name or Path(args.make_wig).stem)
        print(f"New wig saved: {png}\nTry it on:  --wig {png.stem}")
    else:
        run(args)


if __name__ == "__main__":
    main()
