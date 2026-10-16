"""Run: python test_notify_relay.py"""

import notify_relay as nr
import settings

PRIMARY = (0, 0, 2560, 1400)
SECOND = (-1920, 121, 0, 1201)
MONS = [(PRIMARY, True), (SECOND, False)]


def test_diff_seen():
    seen = set()
    fresh, seen = nr.diff_seen(seen, {1, 2})
    assert fresh == {1, 2}, fresh

    fresh, seen = nr.diff_seen(seen, {1, 2})
    assert fresh == set(), "already-shown notifications must not repeat"

    fresh, seen = nr.diff_seen(seen, {1, 2, 3})
    assert fresh == {3}, fresh

    # WhatsApp retracts on phone-read, then the same id can come back.
    fresh, seen = nr.diff_seen(seen, set())
    assert fresh == set() and seen == set(), seen
    fresh, seen = nr.diff_seen(seen, {3})
    assert fresh == {3}, "a resent notification must show again"


def test_target_rect():
    assert nr.target_rect("secondary", MONS) == SECOND
    assert nr.target_rect("primary", MONS) == PRIMARY
    assert nr.target_rect(0, MONS) == PRIMARY
    assert nr.target_rect(1, MONS) == SECOND
    assert nr.target_rect(9, MONS) == SECOND, "out-of-range index falls back"
    assert nr.target_rect("secondary", [(PRIMARY, True)]) == PRIMARY


def test_stack_layout_all_corners():
    heights = [100, 120, 90, 80]
    for pos in settings.POSITIONS:
        for expanded in (False, True):
            for x, y, w, h, a in nr.stack_layout(SECOND, pos, heights, expanded):
                assert SECOND[0] <= x and x + w <= SECOND[2], (pos, x)
                assert SECOND[1] <= y and y + h <= SECOND[3], (pos, y)

    # Expanded: newest (index 0) nearest the chosen corner.
    ys = [r[1] for r in nr.stack_layout(SECOND, "bottom-left", heights, True)]
    assert ys == sorted(ys, reverse=True), "bottom corner: newest lowest"
    ys = [r[1] for r in nr.stack_layout(SECOND, "top-left", heights, True)]
    assert ys == sorted(ys), "top corner: newest highest"

    # Collapsed: cards behind peek out away from the corner, narrower, and only
    # SHOWN of them are visible.
    col = nr.stack_layout(SECOND, "bottom-left", heights, False)
    assert col[1][1] < col[0][1] and col[1][2] < col[0][2], col
    assert all(r[3] == heights[0] for r in col), "collapsed cards share the front height"
    assert [r[4] > 0 for r in col] == [True] * nr.SHOWN + [False] * (len(col) - nr.SHOWN)


def test_clamp_lines():
    m = len                     # one "pixel" per character keeps this readable
    assert nr.clamp_lines("short", m, 20, 3) == "short"
    assert nr.clamp_lines("aaa bbb ccc", m, 7, 3) == "aaa bbb\nccc"
    out = nr.clamp_lines("one two three four five six", m, 9, 2)
    lines = out.split("\n")
    assert len(lines) == 2 and lines[-1].endswith("…"), out
    assert all(m(l) <= 9 for l in lines), out


def test_activate_ignores_unpackaged_apps():
    assert nr.activate("") is False, "no AUMID means nothing to activate"


def test_sound_never_doubles_up():
    accepts = nr.QUNS_ACCEPTS_NOTIFICATIONS
    busy = 2        # QUNS_BUSY - a full-screen app is running

    # Default mode: Windows plays the sound itself here, so the relay must not.
    assert nr.should_play_sound(True, accepts, "suppressed") is False
    # ...but covers for it when Windows silences the toast.
    assert nr.should_play_sound(True, busy, "suppressed") is True

    assert nr.should_play_sound(True, accepts, "always") is True
    assert nr.should_play_sound(True, busy, "never") is False

    # Unpinned apps stay silent in every mode.
    for mode in settings.SOUND_MODES:
        assert nr.should_play_sound(False, busy, mode) is False


def test_volume_scales_and_clips():
    import array
    import wave

    src = settings.DEFAULTS["sound_file"]

    def samples(path):
        with wave.open(path) as w:
            a = array.array("h")
            a.frombytes(w.readframes(w.getnframes()))
        return a

    def peak(path):
        return max(abs(v) for v in samples(path))

    assert settings.wav_at_volume(src, 1.0) == src, "1.0 must not copy the file"
    base = peak(src)
    louder = peak(settings.wav_at_volume(src, 2.0))
    assert louder > base, (base, louder)

    # Heavy gain must clamp inside the signed 16-bit range, not wrap around.
    # abs() is no good here: abs(-32768) is 32768, a legal sample.
    clipped = samples(settings.wav_at_volume(src, 100.0))
    assert max(clipped) <= 32767 and min(clipped) >= -32768, (max(clipped),
                                                              min(clipped))


