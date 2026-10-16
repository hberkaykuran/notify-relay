"""Hide Windows' own notification popups, and put them back exactly as they were.

Banner off is not notification off: only ShowBanner is touched, so notifications
still reach the notification centre and UserNotificationListener, and therefore the
relay. Before the first write to an app's ShowBanner, its prior state (a value, or
None for "not set") goes into a backup file. restore_all() replays that file, so an
uninstall leaves every app as it found it, including ones the user had turned off.

Standard library only, and imported before anything else on the --banners path: the
uninstaller runs `--banners restore`, and a broken GUI dependency must not stop it.

One writer: inside the relay, every call happens on the Tk main thread.
"""

import json
import os
import sys
import winreg

import settings

ROOT = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
BACKUP = os.path.join(settings.DIR, "banners_backup.json")


def _read_backup():
    """The recorded originals. Raises if the file exists but cannot be read, so a
    damaged backup is never silently replaced with a partial one."""
    if not os.path.exists(BACKUP):
        return {}
    with open(BACKUP, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("banner backup is not an object")
    return data


def _write_backup(data):
    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    tmp = BACKUP + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, BACKUP)


def _current(aumid):
    """ShowBanner for one app, or None when it is not set."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ROOT + "\\" + aumid) as k:
            return winreg.QueryValueEx(k, "ShowBanner")[0]
    except OSError:
        return None


def _set(aumid, value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ROOT + "\\" + aumid) as k:
        if value is None:
            try:
                winreg.DeleteValue(k, "ShowBanner")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, "ShowBanner", 0, winreg.REG_DWORD, value)


def apps():
    """Every app key under ROOT. AUMIDs may contain backslashes, which the registry
    turns into nested keys, so walk the tree; a key holding values is an app."""
    out = []

    def walk(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ROOT + ("\\" + path if path else "")) as k:
                subs, values, _ = winreg.QueryInfoKey(k)
                names = [winreg.EnumKey(k, i) for i in range(subs)]
        except OSError:
            return
        if path and values:
            out.append(path)
        for name in names:
            walk(path + "\\" + name if path else name)

    walk("")
    return out


def hide_many(aumids):
    """Turn the popup off for these apps, backing up each original first.

    The first recorded value per app wins, so hiding again (or reinstalling after a
    failed restore) never overwrites the real original with our own 0.
    """
    backup = _read_backup()
    todo = [a for a in aumids if a not in backup]
    for a in todo:
        backup[a] = _current(a)
    if todo:
        _write_backup(backup)       # record before changing anything
    for a in aumids:
        if _current(a) != 0:
            _set(a, 0)


def hide(aumid):
    hide_many([aumid])


def hide_all():
    hide_many(apps())


def restore_all():
    """Put every recorded app back. True on full success, which also removes the
    backup; on any failure the backup stays so a later run can retry."""
    backup = _read_backup()
    failed = False
    for aumid, value in backup.items():
        try:
            _set(aumid, value)
        except OSError:
            failed = True
    if failed:
        return False
    try:
        os.remove(BACKUP)
    except FileNotFoundError:
        pass
    return True


def cli(argv):
    """`--banners hide|restore`. Exit code 0 only on success."""
    try:
        cmd = argv[argv.index("--banners") + 1]
    except (ValueError, IndexError):
        print("usage: --banners hide|restore", file=sys.stderr)
        return 2
    try:
        if cmd == "hide":
            hide_all()
            return 0
        if cmd == "restore":
            return 0 if restore_all() else 1
    except Exception as e:
        print("banners:", e, file=sys.stderr)
        return 1
    print("usage: --banners hide|restore", file=sys.stderr)
    return 2
