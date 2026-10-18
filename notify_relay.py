"""Mirror Windows notifications onto the secondary monitor.

Windows renders toasts on the primary display only, and withholds them entirely
while any app is full-screen (SHQueryUserNotificationState -> QUNS_BUSY). Neither
is configurable, so native toasts cannot do what this does. This reads
notifications through UserNotificationListener, which still receives them when
the banner is suppressed, and draws its own card on the secondary monitor - a
different display than the full-screen app, so there is no z-order fight.

Native banners are turned off separately by native_banners.ps1, so a notification
shows in exactly one place.

Options live in settings.json - run `python notify_relay.py --settings` for the GUI.
"""

import argparse
import asyncio
import base64
import ctypes
import io
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
import winsound
from ctypes import wintypes

import settings

POLL = 0.4              # ponytail: polling beats NotificationChanged, which needs
                        # package identity. Switch if this ever runs packaged.
CLOCK_JUMP = 60         # seconds. Tcl 8.6 'after' runs on wall time, so a backward
                        # clock jump (power cut -> bad clock -> NTP fix) stalls the
                        # Tk pump until the old time comes round again. Restart instead.

# Unpackaged apps (anything not from the Store) resolve no AppInfo, so Windows
# can give us neither a display name nor a logo. Supply both here, keyed by the
# AUMID the app passes to setAppUserModelId().
UNPACKAGED = {
    "com.squirrel.Discord.Discord": {
        "name": "Discord",
        "icon": os.path.expandvars(r"%LOCALAPPDATA%\Discord\app.ico"),
        "window": "Discord"},
}

# Windows 11 toast metrics and dark-theme palette (design 1a).
WIDTH, MARGIN, GAP, PAD, PAD_TOP, ICON = 360, 16, 10, 14, 10, 20
BG, FG_TITLE, FG_BODY, FG_APP, EDGE = "#2c2c2c", "#ffffff", "#c8c8c8", "#9d9d9d", "#444444"
RADIUS = 8
SHADOW = 14             # transparent margin around each card bitmap for its soft shadow
BODY_LINES = 3
# Segoe UI Variable is Windows 11 only; Windows 10 has the static Segoe UI files.
FONTS = {"title": [("SegUIVar.ttf", "Semibold Text"), ("seguisb.ttf", None)],
         "body": [("SegUIVar.ttf", "Regular"), ("segoeui.ttf", None)],
         "small": [("SegUIVar.ttf", "Regular Small"), ("segoeui.ttf", None)]}
SIZES = {"title": 14, "body": 13, "small": 12}
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

# Collapsed stack (design 2a): the newest card in front, SHOWN-1 older ones peeking
# out behind it, each PEEK px further back, scaled to (WIDTH - 2*INSET*depth)/WIDTH
# about its top centre, and darker. PEEK is tall enough for the header, so each strip
# says which app is waiting. Hovering fans the stack into a list. Seconds below.
SHOWN, PEEK, INSET = 3, 30, 7
# Depth is shown by darker colours, not opacity: a translucent strip would let the
# card in front show through. (surface, app-name colour) per depth.
SHADE = (("#2c2c2c", "#9d9d9d"), ("#252525", "#858585"), ("#1f1f1f", "#6f6f6f"))
T_MOVE, T_ENTER, T_EXIT = 0.28, 0.32, 0.18


# --- monitor geometry -------------------------------------------------------
def monitors():
    """Return [(work_rect, is_primary)] for each display."""
    out = []
    MONITORINFOF_PRIMARY = 1

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def handler(hmon, hdc, rect, lparam):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcWork
        out.append(((r.left, r.top, r.right, r.bottom),
                    bool(mi.dwFlags & MONITORINFOF_PRIMARY)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, cb(handler), 0)
    return out


def target_rect(monitor="secondary", mons=None):
    """Work rect for the configured monitor, falling back to primary."""
    mons = mons if mons is not None else monitors()
    if not mons:
        return (0, 0, 1920, 1080)
    if isinstance(monitor, int) and 0 <= monitor < len(mons):
        return mons[monitor][0]
    if monitor != "primary":
        for rect, primary in mons:
            if not primary:
                return rect
    for rect, primary in mons:
        if primary:
            return rect
    return mons[0][0]


def stack_layout(rect, position, heights, expanded):
    """Target (x, y, w, h, alpha) for each card, newest first.

    Expanded is a plain list, newest nearest the corner. Collapsed, every card
    takes the front card's height so the ones behind only show a strip of
    padding; cards past SHOWN park invisibly behind the last visible one.
    """
    left, top, right, bottom = rect
    base_x = left + MARGIN if position.endswith("left") else right - WIDTH - MARGIN
    out, offset = [], 0
    for i, h in enumerate(heights):
        if expanded:
            inset, edge, hh, alpha = 0, offset, h, 1.0
            offset += h + GAP
        else:
            depth = min(i, SHOWN - 1)
            inset, edge, hh = INSET * depth, PEEK * depth, heights[0]
            alpha = 1.0 if i < SHOWN else 0.0
        y = bottom - MARGIN - edge - hh if position.startswith("bottom") \
            else top + MARGIN + edge
        out.append((base_x + inset, y, WIDTH - 2 * inset, hh, alpha))
    return out


def clamp_lines(text, measure, width, lines):
    """Word-wrap `text` to `width` px and keep `lines` lines, ellipsising the last."""
    out, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}" if cur else word
        if cur and measure(trial) > width:
            out.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        out.append(cur)
    if len(out) > lines:
        last = out[lines - 1]
        while last and measure(last + "…") > width:
            last = last[:-1]
        out = out[:lines - 1] + [last.rstrip() + "…"]
    return "\n".join(out)


