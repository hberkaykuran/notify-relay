"""Settings shared by the relay and its options window.

One JSON file in %APPDATA%\notify-relay, because an installed copy cannot write
next to its own exe. A dev checkout's old settings.json beside this module is
copied over once. Inside the relay only the Tk main thread writes it.
"""

import array
import json
import os
import shutil
import tempfile
import wave
import winreg

DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "notify-relay")
PATH = os.path.join(DIR, "settings.json")
LEGACY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULTS = {
    "monitor": "secondary",         # "secondary" | "primary" | monitor index
    "position": "bottom-left",      # {top,bottom}-{left,right}
    "duration": 8.0,                # seconds a normal card stays up
    "duration_pinned": 20.0,        # ...for pinned apps
    "max_cards": 5,
    "sound_mode": "suppressed",     # "never" | "suppressed" | "always"
    "sound_file": r"C:\Windows\Media\Windows Notify System Generic.wav",
    # The Windows notification wavs peak near a third of full scale, which reads
    # as far too quiet over a game. 1.0 is the file as recorded; this is louder.
    "sound_volume": 2.5,
    "pinned": ["5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"],
    "hide_windows_popups": True,    # ShowBanner=0 per app, see banners.py
    "start_with_windows": True,
    "first_run_done": False,        # the welcome window shows until Finish
    "paused": False,
    "seen_apps": {},                # aumid -> display name, for the pinned list
}

POSITIONS = ["bottom-left", "bottom-right", "top-left", "top-right"]
SOUND_MODES = {
    "never": "Never (Windows already plays one)",
    "suppressed": "Only when Windows is silenced by full-screen",
    "always": "Every time",
}


def load():
    """Defaults merged with whatever is on disk. Never raises."""
    if not os.path.exists(PATH) and os.path.exists(LEGACY):
        try:
            os.makedirs(DIR, exist_ok=True)
            shutil.copyfile(LEGACY, PATH)
        except OSError:
            pass
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULTS.items()}
    try:
        with open(PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    return cfg


def save(cfg):
    os.makedirs(DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, PATH)


def mtime():
    try:
        return os.path.getmtime(PATH)
    except OSError:
        return 0.0


RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_autostart(enabled, command):
    """Start with Windows via the per-user Run key; removing it is idempotent."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN) as k:
        if enabled:
            winreg.SetValueEx(k, "notify-relay", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(k, "notify-relay")
            except FileNotFoundError:
                pass


# --- volume -----------------------------------------------------------------
# winsound has no volume control, so gain is baked into a copy of the wav.
# That also allows >100%, which a quiet source file needs.
_cache = {}


def wav_at_volume(path, volume):
    """Return a path to `path` scaled by `volume`. Cached per (path, volume)."""
    if abs(volume - 1.0) < 0.01:
        return path
    key = (path, round(volume, 2))
    if key in _cache and os.path.exists(_cache[key]):
        return _cache[key]
    try:
        with wave.open(path, "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        if params.sampwidth != 2:       # only 16-bit PCM; anything else plays as-is
            return path
        samples = array.array("h")
        samples.frombytes(frames)
        lo, hi = -32768, 32767
        for i, v in enumerate(samples):
            s = int(v * volume)
            samples[i] = hi if s > hi else lo if s < lo else s
        out = os.path.join(tempfile.gettempdir(),
                           f"notify-relay-{abs(hash(key)) & 0xffffffff:08x}.wav")
        with wave.open(out, "wb") as w:
            w.setparams(params)
            w.writeframes(samples.tobytes())
        _cache[key] = out
        return out
    except Exception:
        return path
