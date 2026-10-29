# PyInstaller build for GlamCam: dist/GlamCam.app on macOS, dist/GlamCam/GlamCam.exe on Windows.
# Build with ./build_app.sh (macOS) or build_app.bat (Windows); models must be downloaded first.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("mediapipe", "pyvirtualcam", "sounddevice", "_sounddevice_data"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

for model in ("face_landmarker.task", "selfie_segmenter.tflite", "hair_segmenter.tflite"):
    if not Path("models", model).exists():
        sys.exit(f"models/{model} is missing. Run: python glamcam.py --download-models")
datas += [("models", "models"), ("wigs", "wigs"), ("props", "props")]

a = Analysis(
    ["glamcam.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GlamCam",
    console=False,  # a window app: no terminal pops up
    icon="assets/icon.ico" if sys.platform == "win32" else "assets/icon.icns",
)
coll = COLLECT(exe, a.binaries, a.datas, name="GlamCam")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="GlamCam.app",
        icon="assets/icon.icns",
        bundle_identifier="com.jdonohoo.glamcam",
        info_plist={
            "CFBundleDisplayName": "GlamCam",
            "CFBundleShortVersionString": Path("VERSION").read_text().strip(),
            # without this macOS refuses camera access outright, with no pop-up
            "NSCameraUsageDescription": "GlamCam needs your camera to put a wig on you.",
            "NSMicrophoneUsageDescription": "GlamCam listens (never records) so your Cave Mode mouth moves when you talk.",
            "NSHighResolutionCapable": True,
        },
    )