# --- notification suppression state -----------------------------------------
QUNS_ACCEPTS_NOTIFICATIONS = 5


def user_notification_state():
    """SHQueryUserNotificationState. 5 means Windows will present toasts itself."""
    try:
        state = ctypes.c_int()
        ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        return state.value
    except Exception:
        return QUNS_ACCEPTS_NOTIFICATIONS


def should_play_sound(pinned, state, mode):
    """Whether the relay plays a sound, given the suppression state and mode.

    ShowBanner=0 hides the popup but something still plays a sound on arrival, so
    "suppressed" - the default - only covers the full-screen case, where Windows
    silences the toast outright. "always" will double up outside full-screen.
    """
    if not pinned or mode == "never":
        return False
    if mode == "always":
        return True
    return state != QUNS_ACCEPTS_NOTIFICATIONS


# --- app activation ---------------------------------------------------------
def focus_window(title_part):
    """Raise the first visible top-level window whose title contains title_part."""
    user32 = ctypes.windll.user32
    found = []

    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def handler(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_part.lower() in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(cb(handler), 0)
    if not found:
        return False
    hwnd = found[0]
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE, in case it is minimised
    user32.SetForegroundWindow(hwnd)
    return True


def activate(aumid):
    """Bring the notification's app to the front.

    Packaged AUMIDs are 'PackageFamilyName!AppId' and can be activated through the
    shell. Unpackaged ones (a bare id passed to setAppUserModelId) cannot, so fall
    back to raising a window by title.
    """
    if not aumid:
        return False
    try:
        if "!" in aumid:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{aumid}"],
                             creationflags=0x08000000)   # CREATE_NO_WINDOW
            return True
        hint = UNPACKAGED.get(aumid, {}).get("window")
        return focus_window(hint) if hint else False
    except Exception as e:
        print("activate failed:", e, file=sys.stderr)
        return False


# --- notification source ----------------------------------------------------
def texts_of(n):
    try:
        binding = n.notification.visual.get_binding("ToastGeneric")
        els = binding.get_text_elements()
        return [els.get_at(i).text for i in range(els.size)]
    except Exception:
        return []


def appid_of(n):
    try:
        return n.app_info.app_user_model_id, n.app_info.display_info.display_name
    except Exception:
        return "", ""       # unpackaged apps cannot resolve AppInfo


WPNDB = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Microsoft\Windows\Notifications\wpndatabase.db")


def restart():
    """Replace this process with a fresh copy; the only cure for stalled Tk timers.

    --restarted makes the child wait for this process's single-instance mutex
    instead of treating itself as a second launch and exiting.
    """
    args = [a for a in sys.argv[1:] if a != "--restarted"] + ["--restarted"]
    if getattr(sys, "frozen", False):
        cmd, home = [sys.executable] + args, os.path.dirname(sys.executable)
    else:
        script = os.path.abspath(sys.argv[0])
        cmd, home = [sys.executable, script] + args, os.path.dirname(script)
    subprocess.Popen(cmd, cwd=home, creationflags=0x08000000)   # CREATE_NO_WINDOW
    os._exit(0)


def aumid_from_db(nid):
    """Fallback identity for unpackaged apps: look the id up in Windows' own store.

    UserNotification.id matches Notification.Id in wpndatabase. Opened read-only
    on the live file - copying it costs 4MB per notification for no benefit.
    """
    try:
        uri = "file:" + WPNDB.replace("\\", "/") + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.5) as con:
            row = con.execute(
                "select h.PrimaryId from Notification n "
                "join NotificationHandler h on n.HandlerId = h.RecordId "
                "where n.Id = ?", (nid,)).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def diff_seen(seen, live):
    """New ids to show, and the pruned 'seen' set.

    Pruning to 'live' is what lets a retracted-then-resent notification show
    again; without it WhatsApp's retract-on-phone-read would mute the resend.
    """
    return live - seen, set(live)


