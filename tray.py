"""System tray icon. Runs pystray on its own thread.

Tk is not thread-safe, so a menu click never touches the GUI: it post()s
{"cmd": name}, which queues it and wakes the Tk main thread.
"""

import threading

import pystray
from PIL import Image


def start(post, icon_path, can_uninstall, is_paused):
    def cmd(name):
        return lambda _icon, _item: post({"cmd": name})

    menu = pystray.Menu(
        pystray.MenuItem("Settings…", cmd("settings"), default=True),
        pystray.MenuItem("Preview", cmd("preview")),
        pystray.MenuItem(lambda _item: "Resume" if is_paused() else "Pause", cmd("pause")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Uninstall…", cmd("uninstall"), visible=can_uninstall),
        pystray.MenuItem("Quit", cmd("quit")),
    )
    icon = pystray.Icon("notify-relay", Image.open(icon_path), "notify-relay", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def refresh(icon, paused):
    icon.title = "notify-relay (paused)" if paused else "notify-relay"
    icon.update_menu()
