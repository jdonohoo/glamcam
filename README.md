<p align="center">
  <img src="assets/hero.gif" alt="GlamCam: a cartoon mascot in a webcam window, cycling through neon wigs that follow its head as it sways" width="100%">
</p>

<p align="center">
  <b>A face-tracking wig for your webcam.</b><br>
  For meetings that could have been an email, but now have <i>hair</i>.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon%20%26%20Intel-ff5fd2?style=for-the-badge&labelColor=1b0b3a" alt="macOS">
  <img src="https://img.shields.io/badge/Windows-10%20%26%2011-00f0ff?style=for-the-badge&labelColor=1b0b3a" alt="Windows">
  <img src="https://img.shields.io/badge/Python-3.10%E2%80%933.13-7d56f4?style=for-the-badge&labelColor=1b0b3a" alt="Python 3.10-3.13">
  <img src="https://img.shields.io/badge/hair-yes-ffe95a?style=for-the-badge&labelColor=1b0b3a" alt="hair: yes">
</p>

---

## What is this?

You know that one coworker with *the hair*? Flowing. Luminous. Shampoo-commercial-in-slow-motion hair?

**GlamCam lets you wear it to standup.**

It finds your face 30 times a second, glues a wig to your forehead (it follows you when you lean, tilt, nod, or dramatically turn away from a bad idea), blurs the pile of laundry behind you, and pipes the whole thing into Google Meet, Zoom, or Teams as a normal-looking camera.