def to_icon_png(raw):
    """Normalise arbitrary logo bytes to a 48x48 PNG that Tk can load directly."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        im.thumbnail((ICON, ICON), Image.LANCZOS)
        flat = Image.new("RGBA", (ICON, ICON), (0, 0, 0, 0))
        flat.paste(im, ((ICON - im.width) // 2, (ICON - im.height) // 2), im)
        buf = io.BytesIO()
        flat.save(buf, "PNG")
        return base64.b64encode(buf.getvalue())
    except Exception:
        return None


SHM = WPNDB + "-shm"
WATCH_EVERY = 0.2       # s between looks at the store's change counter
SAFETY_EVERY = 30.0     # s: re-open the mapping and read once regardless


class StoreWatch:
    """Cheap "did Windows' notification store change?" check.

    The store is SQLite in WAL mode; every write updates the WAL-index header at
    the start of wpndatabase.db-shm, which the notification service keeps mapped.
    Mapping it read-only and comparing its first 48 bytes is a memory read, so
    looking five times a second costs nothing measurable. Measured: the header
    changes 0-40 ms after each toast, once per toast. A directory change
    notification was tried first and missed spaced toasts by up to 18 s (NTFS
    reports those only when its cache is flushed); file times read through a
    handle did not change at all.
    """

    def __init__(self, path=SHM):
        self.path, self.view, self.handles, self.last = path, None, [], None
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileMappingW.restype = wintypes.HANDLE
        k.CreateFileMappingW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.c_size_t]
        k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.reopen()

    def reopen(self):
        self.close()
        k = self.k
        fh = k.CreateFileW(self.path, 0x80000000, 7, None, 3, 0x80, None)   # GENERIC_READ, share all
        if fh in (None, wintypes.HANDLE(-1).value):
            return False
        mp = k.CreateFileMappingW(fh, None, 0x02, 0, 0, None)              # PAGE_READONLY
        view = k.MapViewOfFile(mp, 0x0004, 0, 0, 0) if mp else None          # FILE_MAP_READ
        self.handles = [h for h in (mp, fh) if h]
        if not view:
            self.close()
            return False
        self.view = view
        self.last = ctypes.string_at(view, 48)
        return True

    def changed(self):
        """True once per change of the header since the last call (True if unmapped)."""
        if not self.view:
            return True
        cur = ctypes.string_at(self.view, 48)
        if cur != self.last:
            self.last = cur
            return True
        return False

    def close(self):
        if self.view:
            self.k.UnmapViewOfFile(self.view)
            self.view = None
        for h in self.handles:
            self.k.CloseHandle(h)
        self.handles = []


def until_ok(fn, stop, wait=5.0):
    """fn() until it stops raising (or stop is set); errors are logged, never fatal."""
    while not stop.is_set():
        try:
            return fn()
        except Exception as e:
            print("retrying after:", e, file=sys.stderr)
            stop.wait(wait)
    return None


def clock_jumped(offset, extra=0.0):
    """Has the wall clock moved against the monotonic clock since `offset` was taken?"""
    return abs(time.time() + extra - time.monotonic() - offset) > CLOCK_JUMP


def reader(post, stop, get_cfg):
    """Read the listener only when Windows' notification store has changed.

    The expensive part, the WinRT listener query, runs only after StoreWatch sees
    the store's change counter move; looking at it is a memory read. Each change
    re-reads three times (+0, +0.15, +0.4 s) to catch rows that land a moment after
    the counter moves. Every SAFETY_EVERY seconds the mapping is re-opened (in case
    the service recreated the file) and the list read once, and the clock-jump
    check runs on every pass.
    """
    from winrt.windows.ui.notifications.management import (
        UserNotificationListener, UserNotificationListenerAccessStatus)
    from winrt.windows.ui.notifications import NotificationKinds
    from winrt.windows.foundation import Size
    from winrt.windows.storage.streams import DataReader

    logos = {}   # aumid -> base64 png | None
    loop = asyncio.new_event_loop()

    async def logo_for(n, aumid):
        if aumid in logos:
            return logos[aumid]
        icon = None
        known = UNPACKAGED.get(aumid, {}).get("icon")
        if known:
            try:
                with open(known, "rb") as fh:
                    icon = to_icon_png(fh.read())
            except Exception:
                icon = None
        if icon is None:
            try:
                ref = n.app_info.display_info.get_logo(Size(48, 48))   # sharper downscaled than at 20
                stream = await ref.open_read_async()
                dr = DataReader(stream)
                await dr.load_async(stream.size)
                icon = to_icon_png(bytes(dr.read_buffer(stream.size)))
            except Exception:
                icon = None
        logos[aumid] = icon
        return icon

    state = {"seen": set(), "told": set(), "first": True}

    async def read_once(listener):
        ns = await listener.get_notifications_async(NotificationKinds.TOAST)
        by_id = {}
        for i in range(ns.size):
            n = ns.get_at(i)
            by_id[n.id] = n
        fresh, state["seen"] = diff_seen(state["seen"], set(by_id))
        if state["first"]:              # don't replay the backlog on startup
            state["first"] = False
            return 0
        pinned_ids = set(get_cfg().get("pinned", []))
        for nid in fresh:
            n = by_id[nid]
            aumid, name = appid_of(n)
            if not aumid:
                aumid = aumid_from_db(nid)
            if not name:
                name = UNPACKAGED.get(aumid, {}).get("name") or aumid
            if aumid:
                # Registry and settings writes belong to the main thread (one writer
                # each), so only ask for them here.
                post({"mute": aumid})
                if aumid not in state["told"]:
                    state["told"].add(aumid)
                    post({"seen": (aumid, name or aumid)})
            t = texts_of(n)
            post({"app": name or "Notification",
                  "title": t[0] if t else "",
                  "body": " ".join(t[1:]) if len(t) > 1 else "",
                  "pinned": aumid in pinned_ids,
                  "aumid": aumid,
                  "icon": await logo_for(n, aumid) if aumid else None})
        return len(fresh)

    # Test hook: pretend the wall clock jumped, to exercise this very check.
    extra = 0.0
    if "--restarted" not in sys.argv:
        extra = float(os.environ.get("NOTIFY_RELAY_TEST_CLOCK_OFFSET") or 0)

    try:
        def first_read():
            listener = UserNotificationListener.current
            status = loop.run_until_complete(listener.request_access_async())
            if status != UserNotificationListenerAccessStatus.ALLOWED:
                return None
            loop.run_until_complete(read_once(listener))      # the backlog, silently
            return listener

        # At login the notification platform may not be ready yet; a failure here must
        # not end the thread (cards would stop while Windows' popups stay hidden).
        listener = until_ok(first_read, stop)
        if listener is None:
            if not stop.is_set():
                post({"app": "notify-relay", "title": "No notification access",
                      "body": "Settings > Privacy & security > Notifications",
                      "pinned": True, "aumid": "", "icon": None})
            return
        offset = time.time() - time.monotonic()
        watch = StoreWatch()
        # Without the mapping (it failed to open), changed() is always True: that is
        # the old 0.4 s polling, so notifications still arrive, just less cheaply.
        interval = WATCH_EVERY if watch.view else POLL
        safety_at = time.monotonic() + SAFETY_EVERY
        while not stop.is_set():
            time.sleep(interval)
            if clock_jumped(offset, extra):
                restart()
            delays = (0, 0.15, 0.25) if watch.changed() else ()
            if time.monotonic() >= safety_at:
                safety_at = time.monotonic() + SAFETY_EVERY
                if watch.reopen():
                    interval = WATCH_EVERY
                delays = delays or (0,)
            try:
                for delay in delays:    # re-read only while the new row hasn't shown up
                    time.sleep(delay)
                    if loop.run_until_complete(read_once(listener)):
                        break
            except Exception as e:
                print("reader error:", e, file=sys.stderr)
    except Exception as e:
        print("reader stopped:", e, file=sys.stderr)


# --- card rendering ---------------------------------------------------------
_fonts = {}


def font(kind):
    """Segoe UI Variable where it exists (Windows 11), static Segoe UI otherwise, and
    Pillow's built-in font as a last resort: a missing font never stops a card."""
    if kind not in _fonts:
        from PIL import ImageFont
        chosen = None
        for name, variation in FONTS[kind]:
            try:
                f = ImageFont.truetype(os.path.join(FONT_DIR, name), SIZES[kind])
                if variation:
                    f.set_variation_by_name(variation)
                chosen = f
                break
            except Exception:
                continue
        _fonts[kind] = chosen or ImageFont.load_default()
    return _fonts[kind]


