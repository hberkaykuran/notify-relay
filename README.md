# notify-relay

Windows notifications on your second monitor, even while something is full-screen.

Windows hides notification banners whenever *any* app is full-screen, and it only
ever draws them on the primary display. notify-relay picks up every notification
anyway and shows it as a Windows 11-style card in a corner of the monitor you choose,
so you still see your messages during a game or a full-screen video.

- Cards look like native Windows 11 toasts: the app's icon, title, and body text.
- Older cards stack and peek out behind the newest one. Hover to fan them out and
  pause their timers. Left-click opens the app, right-click dismisses the card.
- Cards never take focus, so a game keeps keyboard and mouse input.
- Pinned apps stay on screen longer and can play a sound.
- Lives in the system tray, runs at sign-in if you want, and installs without admin rights.

## Install

Download `notify-relay-setup.exe` from
[Releases](../../releases) and run it.

- Needs **Windows 10 1809 or newer, or Windows 11**.
- **No admin rights needed.** It installs to `%LOCALAPPDATA%\Programs\notify-relay`.
- **SmartScreen will warn you** ("Windows protected your PC") because the exe is
  not code-signed. Click *More info*, then *Run anyway*.
- **The first launch opens a setup window** where you pick the monitor, the corner,
  timing, sound, pinned apps, "Hide Windows' own popups" and "Start with Windows".
  **Preview** shows sample cards. Nothing changes in Windows until you click **Finish**.
- **To uninstall,** use the tray icon or *Settings > Apps > Installed apps*.
  Uninstalling puts every app's popup setting back exactly as it was before the install.

It is made for two monitors. On a single monitor the cards go to the main screen,
where a full-screen app covers them.

## Why notifications vanish

It looks like broken notification settings, but it isn't one. Every toggle can be
correct (app notifications on, banners on, Do Not Disturb off, the app on the
priority list) and banners still never show.

The cause is `SHQueryUserNotificationState`. It returns `QUNS_BUSY` whenever any app
is full-screen anywhere on the system, and while it does, Windows withholds banners:

```
TOAST  quns=ACCEPTS_NOTIFICATIONS   fg: (normal window)     <- banner + sound
TOAST  quns=BUSY(fullscreen+D3D)    fg: browser (video)     <- nothing
TOAST  quns=BUSY(fullscreen+D3D)    fg: explorer            <- nothing
```

Three things make it worse than it sounds:

1. **Do Not Disturb is not involved.** The DND profile stays `Unrestricted` through
   every suppressed toast, so changing DND settings does nothing.
2. **The priority list doesn't help.** Apps on it are suppressed too.
3. **The full-screen app doesn't have to be in front.** A full-screen video in a
   background browser tab silences notifications everywhere, so it feels like
   notifications *never* work rather than "not while gaming".

There is no setting to move toasts to another monitor or to show them over
full-screen apps.

`diagnostics/quns_monitor.ps1` logs the suppression state, the foreground app and
the DND profile at the moment each toast arrives. Run it before you change any
setting. If `quns` shows anything other than `ACCEPTS_NOTIFICATIONS`, your settings
are not the problem. It watches WhatsApp by default; edit `$aumid` at the top to
watch a different app.

## How it works

`UserNotificationListener` still *receives* notifications when the banner is
suppressed, because suppression only affects what is displayed. The relay reads
notifications from the listener and draws its own cards on the other monitor. That
monitor isn't the one the full-screen app is on, so the cards are never hidden
behind it.

```
notification -> Windows -> UserNotificationListener -> notify-relay -> card on monitor 2
                   |
                   +-> native banner (optionally hidden per app: ShowBanner=0)
```

- **Waking up.** Windows keeps notifications in a SQLite store (`wpndatabase.db`,
  WAL mode), and every write updates the WAL-index header in `wpndatabase.db-shm`.
  The relay maps that file read-only and compares 48 bytes five times a second.
  It only queries the listener when those bytes change. Cards appear 0.1 to 0.3 s
  after a toast, and idle CPU is about 0.01 %.
