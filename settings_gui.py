"""Settings window, opened inside the running relay (tray, Start menu, first run).

It never writes settings.json itself. Save hands the fields it edits to `commit`,
which merges them into the relay's live settings on the Tk main thread, so a
setting the relay changed meanwhile (a newly seen app) is never overwritten.
"""

import os
import tkinter as tk
import winsound
from tkinter import filedialog, ttk

import notify_relay as nr
import settings

MEDIA = r"C:\Windows\Media"
WHATSAPP = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"


def monitor_choices():
    """[(label, value)] for the monitor dropdown."""
    mons = nr.monitors()
    out = []
    if len(mons) > 1:
        out.append(("Second monitor (recommended)", "secondary"))
    out.append(("Main monitor", "primary"))
    for i, (rect, primary) in enumerate(mons):
        left, top, right, bottom = rect
        tag = ", main" if primary else ""
        out.append((f"Display {i + 1} ({right - left}x{bottom - top}{tag})", i))
    return out


def sound_choices(current):
    """Windows notification wavs, plus whatever is currently configured."""
    names = []
    try:
        names = sorted(f for f in os.listdir(MEDIA)
                       if f.lower().startswith("windows ") and f.lower().endswith(".wav"))
    except OSError:
        pass
    paths = [os.path.join(MEDIA, n) for n in names]
    if current and current not in paths:
        paths.insert(0, current)
    return paths


def section(frm, row, text):
    ttk.Label(frm, text=text, font=("Segoe UI Variable Text Semibold", 10)).grid(
        row=row, column=0, columnspan=3, sticky="w", pady=(10 if row else 0, 4))
    return row + 1