def card_texts(item):
    """(title, body) already clamped to the card's width."""
    wrap = WIDTH - 2 * PAD
    title = clamp_lines(item.get("title") or "", font("title").getlength, wrap, 1)
    body = clamp_lines(item.get("body") or "", font("body").getlength, wrap, BODY_LINES)
    return title, body


def card_height(item):
    title, body = card_texts(item)
    h = PAD_TOP + ICON + 8
    if title:
        h += 19
    if body:
        h += 2 + 18 * (body.count("\n") + 1)
    return h + PAD


def render_card(item, depth=0, more=""):
    """The card as an RGBA image with a SHADOW px transparent margin for its shadow."""
    from PIL import Image, ImageDraw, ImageFilter
    bg, app_fg = SHADE[depth]
    h = card_height(item)
    size = (WIDTH + 2 * SHADOW, h + 2 * SHADOW)
    s = SHADOW
    shade = Image.new("L", size, 0)
    ImageDraw.Draw(shade).rounded_rectangle((s, s + 3, s + WIDTH, s + h + 3), RADIUS, fill=95)
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    im.putalpha(shade.filter(ImageFilter.GaussianBlur(6)))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((s, s, s + WIDTH - 1, s + h - 1), RADIUS, fill=bg, outline=EDGE)
    x = s + PAD
    mid = s + PAD_TOP + ICON // 2
    if item.get("icon"):
        try:
            icon = Image.open(io.BytesIO(base64.b64decode(item["icon"]))).convert("RGBA")
            im.alpha_composite(icon.resize((ICON, ICON)), (x, s + PAD_TOP))
            x += ICON + 8
        except Exception:
            pass
    d.text((x, mid), item.get("app") or "", font=font("small"), fill=app_fg, anchor="lm")
    if more:
        d.text((s + WIDTH - PAD, mid), more, font=font("small"), fill=app_fg, anchor="rm")
    title, body = card_texts(item)
    y = s + PAD_TOP + ICON + 8
    if title:
        d.text((s + PAD, y), title, font=font("title"), fill=FG_TITLE)
        y += 19 + 2
    if body:
        d.multiline_text((s + PAD, y), body, font=font("body"), fill=FG_BODY, spacing=3)
    return im


