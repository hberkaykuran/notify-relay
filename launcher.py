"""Entry point for the packaged app (and `python launcher.py` in a checkout).

`--banners hide|restore` is handled before anything else is imported: the
uninstaller relies on it, and a broken Tk, winrt or pystray must not stop
Windows' popups being put back.
"""

import sys


def main():
    if "--banners" in sys.argv:
        import banners
        sys.exit(banners.cli(sys.argv))
    import notify_relay
    notify_relay.main()


if __name__ == "__main__":
    main()