def test_settings_roundtrip():
    cfg = settings.load()
    for key in settings.DEFAULTS:
        assert key in cfg, f"{key} missing from loaded settings"
    assert cfg["position"] in settings.POSITIONS
    assert cfg["sound_mode"] in settings.SOUND_MODES


# --- banners: every test runs against a throwaway registry subtree and backup file ---
def _banners_sandbox():
    import os, tempfile, winreg
    import banners
    root = r"Software\notify-relay-test\Settings"
    banners.ROOT = root
    banners.BACKUP = os.path.join(tempfile.mkdtemp(), "banners_backup.json")
    def put(aumid, value):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + "\\" + aumid) as k:
            winreg.SetValueEx(k, "LastNotificationAddedTime", 0, winreg.REG_QWORD, 1)
            if value is not None:
                winreg.SetValueEx(k, "ShowBanner", 0, winreg.REG_DWORD, value)
    return banners, put


def _banners_cleanup():
    import winreg
    def drop(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                subs = [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
        except OSError:
            return
        for name in subs:
            drop(path + "\\" + name)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    drop(r"Software\notify-relay-test")


def test_banners_restore_exact_original_states():
    banners, put = _banners_sandbox()
    try:
        put("app.absent", None)
        put("app.on", 1)
        put("app.off", 0)
        put(r"{GUID}\Nested\tool.exe", 1)          # an AUMID with backslashes
        assert set(banners.apps()) == {"app.absent", "app.on", "app.off",
                                       r"{GUID}\Nested\tool.exe"}, banners.apps()
        banners.hide_all()
        for a in banners.apps():
            assert banners._current(a) == 0, a
        banners.hide_all()                         # again: must not record our own 0s
        assert banners.restore_all() is True
        assert banners._current("app.absent") is None
        assert banners._current("app.on") == 1
        assert banners._current("app.off") == 0
        assert banners._current(r"{GUID}\Nested\tool.exe") == 1
        import os
        assert not os.path.exists(banners.BACKUP), "backup must go after a full restore"
    finally:
        _banners_cleanup()


def test_banners_failed_restore_keeps_backup():
    import os
    banners, put = _banners_sandbox()
    try:
        put("app.on", 1)
        banners.hide("app.on")
        real = banners._set
        def broken(aumid, value):
            raise OSError("simulated")
        banners._set = broken
        try:
            assert banners.restore_all() is False
        finally:
            banners._set = real
        assert os.path.exists(banners.BACKUP), "a failed restore must keep the record"
        assert banners.restore_all() is True and banners._current("app.on") == 1
    finally:
        _banners_cleanup()


def test_banners_damaged_backup_is_never_overwritten():
    import os
    banners, put = _banners_sandbox()
    try:
        put("app.on", 1)
        with open(banners.BACKUP, "w") as fh:
            fh.write("{not json")
        try:
            banners.hide("app.on")
            raise AssertionError("hide() must refuse a damaged backup")
        except ValueError:
            pass
        assert open(banners.BACKUP).read() == "{not json"
        assert banners._current("app.on") == 1, "nothing may change without a backup"
        assert banners.cli(["x", "--banners", "restore"]) == 1
    finally:
        _banners_cleanup()


def test_reader_never_writes_registry_or_backup():
    import inspect
    src = inspect.getsource(nr.reader)
    assert "banners" not in src and "winreg" not in src, "reader must only queue requests"
    assert '{"mute": aumid}' in src


def test_single_instance_and_restart_wait():
    import ctypes, threading
    nr.MUTEX = "Local\notify-relay-selftest"
    k32 = ctypes.WinDLL("kernel32")
    held = nr.single_instance(0)
    assert held, "first copy must get the mutex"
    assert nr.single_instance(0) is None, "second copy must not"
    threading.Timer(0.5, lambda: k32.CloseHandle(held)).start()
    got = nr.single_instance(5)                    # what a --restarted child does
    assert got, "a restarted copy must get the mutex once the parent lets go"
    k32.CloseHandle(got)


def test_autostart_command_is_background():
    assert nr.launch_command().endswith("--background"), nr.launch_command()


def test_cubic_segment_matches_ease():
    import dcomp
    a, b, T = 120.0, 20.0, 0.28
    c0, c1, c2, c3 = dcomp.cubic_terms(a, b, T)
    f = lambda t: c0 + c1 * t + c2 * t * t + c3 * t ** 3
    assert abs(f(0) - a) < 1e-6 and abs(f(T) - b) < 1e-6
    for s in (0.25, 0.5, 0.75):
        assert abs(f(s * T) - dcomp.ease_value(a, b, s)) < 1e-6


def test_card_renders_without_variable_font():
    saved = dict(nr.FONTS)
    nr._fonts.clear()
    try:
        for k in nr.FONTS:
            nr.FONTS[k] = [("no-such-font.ttf", "Regular")] + [f for f in saved[k] if f[0] != "SegUIVar.ttf"]
        item = {"app": "WhatsApp", "title": "Friend", "body": "a body " * 20, "icon": None}
        im = nr.render_card(item, depth=1, more="+2 more")
        assert im.size == (nr.WIDTH + 2 * nr.SHADOW, nr.card_height(item) + 2 * nr.SHADOW), im.size
        for k in nr.FONTS:                         # nothing at all: Pillow's own font
            nr.FONTS[k] = [("no-such-font.ttf", None)]
        nr._fonts.clear()
        assert nr.render_card(item).size[0] == nr.WIDTH + 2 * nr.SHADOW
    finally:
        nr.FONTS.clear(); nr.FONTS.update(saved); nr._fonts.clear()


def test_post_before_mainloop_and_after_destroy():
    import queue, threading, time, tkinter as tk
    root = tk.Tk(); root.withdraw()
    q = queue.Queue()
    post = nr.make_post(q, root)
    got, errors = [], []
    def worker():
        try:
            post({"n": 1})                         # before mainloop: must not raise or die
        except Exception as e:
            errors.append(e)
    t = threading.Thread(target=worker); t.start(); t.join(5)
    assert not errors and not t.is_alive(), errors
    root.bind("<<relay>>", lambda e: nr.drain_queue(q, got.append))
    root.after(0, lambda: nr.drain_queue(q, got.append))      # the startup drain
    start = time.monotonic()
    root.after(1000, root.quit); root.mainloop()
    assert got == [{"n": 1}], got
    root.destroy()
    post({"n": 2})                                 # after destroy: must not raise


def test_drain_isolates_failures():
    import queue
    q = queue.Queue()
    for n in (1, 2, 3):
        q.put({"n": n})
    done = []
    def handle(item):
        if item["n"] == 2:
            raise RuntimeError("bad item")
        done.append(item["n"])
    nr.drain_queue(q, handle)
    assert done == [1, 3], done


def test_clock_jump_check():
    import time
    off = time.time() - time.monotonic()
    assert not nr.clock_jumped(off)
    assert nr.clock_jumped(off, nr.CLOCK_JUMP + 5)
    assert nr.clock_jumped(off, -(nr.CLOCK_JUMP + 5))


def test_store_watch_sees_header_changes_once():
    import os, tempfile
    path = os.path.join(tempfile.mkdtemp(), "fake.db-shm")
    with open(path, "wb") as fh:
        fh.write(b"\0" * 32768)
    w = nr.StoreWatch(path)
    assert w.view, "the mapping must open"
    assert not w.changed(), "nothing changed yet"
    with open(path, "r+b") as fh:          # a writer bumps the header, as SQLite does
        fh.seek(8); fh.write(b"\1\0\0\0"); fh.flush()
    assert w.changed() and not w.changed(), "exactly one change reported"
    w.close()
    assert nr.StoreWatch(path + ".missing").changed(), "unmapped means always look"


def test_until_ok_retries_instead_of_dying():
    import threading
    calls = []
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OSError("platform not ready")
        return "listener"
    stop = threading.Event()
    assert nr.until_ok(flaky, stop, wait=0.01) == "listener" and len(calls) == 3
    stop.set()
    assert nr.until_ok(lambda: 1 / 0, stop, wait=0.01) is None, "a stopped thread gives up"


if __name__ == "__main__":
    test_diff_seen()
    test_target_rect()
    test_stack_layout_all_corners()
    test_clamp_lines()
    test_activate_ignores_unpackaged_apps()
    test_sound_never_doubles_up()
    test_volume_scales_and_clips()
    test_settings_roundtrip()
    test_banners_restore_exact_original_states()
    test_banners_failed_restore_keeps_backup()
    test_banners_damaged_backup_is_never_overwritten()
    test_reader_never_writes_registry_or_backup()
    test_single_instance_and_restart_wait()
    test_autostart_command_is_background()
    test_cubic_segment_matches_ease()
    test_card_renders_without_variable_font()
    test_post_before_mainloop_and_after_destroy()
    test_drain_isolates_failures()
    test_clock_jump_check()
    test_store_watch_sees_header_changes_once()
    test_until_ok_retries_instead_of_dying()
    print("ok")