CLOSE = 22              # close glyph box, top-right of the card


def render_close():
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (CLOSE, CLOSE), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    a, b = 7, CLOSE - 8
    d.line((a, a, b, b), fill=FG_APP, width=1)
    d.line((a, b, b, a), fill=FG_APP, width=1)
    return im


# --- overlay ----------------------------------------------------------------
def _bbox(rects):
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


class Card:
    def __init__(self, item, ttl):
        self.item, self.remaining = item, ttl
        self.started = time.monotonic()        # None while the countdown is paused
        self.h = card_height(item)
        self.vis = self.close = None
        self.content = None                    # (depth, more) currently shown
        self.surfaces = {}                     # (depth, more) -> surface on the current device
        self.rest = None                       # resting screen rect (l, t, r, b)
        self.gone_at = None                    # monotonic time its exit animation ends


class Overlay:
    """The card stack, composed by DirectComposition (see dcomp.py).

    Python only decides where things go and commits; DWM animates every frame.
    Nothing is ever resized or re-laid out mid-flight, so nothing can flash.
    """

    def __init__(self, root, cfg):
        self.root = root
        self.cards = []            # oldest first
        self.leaving = []
        self.expanded = False
        self.comp = None
        self.rebuilds = 0
        self.tracking = False
        self.collapse_job = self.expire_job = self.settle_job = None
        self.apply(cfg)

    # --- device and window ---
    def column(self):
        """The overlay window: a column at the chosen corner, card width plus shadow."""
        left, top, right, bottom = self.rect
        x = left + MARGIN if self.position.endswith("left") else right - WIDTH - MARGIN
        return (x - SHADOW, top, x + WIDTH + SHADOW, bottom)

    def _ensure(self):
        if self.comp is None:
            import dcomp
            self.comp = dcomp.Compositor(self.column(), self._on_message)

    def _safe(self, fn, *args):
        """Run fn; on a device error rebuild everything once and retry."""
        try:
            return fn(*args)
        except OSError:
            self._rebuild()
            return fn(*args)

    def _rebuild(self):
        self.rebuilds += 1
        try:
            if self.comp:
                self.comp.close()
        except Exception:
            pass
        self.comp = None
        self.leaving = []
        for c in self.cards:
            c.vis = c.close = c.content = None
            c.surfaces = {}
        self._ensure()
        for c in self.cards:
            self._materialize(c)
        self._layout(None, animate=False)

    def _materialize(self, c):
        import dcomp
        c.vis = dcomp.Visual(self.comp)
        c.vis.set_static(SHADOW + WIDTH / 2, SHADOW, WIDTH + 2 * SHADOW)
        surf = self.comp.surface(render_close())
        c.close = c.vis.child(surf, SHADOW + WIDTH - PAD - CLOSE + 6, SHADOW + PAD_TOP - 1)
        c.vis.child_opacity(c.close, 0.0, time.monotonic(), 0)

    def _show(self, c, depth, more):
        if c.content == (depth, more):
            return
        key = (depth, more)
        if key not in c.surfaces:
            c.surfaces[key] = self.comp.surface(render_card(c.item, depth, more))
        c.vis.set_content(c.surfaces[key])
        c.content = key

    # --- settings ---
    def apply(self, cfg):
        self.cfg = cfg
        self.rect = target_rect(cfg.get("monitor", "secondary"))
        self.position = cfg.get("position", "bottom-left")
        self._safe(self._apply)

    def _apply(self):
        self._ensure()
        self.comp.move_window(self.column())
        self._layout(None, animate=False)

    def play(self, pinned):
        cfg = self.cfg
        path = cfg.get("sound_file")
        if not path or not should_play_sound(pinned, user_notification_state(),
                                             cfg.get("sound_mode", "suppressed")):
            return
        try:
            wav = settings.wav_at_volume(path, float(cfg.get("sound_volume", 1.0)))
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            pass

    # --- cards ---
    def add(self, item):
        log = os.environ.get("NOTIFY_RELAY_TEST_LOG")       # tests only: card arrival times
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{time.time():.3f}\t{item.get('title', '')}\n")
        self.play(item.get("pinned"))
        ttl = float(self.cfg["duration_pinned"] if item.get("pinned") else self.cfg["duration"])
        card = Card(item, ttl)
        if self.expanded:
            card.started = None                # paused like the rest while hovered
        self._safe(self._add, card)

    def _add(self, card):
        self._ensure()
        self.comp.raise_top()
        self._materialize(card)
        self.cards.append(card)
        while len(self.cards) > int(self.cfg.get("max_cards", 5)):
            self._dismiss(self.cards[0], relayout=False)
        self._layout(card)
        self._schedule_expiry()

    def dismiss(self, c):
        self._safe(self._dismiss, c)

    def _dismiss(self, c, relayout=True):
        if c not in self.cards:
            return
        self.cards.remove(c)
        clock = time.monotonic()
        slide = -28 if self.position.endswith("left") else 28     # toward the edge
        if c.vis:
            c.vis.go(clock, T_EXIT, x=c.vis.props["x"].now(clock) + slide, opacity=0.0)
            c.gone_at = clock + T_EXIT
            self.leaving.append(c)
        if relayout:
            self._layout(None)
        self._schedule_expiry()
        self.hover_out()        # the pointer may have been on the card that just went

    # --- motion ---
    def _layout(self, entering, animate=True):
        comp = self.comp
        ox, oy = comp.origin
        order = list(reversed(self.cards))                      # newest first
        targets = stack_layout(self.rect, self.position, [c.h for c in order], self.expanded)
        hidden = len(order) - SHOWN
        clock = time.monotonic()
        rise = 16 if self.position.startswith("bottom") else -16
        paths = []                                              # one box per card's whole path
        for i, (c, (x, y, w, h, alpha)) in enumerate(zip(order, targets)):
            peeking = not self.expanded and i > 0
            depth = min(i, SHOWN - 1) if peeking else 0
            more = f"+{hidden} more" if peeking and i == SHOWN - 1 and hidden > 0 else ""
            self._show(c, depth, more)
            scale = w / WIDTH
            left = x - (WIDTH - w) / 2                          # unscaled card's left edge
            vx, vy = left - ox - SHADOW, y - oy - SHADOW
            clip = SHADOW + (h / scale if peeking else c.h) + SHADOW
            dur = (T_ENTER if c is entering else T_MOVE) if animate else 0
            if c is entering:
                c.vis.go(clock, 0, x=vx, y=vy + rise, opacity=0.0, scale=scale, clip=clip)
            c.vis.go(clock, dur, x=vx, y=vy, opacity=alpha, scale=scale, clip=clip)
            c.vis.child_opacity(c.close, 1.0 if self.expanded else 0.0, clock)
            old = c.rest
            if c is entering:
                old = (x, y + rise, x + w, y + h + rise)
            c.rest = (x, y, x + w, y + h) if alpha > 0 else None
            box = _bbox([r for r in (old, c.rest) if r])
            if box:
                paths.append(box)
        slide = 28
        for c in self.leaving:
            if c.rest:
                l, t, r, b = c.rest
                paths.append((l - slide, t, r + slide, b))
        comp.commit()
        # The region also clips drawing, so while anything moves it covers each card's
        # whole path; _settle shrinks it to the cards themselves once motion ends.
        self._region(paths)
        if self.settle_job:
            self.root.after_cancel(self.settle_job)
        longest = max(T_ENTER, T_MOVE, T_EXIT) if animate else 0
        self.settle_job = self.root.after(int(longest * 1000) + 40, self._settle)

    def _region(self, screen_rects):
        ox, oy = self.comp.origin
        self.comp.set_region([(l - ox - SHADOW, t - oy - SHADOW, r - ox + SHADOW, b - oy + SHADOW)
                              for (l, t, r, b) in screen_rects])

    def _settle(self):
        self.settle_job = None
        try:
            now = time.monotonic()
            removed = False
            for c in list(self.leaving):
                if c.gone_at and now >= c.gone_at:
                    self.leaving.remove(c)
                    if c.vis:
                        c.vis.destroy()
                        for surf in c.surfaces.values():
                            self.comp.release(surf)
                        removed = True
                    c.vis = None
            if removed:
                self.comp.commit()
            self._region([c.rest for c in self.cards if c.rest])
        except OSError:
            self._rebuild()

    # --- input (WS_EX_NOACTIVATE window; messages arrive through Tk's loop) ---
    def _on_message(self, msg, wp, lp):
        if msg == 0x0200:                                   # WM_MOUSEMOVE
            if not self.tracking:
                self.tracking = True
                self.comp.track_leave()
            self.hover_in()
            return 0
        if msg == 0x02A3:                                   # WM_MOUSELEAVE
            self.tracking = False
            self.hover_out()
            return 0
        if msg in (0x0202, 0x0205):                         # WM_LBUTTONUP, WM_RBUTTONUP
            x = ctypes.c_short(lp & 0xFFFF).value + self.comp.origin[0]
            y = ctypes.c_short((lp >> 16) & 0xFFFF).value + self.comp.origin[1]
            c = self.card_at(x, y)
            if c:
                if msg == 0x0202 and not self._on_close(c, x, y):
                    activate(c.item.get("aumid", ""))
                self.dismiss(c)
            return 0
        return None

    def card_at(self, x, y):
        for c in reversed(self.cards):                      # newest (front) first
            r = c.rest
            if r and r[0] <= x < r[2] and r[1] <= y < r[3]:
                return c
        return None

    def _on_close(self, c, x, y):
        if not self.expanded or not c.rest:
            return False
        l, t, r, _ = c.rest
        return r - PAD - CLOSE + 6 <= x < r - PAD + 6 and t + PAD_TOP - 1 <= y < t + PAD_TOP - 1 + CLOSE

    # --- hover: fan out and pause the countdown ---
    def hover_in(self, _e=None):
        if self.collapse_job:
            self.root.after_cancel(self.collapse_job)
            self.collapse_job = None
        if not self.expanded and self.cards:
            now = time.monotonic()
            for c in self.cards:
                if c.started is not None:
                    c.remaining -= now - c.started
                    c.started = None
            self.expanded = True
            self._safe(self._layout, None)
            self._schedule_expiry()

    def hover_out(self, _e=None):
        # Leaving one card for the gap between two expanded cards is not leaving the
        # stack, so wait, then test the pointer against the whole stack.
        if self.collapse_job:
            self.root.after_cancel(self.collapse_job)
        self.collapse_job = self.root.after(120, self._maybe_collapse)

    def _maybe_collapse(self):
        self.collapse_job = None
        if not self.expanded:
            return
        rects = [c.rest for c in self.cards if c.rest]
        if rects:
            px, py = self.root.winfo_pointerxy()
            if (min(r[0] for r in rects) <= px < max(r[2] for r in rects) and
                    min(r[1] for r in rects) <= py < max(r[3] for r in rects)):
                return
        self.expanded = False
        now = time.monotonic()
        for c in self.cards:
            if c.started is None:
                c.started = now
        if self.cards:
            self._safe(self._layout, None)
        self._schedule_expiry()

    # --- countdown: one timer for the next card due, none while hovered ---
    def _schedule_expiry(self):
        if self.expire_job:
            self.root.after_cancel(self.expire_job)
            self.expire_job = None
        live = [c for c in self.cards if c.started is not None]
        if self.expanded or not live:
            return
        due = min(c.started + c.remaining for c in live)
        self.expire_job = self.root.after(max(0, int((due - time.monotonic()) * 1000)) + 10,
                                          self._expire)

    def _expire(self):
        self.expire_job = None
        now = time.monotonic()
        for c in list(self.cards):
            if c.started is not None and now >= c.started + c.remaining:
                self.dismiss(c)
        self._schedule_expiry()

    def lose_device(self):
        """Test hook: the next compositor call fails as a removed device would."""
        if self.comp:
            self.comp.lose_device()