Ships with **Glaaron**, the hair that started it all, plus four neon wigs nobody asked for. Or [make your own](#-make-your-own-wig) from any photo.

| | |
|---|---|
| **Glossiness** | Very |
| **Latency** | Smaller than your attention span in a 9am |
| **HR approved** | Pending |
| **Real hair harmed** | 0 |

## How it works

<p align="center"><img src="assets/how.png" alt="Webcam → 478 face dots → wig glued on → background blurred → OBS Virtual Camera → Meet/Zoom/Teams"></p>

1. **MediaPipe** puts 478 dots on your face. GlamCam only listens to the forehead, brows, eyes and temples, so the wig doesn't flap when you talk.
2. The wig gets moved, scaled and rotated to match those dots, every frame.
3. A second model figures out which pixels are *you* and blurs everything else.
4. The result goes out through the **OBS Virtual Camera**, which every video app already knows how to use.

## The Wig Closet

<p align="center"><img src="assets/closet.png" alt="Five wigs on a cartoon mascot: Glaaron, Disco Inferno, Synthwave Mullet, Cyber Mohawk, The Executive, plus a slot for your own"></p>

| Wig | `--wig` | Vibe |
|---|---|---|
| Glaaron | `glaaron` | The original. The legend. Default. |
| Disco Inferno | `disco-inferno` | Has a comb in it. The comb lives there now. |
| Synthwave Mullet | `synthwave-mullet` | Business in the front, Miami Vice in the back. |
| Cyber Mohawk | `cyber-mohawk` | Do not attempt doorways. |
| The Executive | `the-executive` | Nine hairs. One dream. |

Press **`n`** while it's running to cycle through them.

---

## 🍎 Install on macOS

Works on Apple Silicon (M1–M4, Mac Studio, etc.) and Intel Macs. Takes about 5 minutes, 4 of which are downloads.

**1. Install Homebrew** (skip if `brew --version` already works). Paste the command from [brew.sh](https://brew.sh) into Terminal.

**2. Install Python 3.12 and OBS**

```bash
brew install python@3.12
```

```bash
brew install --cask obs
```

> macOS ships an ancient Python 3.9, and pyenv likes to hand you that one. GlamCam needs **3.10–3.13**. `setup.sh` hunts down the Homebrew one for you.

**3. Wake up the OBS Virtual Camera (one time only)**

Open **OBS** → click **Start Virtual Camera** (bottom right). If macOS asks to allow a system extension, allow it in **System Settings → General → Login Items & Extensions** (or **Privacy & Security** on older macOS). Then click **Stop Virtual Camera** and **quit OBS**. GlamCam borrows that camera, and OBS can't be using it at the same time.

**4. Get GlamCam**

```bash
git clone https://github.com/jdonohoo/glamcam.git
```

```bash
cd glamcam
```

(No git? Hit **Code → Download ZIP** on GitHub and unzip it.)

**5. Set it up**

```bash
./setup.sh
```

This makes a private `.venv`, installs everything, and downloads the face models (~5 MB). (Forgot this step? `run.sh` notices and does it for you.)

**6. Get glam**

```bash
./run.sh
```

Run this from **Terminal** or **iTerm2** on the Mac itself, not over SSH or from an SSH app like Termius. macOS won't give those camera access. The first time, macOS asks whether your terminal may use the camera: click **Allow** and GlamCam carries on.

> **Mac Studio / Mac mini / desktop Macs** have no built-in camera. If GlamCam grabs the wrong one (hi, iPhone Continuity Camera 👋), try `./run.sh --camera 1`, then `--camera 2`, and so on.

**7. In Google Meet**: **Settings → Video → Camera → OBS Virtual Camera**. Turn **off** Meet's own blur and effects (GlamCam already did that, and two blurs make a smudge).

---

## 🪟 Install on Windows

Works on Windows 10 and 11.

**1. Install Python 3.12 and OBS.** Open **PowerShell** or **Terminal** and run:

```powershell
winget install -e --id Python.Python.3.12
```

```powershell
winget install -e --id OBSProject.OBSStudio
```

(Or grab them from [python.org](https://www.python.org/downloads/) and [obsproject.com](https://obsproject.com). Either way, close and reopen your terminal afterwards so it notices Python.)

**2. Wake up the OBS Virtual Camera (one time only)**

Open **OBS** → click **Start Virtual Camera** → click **Stop Virtual Camera** → **close OBS**. GlamCam uses that same camera, and OBS can't be using it at the same time.

**3. Get GlamCam**

```powershell
git clone https://github.com/jdonohoo/glamcam.git
```

(No git? On GitHub, click **Code → Download ZIP**, then right-click the ZIP → **Extract All**.)

**4. Set it up.** Double-click **`setup.bat`** in the `glamcam` folder. Or, from a terminal:

```powershell
cd glamcam
```

```powershell
.\setup.bat
```

If Windows SmartScreen gets nervous, click **More info → Run anyway**. It's a text file that runs `pip install`. We promise. Look at it.

**5. Get glam.** Double-click **`run.bat`**, or with options:

```powershell
.\run.bat --wig disco-inferno
```

If the camera won't open: **Settings → Privacy & security → Camera →** turn on **Let desktop apps access your camera**.

**6. In Google Meet**: **Settings → Video → Camera → OBS Virtual Camera**. If it's not in the list, fully quit and reopen Chrome or Edge.

---

## 🎮 Controls

Click the preview window first, then:

| Key | Does |
|---|---|
| `w` `a` `s` `d` | Nudge the wig up / left / down / right |
| `-` `=` | Shrink / grow the wig |
| `n` | Next wig |
| `h` | Wig off / on (for when your skip-level joins) |
| `b` | Background blur off / on |
| `l` | Lighting match: dim the wig to match your room (off by default, because a wig lit by its own personal sun is funnier) |
| `p` | Save this wig's fit (remembered in `fit.json`) |
| `q` / `Esc` | Quit, return to your natural, disappointing hair |

The preview is mirrored like a mirror. Meet gets the un-mirrored version, the same way it handles a normal camera.

## 🧪 Make your own wig

<p align="center"><img src="assets/diy.png" alt="Step 1: a photo of Kevin. Step 2: AI cuts out only the hair. Step 3: you, wearing Kevin's hair."></p>

Any photo with a face and some hair works. GlamCam finds the face, cuts out *only* the hair, and saves it as a wig.

macOS:

```bash
./run.sh --make-wig ~/Pictures/kevin.jpg --name Kevin
```

```bash
./run.sh --wig kevin
```

Windows:

```powershell
.\run.bat --make-wig C:\Users\you\Pictures\kevin.jpg --name Kevin
```

```powershell
.\run.bat --wig kevin
```

**For the best wig:**

- Face the camera straight on, with the hair clearly visible. Plain backgrounds help.
- Screenshots of video calls work fine (that's how Glaaron was born). Bigger is better.
- If it sits a bit off, nudge it with `w a s d` / `- =` and press `p` to save.

Wigs you make go in **`my-wigs/`**, which is **git-ignored**, so you can't accidentally publish Kevin. Only the hair is kept: the face and background are blanked out of the file entirely, not just made transparent.

> 💅 **Wig etiquette:** Hair is personal. Ask before you wear someone's. Glaaron is here with Aaron's full, enthusiastic blessing. Aaron is reportedly very excited.

Want to add a wig for everyone? Put `name.png` + `name.json` in `wigs/` and open a PR. The neon wigs are drawn in code in [`tools/draw_wigs.py`](tools/draw_wigs.py), if you want to make more.

## ⚙️ All the options

| Option | Does |
|---|---|
| `--wig NAME` | Start with this wig (default `glaaron`) |
| `--list-wigs` | Show every wig you've got |
| `--make-wig PHOTO` | Turn a photo into a wig (`--name` to name it) |
| `--camera N` | Use camera number N (default 0) |
| `--width` / `--height` / `--fps` | Camera resolution (default 1280×720 @ 30) |
| `--smooth 0.7` | Steadier wig, a bit more lag (0–0.9, default 0.5) |
| `--no-blur` | Start with background blur off |
| `--match-lighting` | Start with lighting match on |
| `--no-vcam` | Preview only, don't send to the virtual camera |
| `--download-models` | Re-download the AI models |

## 🩹 Troubleshooting

**"GlamCam needs Python 3.10–3.13"**
macOS: `brew install python@3.12`, then `./setup.sh` again. Windows: `winget install -e --id Python.Python.3.12`, open a **new** terminal, then run `setup.bat` again.

**"Virtual camera unavailable"**
Install OBS, click **Start Virtual Camera** once, then stop it and quit OBS. If OBS is open with its own virtual camera running, GlamCam can't use it.

**Meet shows a black screen or "camera in use"**
OBS is probably still running its virtual camera. Quit OBS, then restart GlamCam.

**"OBS Virtual Camera" isn't in Meet's camera list**
Fully quit the browser (on Mac, ⌘Q, not just closing the window) and reopen it.

**Could not open camera**
On a Mac, run it from **Terminal** or **iTerm2** on the Mac itself. SSH sessions and SSH apps like Termius can't get camera permission (they never even show up in the Camera list). Make sure your terminal is switched on in **System Settings → Privacy & Security → Camera**, and if you just switched it on, quit the terminal (⌘Q) and reopen it. On Windows, check **Settings → Privacy & security → Camera**. Still stuck? Try `--camera 1` (or 2, 3…) and close other apps using the camera (Zoom, FaceTime, Photo Booth).

**The wig jitters**
`--smooth 0.7`. Good lighting on your face helps a lot too. Trackers love a ring light.

**The wig vanished**
It hides when it can't see your face for a third of a second. Come back into frame, or check that `h` didn't get bumped.

**Turning my head sideways looks weird**
The wig follows sliding, tilting and leaning in or out, but it's a flat picture, so it doesn't rotate in 3D. Keep it facing the camera. Like a newscaster. A glamorous newscaster.

## Credits

- Face tracking and segmentation: [MediaPipe](https://ai.google.dev/edge/mediapipe) (Apache 2.0 models, downloaded at setup)
- Virtual camera: [pyvirtualcam](https://github.com/letmaik/pyvirtualcam) + [OBS Studio](https://obsproject.com)
- Glaaron: Aaron's hair, used with permission and deep admiration
- Aesthetic: lovingly inspired by [Charm](https://charm.land) ✨

MIT licensed. See [LICENSE](LICENSE).

<p align="center"><sub>bad hair days &gt; /dev/null™</sub></p>