class SettingsWindow:
    def __init__(self, root, cfg, commit, preview, end_preview, first_run, icon=None):
        self.cfg, self.commit = cfg, commit
        self.preview_cb, self.end_preview = preview, end_preview
        self.first_run = first_run
        self.top = top = tk.Toplevel(root)
        top.title("notify-relay" + (" setup" if first_run else " settings"))
        top.resizable(False, False)
        if icon:
            try:
                top.iconbitmap(icon)
            except tk.TclError:
                pass
        top.protocol("WM_DELETE_WINDOW", self.close)

        frm = ttk.Frame(top, padding=18)
        frm.grid(sticky="nsew")
        row = 0
        if first_run:
            ttk.Label(frm, text="Welcome to notify-relay", font=("Segoe UI Variable Display Semib", 14)).grid(
                row=row, column=0, columnspan=3, sticky="w")
            row += 1
            ttk.Label(frm, justify="left", wraplength=420, text=(
                "It shows your Windows notifications as cards in a corner of the screen you "
                "choose, even while a game or video is full-screen. Pick where they go, press "
                "Preview to see them, then Finish. You can change all of this later from the "
                "tray icon.")).grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 6))
            row += 1

        # --- placement ---
        row = section(frm, row, "Placement")
        ttk.Label(frm, text="Monitor").grid(row=row, column=0, sticky="w")
        self.mons = monitor_choices()
        self.monitor = ttk.Combobox(frm, state="readonly", width=42,
                                    values=[m[0] for m in self.mons])
        cur = cfg.get("monitor", "secondary")
        self.monitor.current(next((i for i, m in enumerate(self.mons) if m[1] == cur), 0))
        self.monitor.grid(row=row, column=1, columnspan=2, sticky="w", pady=2)
        row += 1
        ttk.Label(frm, text="Corner").grid(row=row, column=0, sticky="w")
        self.position = ttk.Combobox(frm, state="readonly", width=42, values=settings.POSITIONS)
        self.position.set(cfg.get("position", "bottom-left"))
        self.position.grid(row=row, column=1, columnspan=2, sticky="w", pady=2)
        row += 1

        # --- timing ---
        row = section(frm, row, "Timing")
        self.duration = self._spin(frm, row, "Seconds on screen", 2, 120, cfg.get("duration", 8))
        row += 1
        self.duration_pinned = self._spin(frm, row, "...for pinned apps", 2, 300,
                                          cfg.get("duration_pinned", 20))
        row += 1
        self.max_cards = self._spin(frm, row, "Max cards at once", 1, 10, cfg.get("max_cards", 5))
        row += 1

        # --- pinned apps ---
        row = section(frm, row, "Pinned apps (sound + longer on screen)")
        apps = dict(cfg.get("seen_apps") or {})
        apps.setdefault(WHATSAPP, "WhatsApp")
        for a in cfg.get("pinned", []):
            apps.setdefault(a, a)
        self.app_ids = sorted(apps, key=lambda a: apps[a].lower())
        box = ttk.Frame(frm)
        box.grid(row=row, column=0, columnspan=3, sticky="we")
        self.pins = tk.Listbox(box, selectmode="multiple", height=5, width=52,
                               exportselection=False, activestyle="none")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.pins.yview)
        self.pins.config(yscrollcommand=sb.set)
        self.pins.pack(side="left")
        sb.pack(side="left", fill="y")
        for i, a in enumerate(self.app_ids):
            self.pins.insert("end", apps[a])
            if a in cfg.get("pinned", []):
                self.pins.selection_set(i)
        row += 1
        ttk.Label(frm, text="Click to select. Apps appear here once they have sent a notification.",
                  foreground="#666").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        # --- sound ---
        row = section(frm, row, "Sound")
        ttk.Label(frm, text="Play").grid(row=row, column=0, sticky="w")
        self.modes = list(settings.SOUND_MODES.items())
        self.mode = ttk.Combobox(frm, state="readonly", width=42,
                                 values=[label for _, label in self.modes])
        cur_mode = cfg.get("sound_mode", "suppressed")
        self.mode.current(next((i for i, (k, _) in enumerate(self.modes) if k == cur_mode), 0))
        self.mode.grid(row=row, column=1, columnspan=2, sticky="w", pady=2)
        row += 1
        ttk.Label(frm, text="Sound file").grid(row=row, column=0, sticky="w")
        self.sound = ttk.Combobox(frm, width=42, values=sound_choices(cfg.get("sound_file")))
        self.sound.set(cfg.get("sound_file") or "")
        self.sound.grid(row=row, column=1, sticky="w", pady=2)
        ttk.Button(frm, text="...", width=3, command=self.browse).grid(
            row=row, column=2, sticky="w", padx=(4, 0))
        row += 1
        ttk.Label(frm, text="Volume").grid(row=row, column=0, sticky="w")
        vol = ttk.Frame(frm)
        vol.grid(row=row, column=1, columnspan=2, sticky="w", pady=2)
        self.volume = tk.DoubleVar(value=float(cfg.get("sound_volume", 1.0)))
        ttk.Scale(vol, from_=0.1, to=4.0, variable=self.volume, length=200,
                  command=lambda _v: self.vol_label.config(
                      text=f"{self.volume.get() * 100:.0f}%")).pack(side="left")
        self.vol_label = ttk.Label(vol, width=6, text=f"{self.volume.get() * 100:.0f}%")
        self.vol_label.pack(side="left", padx=(8, 0))
        ttk.Button(vol, text="Test", width=6, command=self.test_sound).pack(side="left")
        row += 1

        # --- windows ---
        row = section(frm, row, "Windows")
        self.hide = tk.BooleanVar(value=bool(cfg.get("hide_windows_popups", True)))
        ttk.Checkbutton(frm, variable=self.hide,
                        text="Hide Windows' own popups, so each notification shows only here"
                        ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.autostart = tk.BooleanVar(value=bool(cfg.get("start_with_windows", True)))
        ttk.Checkbutton(frm, variable=self.autostart, text="Start with Windows").grid(
            row=row, column=0, columnspan=3, sticky="w")
        row += 1

        # --- buttons ---
        bar = ttk.Frame(frm)
        bar.grid(row=row, column=0, columnspan=3, sticky="we", pady=(14, 0))
        self.status = ttk.Label(bar, text="", foreground="#2a7")
        self.status.pack(side="left")
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")
        ttk.Button(bar, text="Finish" if first_run else "Save", command=self.save).pack(
            side="right", padx=6)
        ttk.Button(bar, text="Preview", command=self.preview).pack(side="right")

        top.update_idletasks()
        top.lift()
        top.focus_force()

    @staticmethod
    def _spin(frm, row, label, lo, hi, value):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w")
        spin = ttk.Spinbox(frm, from_=lo, to=hi, width=6, increment=1)
        spin.set(int(value))
        spin.grid(row=row, column=1, sticky="w", pady=2)
        return spin

    def browse(self):
        path = filedialog.askopenfilename(parent=self.top, title="Notification sound",
                                          initialdir=MEDIA, filetypes=[("Wave files", "*.wav")])
        if path:
            self.sound.set(path)

    def collect(self):
        """Only the fields this window edits."""
        return {
            "monitor": self.mons[self.monitor.current()][1],
            "position": self.position.get(),
            "duration": float(self.duration.get()),
            "duration_pinned": float(self.duration_pinned.get()),
            "max_cards": int(self.max_cards.get()),
            "pinned": [self.app_ids[i] for i in self.pins.curselection()],
            "sound_mode": self.modes[self.mode.current()][0],
            "sound_file": self.sound.get() or None,
            "sound_volume": round(float(self.volume.get()), 2),
            "hide_windows_popups": bool(self.hide.get()),
            "start_with_windows": bool(self.autostart.get()),
        }

    def say(self, text, ok=True):
        self.status.config(text=text, foreground="#2a7" if ok else "#c33")

    def test_sound(self):
        path = self.sound.get()
        if not path or not os.path.exists(path):
            self.say("No such sound file", ok=False)
            return
        winsound.PlaySound(settings.wav_at_volume(path, float(self.volume.get())),
                           winsound.SND_FILENAME | winsound.SND_ASYNC)

    def preview(self):
        try:
            self.preview_cb(self.collect())
        except ValueError:
            self.say("Check the number fields", ok=False)

    def save(self):
        try:
            fields = self.collect()
        except ValueError:
            self.say("Check the number fields", ok=False)
            return
        if self.first_run:
            fields["first_run_done"] = True
        error = self.commit(fields)
        if error:
            self.say(error, ok=False)
        elif self.first_run:
            self.close()
        else:
            self.say("Saved")

    def close(self):
        self.end_preview()
        self.top.destroy()

    def exists(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False


def confirm_uninstall(root, on_yes, icon=None):
    """A small in-app confirmation; the uninstaller itself asks nothing further."""
    top = tk.Toplevel(root)
    top.title("Uninstall notify-relay")
    top.resizable(False, False)
    if icon:
        try:
            top.iconbitmap(icon)
        except tk.TclError:
            pass
    frm = ttk.Frame(top, padding=18)
    frm.pack()
    ttk.Label(frm, justify="left", wraplength=360, text=(
        "Remove notify-relay from this PC? Windows' own notification popups go back to "
        "exactly how they were before it was installed.")).pack(anchor="w")
    bar = ttk.Frame(frm)
    bar.pack(anchor="e", pady=(14, 0))
    ttk.Button(bar, text="Cancel", command=top.destroy).pack(side="right")
    ttk.Button(bar, text="Uninstall", command=lambda: (top.destroy(), on_yes())).pack(
        side="right", padx=6)
    top.lift()
    top.focus_force()