# --- app --------------------------------------------------------------------
MUTEX = "Local\\notify-relay"
SHOW_EVENT = "Local\\notify-relay-show"    # a second launch sets it: open settings
_mutex = None                              # held for the life of the process
PLACEMENT = ("monitor", "position", "max_cards")   # settings the overlay must re-apply

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.SetEvent.argtypes = [wintypes.HANDLE]
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD


def resource(name):
    """A file shipped with the app: beside the code, or inside PyInstaller's bundle."""
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), name)


def launch_command():
    """How Windows starts this copy at login. --background: never open a window."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(sys.argv[0])}" --background'


def single_instance(wait):
    """Own the named mutex, retrying for `wait` seconds; None if another copy has it."""
    deadline = time.monotonic() + wait
    while True:
        h = _k32.CreateMutexW(None, False, MUTEX)
        if ctypes.get_last_error() != 183:          # ERROR_ALREADY_EXISTS
            return h
        _k32.CloseHandle(h)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def hidden_root():
    """A withdrawn Tk root, without keeping the focus Tk takes while it starts.

    Tk activates its hidden main window during its first idle pass, whatever is done
    to that window beforehand. Windows only allows that when this process may take
    the foreground (launched by the active window, or no input for a while), and in
    exactly those cases it may also hand focus back, so do that at once.
    """
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = wintypes.HWND
    before = u.GetForegroundWindow()
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    now = u.GetForegroundWindow()
    if before and now != before:
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(now, ctypes.byref(pid))
        if pid.value == os.getpid():
            u.SetForegroundWindow(before)
    return root


def make_post(q, root):
    """put() plus a wake-up for the Tk main thread, safe to call from any thread.

    event_generate from a worker raises RuntimeError before mainloop() starts and
    TclError after destroy(); neither may kill the worker. Anything queued before the
    loop runs is picked up by the drain scheduled when it starts.
    """
    def post(item):
        q.put(item)
        try:
            root.event_generate("<<relay>>", when="tail")
        except (RuntimeError, tk.TclError):
            pass
    return post


def drain_queue(q, handle_item):
    """Handle everything queued. One failing item must not hold up the ones behind it."""
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            return
        try:
            handle_item(item)
        except Exception as e:
            print("item failed:", e, file=sys.stderr)
        if item.get("cmd") == "quit":
            return


PREVIEW = [("WhatsApp", "Preview", "This is where your notifications will appear."),
           ("Discord", "Older cards", "They peek out behind the newest one."),
           ("notify-relay", "Hover the stack", "It fans out, and nothing closes while you look.")]


def run_test(overlay, root):
    for i, (app, title, body) in enumerate(PREVIEW + [("Snipping Tool", "Fourth card",
                                                        "The last strip now says +1 more.")]):
        root.after(100 + 600 * i, lambda a=app, t=title, b=body: overlay.add(
            {"app": a, "title": t, "body": b, "pinned": False, "aumid": "", "icon": None}))
    root.after(9000, root.destroy)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", action="store_true", help="open the settings window")
    ap.add_argument("--test", action="store_true", help="show sample cards and exit")
    ap.add_argument("--background", action="store_true", help="started at login; no window")
    ap.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.test:
        root = hidden_root()
        run_test(Overlay(root, settings.load()), root)
        return

    global _mutex
    _mutex = single_instance(10 if args.restarted else 0)
    show = _k32.CreateEventW(None, False, False, SHOW_EVENT)
    if not _mutex:
        if not args.background:     # e.g. the Start-menu shortcut: show the running copy's settings
            _k32.SetEvent(show)
        return

    import banners
    import settings_gui
    import tray

    frozen = getattr(sys, "frozen", False)
    icon_path = resource("icon.ico")
    cfg = settings.load()
    root = hidden_root()
    overlay = Overlay(root, cfg)
    state = {"cfg": cfg, "win": None}
    q, stop = queue.Queue(), threading.Event()
    post = make_post(q, root)
    threading.Thread(target=reader, args=(post, stop, lambda: state["cfg"]), daemon=True).start()

    def wait_for_show():
        while True:
            if _k32.WaitForSingleObject(show, 0xFFFFFFFF) == 0:
                post({"cmd": "settings"})
    if show:                            # no event, no waiter: never spin on a failed wait
        threading.Thread(target=wait_for_show, daemon=True).start()

    def save(fields):
        """The one place settings.json is written."""
        state["cfg"].update(fields)
        settings.save(state["cfg"])
        if any(k in fields for k in PLACEMENT):
            overlay.apply(state["cfg"])

    def enforce():
        """Make Windows match the settings. Nothing changes before first-run Finish."""
        c = state["cfg"]
        if not c.get("first_run_done"):
            return None
        try:
            settings.set_autostart(bool(c.get("start_with_windows")), launch_command())
        except OSError as e:
            return f"Could not set autostart: {e}"
        try:
            if c.get("hide_windows_popups"):
                banners.hide_all()
            elif os.path.exists(banners.BACKUP) and not banners.restore_all():
                return "Some Windows popups could not be restored"
        except Exception as e:
            return f"Windows popups: {e}"
        return None

    def commit(fields):
        save(fields)
        return enforce()

    def preview(fields):
        overlay.apply(dict(state["cfg"], **fields))      # unsaved placement
        for app, title, body in PREVIEW:
            overlay.add({"app": app, "title": title, "body": body, "pinned": False,
                         "aumid": "", "icon": None})

    def end_preview():
        overlay.apply(state["cfg"])

    def open_settings():
        w = state["win"]
        if w and w.exists():
            w.top.deiconify()
            w.top.lift()
            w.top.focus_force()
            return
        state["win"] = settings_gui.SettingsWindow(
            root, state["cfg"], commit, preview, end_preview,
            first_run=not state["cfg"].get("first_run_done"), icon=icon_path)

    def quit_app():
        stop.set()
        try:
            icon.stop()
        except Exception:
            pass
        root.destroy()

    def uninstall():
        unins = os.path.join(os.path.dirname(sys.executable), "unins000.exe")
        if frozen and os.path.exists(unins):
            subprocess.Popen([unins])
            quit_app()

    def handle(cmd):
        if cmd == "settings":
            open_settings()
        elif cmd == "preview":
            preview({})
        elif cmd == "pause":
            save({"paused": not state["cfg"].get("paused")})
            tray.refresh(icon, state["cfg"]["paused"])
        elif cmd == "uninstall":
            settings_gui.confirm_uninstall(root, uninstall, icon=icon_path)
        elif cmd == "quit":
            quit_app()

    def handle_item(item):
        if "cmd" in item:
            handle(item["cmd"])
        elif "mute" in item:
            c = state["cfg"]
            if c.get("first_run_done") and c.get("hide_windows_popups"):
                banners.hide(item["mute"])       # raises on a damaged backup: never overwritten
        elif "seen" in item:
            aumid, name = item["seen"]
            seen = state["cfg"].get("seen_apps") or {}
            if seen.get(aumid) != name:
                save({"seen_apps": dict(seen, **{aumid: name})})
        elif not state["cfg"].get("paused"):
            overlay.add(item)

    def drain(_e=None):
        drain_queue(q, handle_item)

    root.bind("<<relay>>", drain)
    icon = tray.start(post, icon_path, can_uninstall=frozen,
                      is_paused=lambda: bool(state["cfg"].get("paused")))
    if state["cfg"].get("paused"):
        tray.refresh(icon, True)
    enforce()                           # e.g. a new app key since last run
    if args.settings or not state["cfg"].get("first_run_done"):
        root.after(300, open_settings)
    hook = os.environ.get("NOTIFY_RELAY_TEST_RESTART")   # tests only: restart on a timer
    if hook and not args.restarted:
        root.after(int(float(hook) * 1000), restart)
    root.after(0, drain)                # whatever was queued before the loop started
    try:
        root.mainloop()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