- **Drawing.** Pillow renders each card once and hands it to DirectComposition
  (`dcomp.py`, through ctypes). Every slide, fade and scale is a GPU animation that
  DWM runs at the monitor's refresh rate. Python does no work per frame.
  Clicks outside the cards go through to the window underneath.
- **No duplicates.** With "Hide Windows' own popups" on, `banners.py` sets
  `ShowBanner=0` per app and backs up the original values, so each notification
  shows up in one place. Notifications still reach the notification centre (Win+N).
- **Clock jumps.** Tcl timers run on wall-clock time, so if the system clock jumps
  by more than 60 s, the relay restarts itself instead of stalling.

## Configuration

To change settings, click the tray icon, then **Settings…**. They're saved to
`%APPDATA%\notify-relay\settings.json` and take effect right away.

| Setting | Meaning |
|---|---|
| `monitor` | `"secondary"`, `"primary"`, or a display index |
| `position` | `bottom-left`, `bottom-right`, `top-left`, `top-right` |
| `duration` / `duration_pinned` | seconds on screen |
| `max_cards` | how many cards stack before the oldest is dropped |
| `sound_mode` | `never`, `suppressed` (only when Windows is silencing notifications), `always` |
| `sound_file` / `sound_volume` | any 16-bit PCM `.wav`; volume above `1.0` amplifies |
| `pinned` | AUMIDs that get the longer duration and the sound |
| `hide_windows_popups` | sets `ShowBanner=0` per app, backing up the originals |
| `start_with_windows` | the HKCU `Run` value `notify-relay` |

### Unpackaged apps

Microsoft Store apps provide their own name and icon. Apps installed outside the
Store (Electron apps and the like) don't, so add them to `UNPACKAGED` in
`notify_relay.py`, keyed on their AUMID. To find an app's AUMID, send yourself a
notification from it, then run:

```powershell
sqlite3 "$env:LOCALAPPDATA\Microsoft\Windows\Notifications\wpndatabase.db" `
  "select h.PrimaryId from Notification n join NotificationHandler h on n.HandlerId=h.RecordId order by n.ArrivalTime desc limit 5;"
```

## Running from source

Needs Python 3.12 with the `winrt-*` packages listed in
`requirements-installed.txt`, plus `pillow` and `pystray`.

```powershell
pythonw launcher.py                    # run (tray icon appears)
pythonw launcher.py --settings         # open settings
python  launcher.py --test             # show sample cards, then exit
python  launcher.py --banners hide     # hide every app's popup, backing up originals
python  launcher.py --banners restore  # put the originals back
python  test_notify_relay.py           # self-check, prints "ok"
```

Only one copy runs at a time. Starting a second copy opens settings in the one
that's already running.

### Building the installer

```powershell
pip install pyinstaller pystray
winget install JRSoftware.InnoSetup
.\build.ps1        # tests -> PyInstaller (onedir) -> Inno Setup
```

The installer is written to `dist\notify-relay-setup.exe`.

## Files

| File | Purpose |
|---|---|
| `launcher.py` | Entry point |
| `notify_relay.py` | Reads notifications, lays out and renders the card stack |
| `dcomp.py` | DirectComposition overlay window, card surfaces, GPU animations |
| `banners.py` | Hides Windows' own popups and restores each app's original setting |
| `tray.py` | Tray icon (Settings, Preview, Pause, Uninstall, Quit) |
| `settings_gui.py`, `settings.py` | Settings / first-run window, settings storage, autostart |
| `make_icon.py` | Draws `icon.ico` |
| `build.ps1`, `installer.iss` | Builds the installer |
| `diagnostics/quns_monitor.ps1` | Logs why a notification was or wasn't shown |

## Known limits

- An exclusive-fullscreen game running *on the secondary monitor* covers the cards.
- The first notification from a brand-new app shows one native popup, because
  Windows only creates the app's settings key when that notification arrives.
  The relay hides that app's popups from then on.
- Unpackaged apps need an `UNPACKAGED` entry to get their icon and click-to-open.
- The 10 px gaps between fanned-out cards don't pass clicks through.
- Text is greyscale-antialiased, not ClearType.
- Only tested at 100 % display scaling.

## License

MIT. See [LICENSE](LICENSE).
