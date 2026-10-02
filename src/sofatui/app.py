"""Interactive terminal remote for the Apple TV, built on pyatv.

Keys act as remote buttons, and the buttons can be clicked with the mouse. Typing
"/" opens a command prompt with tab completion, e.g. "/stream <file>" plays a local
file on the Apple TV.
"""

import argparse
import asyncio
import logging
from importlib.metadata import version as package_version
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import sys
import termios
import time
import tty
from types import SimpleNamespace

from pwinput import pwinput
import pyatv
from pyatv import exceptions
from pyatv.support.net import get_local_address_reaching
from pyatv.const import (
    DeviceState,
    FeatureName,
    FeatureState,
    InputAction,
    PairingRequirement,
    PowerState,
    Protocol,
)
from pyatv.interface import DeviceListener, PowerListener, PushListener
from pyatv.storage.file_storage import FileStorage

from sofatui import __version__, background, pyatv_patches, ytdlp
from sofatui.background import PASSWORD_ENV
from sofatui.relay import Relay

# Where the AirPlay password is looked up when not given on the command line
PASSWORD_FILES = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sofatui" / "password",
    Path(".airplay-password"),
)
PASSWORD_ATTEMPTS = 3
SCAN_TIMEOUT = 5  # seconds to look for Apple TVs when no host is given

# --- styling ----------------------------------------------------------------

RESET, BOLD = "\x1b[0m", "\x1b[1m"
# Colors, plus the invisible "z" markers that Remote.zone() puts around clickable text
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mz]")
BLUE, PINK = (110, 170, 255), (255, 120, 200)


def fg(r, g, b):
    return f"\x1b[38;2;{r};{g};{b}m"


def bg(r, g, b):
    return f"\x1b[48;2;{r};{g};{b}m"


BORDER, TEXT, MUTED = fg(84, 92, 124), fg(222, 226, 242), fg(118, 126, 150)
ACCENT, GOOD, WARN, BAD = fg(*PINK), fg(120, 230, 160), fg(245, 200, 110), fg(255, 110, 110)
FLASH = bg(*PINK) + fg(24, 18, 32) + BOLD


def vlen(text):
    """Visible length of a string containing ANSI colors."""
    return len(ANSI_RE.sub("", text))


def gradient(text, start=BLUE, end=PINK):
    """Color text with a horizontal gradient."""
    steps = max(len(text) - 1, 1)
    return "".join(
        fg(*(round(a + (b - a) * i / steps) for a, b in zip(start, end))) + char
        for i, char in enumerate(text)
    )


def clip(text, width):
    return text if len(text) <= width else text[: width - 1] + "…"


def human_size(size):
    if size >= 1e9:
        return f"{size / 1e9:.1f} GB"
    return f"{size / 1e6:.0f} MB" if size >= 1e6 else f"{size / 1e3:.0f} kB"


def clock(seconds):
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


# --- buttons and keys -------------------------------------------------------

# button -> (label, feature, action)
BUTTONS = {
    "up": ("▲", FeatureName.Up, lambda r: r.atv.remote_control.up()),
    "down": ("▼", FeatureName.Down, lambda r: r.atv.remote_control.down()),
    "left": ("◀", FeatureName.Left, lambda r: r.atv.remote_control.left()),
    "right": ("▶", FeatureName.Right, lambda r: r.atv.remote_control.right()),
    "select": ("OK", FeatureName.Select, lambda r: r.atv.remote_control.select()),
    "back": ("BACK", FeatureName.Menu, lambda r: r.atv.remote_control.menu()),
    "home": ("HOME", FeatureName.Home, lambda r: r.atv.remote_control.home()),
    "play/pause": (
        "▶ II",
        FeatureName.PlayPause,
        lambda r: r.atv.remote_control.play_pause(),
    ),
    "skip back": (
        "« SKIP",
        FeatureName.SkipBackward,
        lambda r: r.atv.remote_control.skip_backward(),
    ),
    "skip forward": (
        "SKIP »",
        FeatureName.SkipForward,
        lambda r: r.atv.remote_control.skip_forward(),
    ),
    "power": ("POWER", None, lambda r: r.toggle_power()),
    # Long presses have keys of their own: a terminal does not report how long a key
    # is held. They light up the button they belong to (see LONG_PRESS).
    "long press ok": (
        "OK",
        FeatureName.Select,
        lambda r: r.atv.remote_control.select(action=InputAction.Hold),
    ),
    "long press home": (
        "HOME",
        FeatureName.Home,
        lambda r: r.atv.remote_control.home(action=InputAction.Hold),
    ),
    "long press back": (
        "BACK",
        FeatureName.Menu,
        lambda r: r.atv.remote_control.menu(action=InputAction.Hold),
    ),
    # Volume: hotkeys only, usable while the Apple TV reports that it has the controls
    "volume down": ("VOL -", FeatureName.VolumeDown, lambda r: r.atv.audio.volume_down()),
    "mute": ("MUTE", FeatureName.VolumeUp, lambda r: pyatv_patches.press_mute(r.atv)),
    "volume up": ("VOL +", FeatureName.VolumeUp, lambda r: r.atv.audio.volume_up()),
}

LONG_PRESS = {  # long press -> the button drawn for it
    "long press ok": "select",
    "long press home": "home",
    "long press back": "back",
}
LONG_PRESS_TIME = 1.0  # seconds pyatv holds the key down
POWER_HOLD_TIME = 1.0  # seconds the mouse must stay down on POWER to turn off
WAKE_ATTEMPTS = 4  # wake commands sent, three seconds apart, until the device is on

KEYS = {
    "\x1b[A": "up", "\x1bOA": "up",
    "\x1b[B": "down", "\x1bOB": "down",
    "\x1b[C": "right", "\x1bOC": "right",
    "\x1b[D": "left", "\x1bOD": "left",
    "\r": "select", "\n": "select",
    "\x1b": "back",
    "h": "home",
    " ": "play/pause",
    "[": "skip back",
    "]": "skip forward",
    "P": "power",
    "O": "long press ok",
    "H": "long press home",
    "B": "long press back",
    "m": "mute",
    "+": "volume up", "=": "volume up",
    "-": "volume down", "_": "volume down",
}  # fmt: skip
KEY_RE = re.compile(
    r"\x1b\[<[0-9;]*[Mm]|\x1b\[[0-9;]*[A-Za-z~]|\x1bO[A-Za-z]|.", re.S
)
MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")  # button, column, row
MOUSE_ON, MOUSE_OFF = "\x1b[?1000h\x1b[?1006h", "\x1b[?1006l\x1b[?1000l"

STATES = {
    DeviceState.Playing: ("▶", "PLAYING", GOOD),
    DeviceState.Paused: ("Ⅱ", "PAUSED", WARN),
    DeviceState.Loading: ("◌", "LOADING", WARN),
    DeviceState.Seeking: ("»", "SEEKING", WARN),
    DeviceState.Stopped: ("■", "STOPPED", MUTED),
    DeviceState.Idle: ("·", "IDLE", MUTED),
}

# command -> (argument, description), typed after "/"
COMMANDS = {
    "stream": ("<file or URL>", "play a file or page"),
    "stop": ("", "stop the stream"),
    "info": ("", "device details"),
    "help": ("", "list commands"),
    "quit": ("", "leave the remote"),
}
HIDDEN_COMMANDS = {"nerd"}  # work, but are not listed or completed
# How a web video reaches the Apple TV ("sofatui stream --via", "/stream --via")
VIA_MODES = {
    "auto": "relay if the site needs it, download if there is no single stream",
    "direct": "the Apple TV loads the video from the site itself",
    "relay": "all traffic goes through this machine, with the headers the site expects",
    "download": "download the whole video first, then stream the file",
}
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mp3", ".m4a", ".aac", ".wav"}
MAX_SUGGESTIONS = 6  # completion rows shown above the command prompt
CURSOR = "\x1b[7m \x1b[27m"

WIDTH = 46  # widest inner width of the card
# Smallest terminal rows / card width for the boxed and the compact layout
FULL_ROWS, FULL_WIDTH = 25, 36
COMPACT_ROWS, COMPACT_WIDTH = 11, 30
FLASH_TIME = 0.09  # seconds a pressed button stays lit
FLASH_GAP = 0.05  # dark pause before the next flash, so quick presses show one by one
FLASH_QUEUE = 3  # flashes that may wait their turn; more (key repeat) are not shown


class Card:
    """Collects the lines of a bordered card with a fixed inner width."""

    def __init__(self, width):
        self.width = width
        self.lines = [f"{BORDER}╭{'─' * (width + 2)}╮{RESET}"]

    def row(self, content="", right=None, center=False):
        space = self.width - vlen(content) - (vlen(right) if right else 0)
        if right is not None:
            content = f"{content}{' ' * space}{right}"
        elif center:
            content = f"{' ' * (space // 2)}{content}{' ' * (space - space // 2)}"
        else:
            content = f"{content}{' ' * space}"
        self.lines.append(f"{BORDER}│{RESET} {content} {BORDER}│{RESET}")

    def rule(self, label=""):
        line = "─" * (self.width + 2)
        if label:
            label = clip(label, self.width - 2)
            line = f"─ {MUTED}{label}{BORDER} " + "─" * (self.width - 1 - len(label))
        self.lines.append(f"{BORDER}├{line}┤{RESET}")

    def close(self):
        return [*self.lines, f"{BORDER}╰{'─' * (self.width + 2)}╯{RESET}"]


def media_files(text):
    """Directories and media files matching a partially typed path."""
    head = text[: text.rfind("/") + 1]
    prefix = text[len(head) :]
    try:
        entries = [
            entry
            for entry in os.scandir(os.path.expanduser(head) or ".")
            if entry.is_dir() or Path(entry.name).suffix.lower() in MEDIA_EXTENSIONS
        ]
    except OSError:
        return []

    def matching(fold):
        return [
            entry
            for entry in entries
            if fold(entry.name).startswith(fold(prefix))
            and (prefix.startswith(".") or not entry.name.startswith("."))
        ]

    found = matching(str) or matching(str.lower)
    found.sort(key=lambda entry: (not entry.is_dir(), entry.name.lower()))
    return [
        SimpleNamespace(
            path=head + entry.name + ("/" if entry.is_dir() else ""),
            label=entry.name + ("/" if entry.is_dir() else ""),
            hint="" if entry.is_dir() else human_size(entry.stat().st_size),
            color=fg(*BLUE) if entry.is_dir() else TEXT,
            final=False,  # a click fills it in, a second click plays it
        )
        for entry in found
    ]


class Remote(PushListener, PowerListener, DeviceListener):
    """Terminal remote control for one connected Apple TV."""

    def __init__(self, conf, password, atv):
        self.name = conf.name
        self.address = str(conf.address)
        self.password = password  # handed to background streams
        self.atv = atv
        self.subtitle = str(atv.device_info).replace(", ", " · ")
        self.playing = None
        self.playing_at = time.monotonic()
        self.app = None
        self.flashes = {}  # button -> (start, end) of its flashes, shown one by one
        self.power_hold = None  # (start, timer) while the mouse is held on POWER
        self.status = ("ready", MUTED)
        self.exit_message = None
        self.stop = asyncio.Event()
        self.queue = asyncio.Queue(maxsize=8)
        self.input = None  # text typed after "/", None when not in command mode
        self.cycle = None  # (candidates, index) while stepping through completions
        self.streaming = None  # name of the file streaming in the background
        self._tasks = set()  # running command tasks
        self._processes = []  # background streams started here, to reap them
        self.conf = conf
        self.view = "remote"  # or "info" / "nerd", the sheets opened by commands
        self.scroll = 0  # first visible row of a sheet
        self.stream_info = None  # state of the background stream, if any
        self._stream_phase = None
        self.stats = SimpleNamespace(
            connected_at=time.monotonic(),
            sent=0,
            failed=0,
            latency=0.0,  # seconds the last button took
            latency_total=0.0,
            updates=0,
            updated_at=None,
            frames=0,
            written=0,  # bytes sent to the terminal
            layout="",
        )
        self._candidates = (None, [], "")  # input, completions, heading
        self._zones = []  # click actions of the frame being built
        self.hitboxes = []  # (row, first column, last column, action) on screen
        self._last_frame = None

    # --- pyatv listeners ---

    def playstatus_update(self, updater, playstatus):
        self.playing = playstatus
        self.playing_at = time.monotonic()
        self.app = self._app_name()
        self.stats.updates += 1
        self.stats.updated_at = self.playing_at

    def playstatus_error(self, updater, exception):
        self.status = (f"push updates failed: {exception}", BAD)

    def powerstate_update(self, old_state, new_state):
        self.status = (f"power is now {new_state.name.lower()}", MUTED)

    def connection_lost(self, exception):
        if not self.stop.is_set():
            self.exit_message = f"Connection lost: {exception}"
            self.stop.set()

    def connection_closed(self):
        # Also called when we close the connection ourselves on quit
        if not self.stop.is_set():
            self.exit_message = "Connection closed by the Apple TV"
            self.stop.set()

    # --- device access ---

    def _app_name(self):
        try:
            app = self.atv.metadata.app
            return app.name if app else None
        except Exception:  # pylint: disable=broad-except
            return None

    def _power_on(self):
        try:
            return self.atv.power.power_state == PowerState.On
        except Exception:  # pylint: disable=broad-except
            return None

    async def toggle_power(self):
        if self._power_on():
            if self.atv.power.get(Protocol.Companion):
                # Paired with Companion: the same command the physical remote sends
                await self.atv.power.turn_off()
            else:
                # pyatv's way without Companion (hold Home, select) no longer works
                await pyatv_patches.sleep(self.atv)
            return
        # A wake sent while the Apple TV is still falling asleep is ignored: repeat it
        for _ in range(WAKE_ATTEMPTS):
            await self.atv.power.turn_on()
            for _ in range(12):
                await asyncio.sleep(0.25)
                if self._power_on():
                    return

    def available(self, button):
        feature = BUTTONS[button][1]
        if feature is None:
            feature = FeatureName.TurnOff if self._power_on() else FeatureName.TurnOn
        return self.atv.features.in_state(FeatureState.Available, feature)

    # --- input ---

    def read_keys(self, fd):
        for key in KEY_RE.findall(os.read(fd, 256).decode(errors="ignore")):
            mouse = MOUSE_RE.fullmatch(key)
            if mouse:
                if mouse[4] == "M":
                    self.mouse(int(mouse[1]), int(mouse[2]), int(mouse[3]))
                elif int(mouse[1]) == 0:  # left button released
                    self.end_power_hold()
            elif self.input is not None:
                self.input_key(key)
            elif key == "/":
                self.input = ""
            elif self.view != "remote":
                self.view_key(key)
            elif key in ("q", "Q", "\x03", "\x04"):
                self.stop.set()
            elif key in KEYS:
                self.press(KEYS[key])
        self.draw()

    def view_key(self, key):
        """Keys while a sheet is open: they scroll or close it, not press buttons."""
        if key in ("\x1b", "\r", "\n"):
            self.view = "remote"
        elif key in ("q", "Q", "\x03", "\x04"):
            self.stop.set()
        else:
            self.scroll += {
                "\x1b[A": -1, "\x1bOA": -1, "\x1b[B": 1, "\x1bOB": 1,
                "\x1b[5~": -5, "\x1b[6~": 5,
            }.get(key, 0)  # fmt: skip

    def show(self, view):
        """Open a sheet, or go back to the remote if it is already open."""
        self.view = "remote" if self.view == view else view
        self.scroll = 0

    def interrupt(self):
        """Ctrl+C leaves the command prompt first, then the remote."""
        if self.input is None:
            self.stop.set()
        else:
            self.input = self.cycle = None
            self.draw()

    def mouse(self, button, column, row):
        if button in (64, 65):  # wheel steps through completions or scrolls a sheet
            if self.input is not None:
                self.complete(1 if button == 65 else -1)
            elif self.view != "remote":
                self.scroll += 1 if button == 65 else -1
        elif button == 0:  # left click
            for line, first, last, (kind, target) in self.hitboxes:
                if line == row and first <= column <= last:
                    if kind == "button" and target == "power" and self._power_on():
                        self.begin_power_hold()
                    elif kind == "button":
                        self.press(target)
                    elif kind == "prompt":
                        self.input = ""
                    else:
                        self.pick(target)
                    break

    def begin_power_hold(self):
        """Turning off by mouse takes a held click, so a stray one cannot do it.

        Unlike keys, the mouse reports both press and release.
        """
        if not self.available("power"):
            self.status = ("power is not available right now", WARN)
            return
        loop = asyncio.get_running_loop()
        timer = loop.call_later(POWER_HOLD_TIME, self._power_hold_done)
        self.power_hold = (time.monotonic(), timer)
        self.status = ("keep holding to turn off…", WARN)
        self._power_hold_tick()

    def _power_hold_tick(self):
        if self.power_hold:  # redraw the button as it fills up
            self.draw()
            asyncio.get_running_loop().call_later(0.04, self._power_hold_tick)

    def _power_hold_done(self):
        self.power_hold = None
        self.press("power")

    def end_power_hold(self):
        if self.power_hold:
            self.power_hold[1].cancel()
            self.power_hold = None
            self.status = ("hold POWER for a second to turn off", MUTED)

    def pick(self, item):
        """Click on a completion: fill it in, or run it if nothing is left to add."""
        complete = not item.value.endswith(("/", " "))
        if complete and (item.final or item.value == self.input):
            self.input = self.cycle = None
            self.run_command(item.value)
        else:
            self.input, self.cycle = item.value, None

    def flash(self, button):
        """Light a button up; presses in quick succession blink one after another."""
        now = time.monotonic()
        # A long press lights up its button for as long as the key is held down
        duration = LONG_PRESS_TIME if button in LONG_PRESS else FLASH_TIME
        button = LONG_PRESS.get(button, button)
        flashes = self.flashes.setdefault(button, [])
        flashes[:] = [(start, end) for start, end in flashes if end + FLASH_GAP > now]
        if len(flashes) >= FLASH_QUEUE:
            return
        start = max([now] + [end + FLASH_GAP for _, end in flashes[-1:]])
        flashes.append((start, start + duration))
        # The regular redraw is too coarse for this: draw when it lights up and ends
        loop = asyncio.get_running_loop()
        for moment in (start, start + duration):
            loop.call_later(moment - now + 0.001, self.draw)

    def press(self, button):
        if not self.available(button):
            self.status = (f"{button} is not available right now", WARN)
            return
        self.flash(button)
        try:
            self.queue.put_nowait(button)
        except asyncio.QueueFull:
            pass

    async def worker(self):
        while True:
            button = await self.queue.get()
            started = time.monotonic()
            try:
                await BUTTONS[button][2](self)
                self.status = (f"sent {button}", ACCENT)
                self.stats.sent += 1
                self.stats.latency = time.monotonic() - started
                self.stats.latency_total += self.stats.latency
            except Exception as ex:  # pylint: disable=broad-except
                self.status = (f"{button} failed: {ex}", BAD)
                self.stats.failed += 1

    # --- command prompt ---

    def input_key(self, key):
        cycling, self.cycle = self.cycle, None
        accepts = cycling and self.input.endswith(("/", " "))
        if key in ("\t", "\x1b[B", "\x1bOB"):
            self.cycle = cycling
            self.complete(1)
        elif key in ("\x1b[Z", "\x1b[A", "\x1bOA"):
            self.cycle = cycling
            self.complete(-1)
        elif key in ("\r", "\n"):
            # Enter on a highlighted directory or command opens it instead of running
            if not accepts:
                command, self.input = self.input, None
                self.run_command(command)
        elif key == "\x1b":
            self.input = None
        elif key in ("\x7f", "\x08"):
            self.input = self.input[:-1] if self.input else None
        elif key == "\x15":  # ctrl+u
            self.input = ""
        elif key == "\x17":  # ctrl+w
            self.input = re.sub(r"[^ /]*[ /]*$", "", self.input)
        elif key == "/" and accepts and self.input.endswith("/"):
            pass
        elif len(key) == 1 and key.isprintable():
            self.input += key

    def candidates(self):
        """Completions for the typed text, each with the input it would produce."""
        if self._candidates[0] != self.input:
            command, space, arg = self.input.partition(" ")
            heading = "commands"
            if not space:
                items = [
                    SimpleNamespace(
                        value=name + (" " if usage else ""),
                        label=f"/{name} {usage}".strip(),
                        hint=text,
                        color=ACCENT,
                        final=True,  # a click runs it right away
                    )
                    for name, (usage, text) in COMMANDS.items()
                    if name.startswith(command)
                ]
            elif command == "stream" and arg.strip().lower().startswith("http"):
                items, heading = [], "web page, played through yt-dlp"
            elif command == "stream":
                items = media_files(arg.lstrip())
                for item in items:
                    item.value = f"{command} {item.path}"
                heading = f"{len(items)} match{'' if len(items) == 1 else 'es'}"
                if not items:
                    heading = "no matching media files"
            else:
                items, heading = [], ""
            self._candidates = (self.input, items, heading)
        return self._candidates[1]

    def complete(self, step):
        """Tab: complete as far as possible, then step through the candidates."""
        if self.cycle:
            items, index = self.cycle
            index = (index + step) % len(items)
        else:
            items = self.candidates()
            if not items:
                return
            if len(items) == 1:
                self.input = items[0].value
                return
            common = os.path.commonprefix([item.value for item in items])
            if len(common) > len(self.input):
                self.input = common
                return
            index = 0 if step > 0 else len(items) - 1
        self.cycle = (items, index)
        self.input = items[index].value

    def run_command(self, text):
        name, _, arg = text.strip().partition(" ")
        if not name:
            return
        if name not in COMMANDS and name not in HIDDEN_COMMANDS:
            self.status = (f"unknown command /{name} (try /help)", WARN)
            return
        getattr(self, f"cmd_{name}")(arg.strip())

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cmd_stream(self, arg):
        options = []
        via = re.match(r"--via\s+(\S+)\s*(.*)", arg)
        if via:
            if via[1] not in VIA_MODES:
                self.status = (f"--via takes one of: {', '.join(VIA_MODES)}", WARN)
                return
            options, arg = ["--via", via[1]], via[2].strip()

        path = Path(os.path.expanduser(arg))
        if not arg:
            self.status = ("usage: /stream [--via <mode>] <file or URL>", WARN)
        elif ytdlp.is_url(arg):
            self._spawn(self._start_stream(arg, "the web page", options))
        elif not path.is_file():
            self.status = (f"no such file: {arg}", BAD)
        else:
            self._spawn(self._start_stream(str(path.resolve()), path.name))

    def cmd_stop(self, arg):
        self._spawn(self._stop_stream())

    def cmd_info(self, arg):
        self.show("info")

    def cmd_nerd(self, arg):
        self.show("nerd")

    def cmd_help(self, arg):
        names = "  ".join(f"/{name} {usage}".strip() for name, (usage, _) in COMMANDS.items())
        self.status = (names, TEXT)

    def cmd_quit(self, arg):
        self.stop.set()

    async def _start_stream(self, source, label, options=()):
        """Play a file or page from a detached process, which survives quitting."""
        self.status = (f"starting stream of {label}…", WARN)
        try:
            process = await background.start(
                source, self.address, self.password, options
            )
        except background.StreamError as ex:
            self.status = (f"stream failed: {ex}", BAD)
        else:
            self._processes.append(process)
            self.streaming = label
            self.status = ("stream runs in the background", GOOD)

    async def _stop_stream(self):
        info = await asyncio.to_thread(background.stop, self.address)
        self.streaming = None
        if info:
            self.status = (f"stopped stream of {background.title(info)}", MUTED)
        else:
            self.status = ("no stream is running", MUTED)

    async def watch_stream(self):
        """Follow the background stream, which may start or end outside this remote."""
        while True:
            info = self.stream_info = background.running(self.address)
            name = background.title(info) if info else None
            phase = info.get("phase") if info else None
            if self.streaming and not name:
                # Its last words say why: playback ended, stopped, or what failed
                ending = background.last_log_line(self.address)
                failed = ending.startswith("Stream failed")
                self.status = (
                    ending or f"stream of {self.streaming} ended",
                    BAD if failed else MUTED,
                )
            elif phase and phase != self._stream_phase:
                self.status = (f"stream: {phase}", GOOD if phase == "playing" else WARN)
            self.streaming, self._stream_phase = name, phase
            self._processes = [p for p in self._processes if p.poll() is None]
            await asyncio.sleep(1)

    # --- rendering ---

    def zone(self, kind, target, text):
        """Make text clickable; draw() turns the markers into screen hitboxes."""
        self._zones.append((kind, target))
        return f"\x1b[{len(self._zones) - 1}z{text}\x1b[z"

    def _cell(self, button, width):
        label = BUTTONS[button][0].center(width)
        now = time.monotonic()
        if any(start <= now < end for start, end in self.flashes.get(button, ())):
            text = f"{FLASH}{label}{RESET}"
        elif button == "power" and self.power_hold:
            # Fills up from the left while the mouse is held down on it
            filled = round(width * (now - self.power_hold[0]) / POWER_HOLD_TIME)
            text = f"{FLASH}{label[:filled]}{RESET}{TEXT}{BOLD}{label[filled:]}{RESET}"
        elif not self.available(button):
            text = f"{BORDER}{label}{RESET}"
        else:
            text = f"{TEXT}{BOLD}{label}{RESET}"
        return self.zone("button", button, text)

    def _boxes(self, *buttons, width=8):
        b, r = BORDER, RESET

        def edge(button, text):  # borders are clickable too
            return self.zone("button", button, f"{b}{text}{r}")

        return [
            " ".join(edge(x, f"╭{'─' * width}╮") for x in buttons),
            " ".join(edge(x, "│") + self._cell(x, width) + edge(x, "│") for x in buttons),
            " ".join(edge(x, f"╰{'─' * width}╯") for x in buttons),
        ]

    def _badge(self):
        power = self._power_on()
        if power:
            return f"{GOOD}● ON{RESET}"
        return f"{MUTED}○ {'OFF' if power is False else '?'}{RESET}"

    def _media(self):
        """Collect what is playing right now, with the position extrapolated."""
        playing = self.playing
        state = playing.device_state if playing else DeviceState.Idle
        icon, label, color = STATES.get(state, ("·", state.name.upper(), MUTED))
        idle = state in (DeviceState.Idle, DeviceState.Stopped)

        total = playing.total_time if playing else None
        position = (playing.position if playing else None) or 0
        if state == DeviceState.Playing:
            position += time.monotonic() - self.playing_at
        if total:
            position = min(position, total)

        return SimpleNamespace(
            icon=icon,
            label=label,
            color=color,
            title=(playing.title if playing else None) or self._stream_title(idle),
            placeholder="Nothing playing" if idle else "Untitled media",
            artist=" · ".join(x for x in (playing.artist, playing.album) if x)
            if playing
            else "",
            position=position,
            total=total,
            times=f"{clock(position)} / {clock(total)}" if total else "",
        )

    def _stream_title(self, idle):
        """The title of our own stream, which the Apple TV does not report."""
        info = self.stream_info
        if info and info.get("phase") == "playing" and not idle and self.app == "AirPlay":
            return background.title(info)
        return None

    @staticmethod
    def _title(media, width):
        if media.title:
            return f"{TEXT}{BOLD}{clip(media.title, width)}{RESET}"
        return f"{MUTED}{clip(media.placeholder, width)}{RESET}"

    @staticmethod
    def _bar(media, width):
        filled = round(width * media.position / media.total) if media.total else 0
        return gradient("━" * filled) + f"{BORDER}{'─' * (width - filled)}{RESET}"

    def _status_row(self, card):
        message, color = self.status
        tag = f"{GOOD}⇡ {clip(self.streaming, 16)}{RESET}" if self.streaming else None
        room = card.width - 2 - (vlen(tag) + 1 if tag else 0)
        text = f"{ACCENT}›{RESET} {color}{clip(message, room).ljust(room)}{RESET}"
        card.row(self.zone("prompt", None, text), right=tag)  # click opens the prompt

    def _prompt(self, width):
        text = self.input
        if len(text) > width - 2:
            text = "…" + text[-(width - 3) :]
        return f"{ACCENT}{BOLD}/{RESET}{TEXT}{text}{RESET}{CURSOR}"

    def _suggestions(self):
        """Return (candidates, selected index, heading) for the command prompt."""
        # While stepping through candidates the list (and its heading) stays put
        items, selected = self.cycle or (self.candidates(), None)
        return items, selected, self._candidates[2]

    def _overlay(self, lines, width):
        """Draw the command prompt and its suggestions over the bottom of a card."""
        items, selected, heading = self._suggestions()
        room = max(0, min(MAX_SUGGESTIONS, len(lines) - 5, len(items)))
        first = max(0, min((selected or 0) - room // 2, len(items) - room))
        card = Card(width)
        card.lines.clear()
        card.rule(heading)
        for index in range(first, first + room):
            item = items[index]
            label = clip(item.label, width - 4 - len(item.hint))
            gap = " " * (width - 2 - len(label) - len(item.hint))
            hint = f"{gap}{MUTED}{item.hint}{RESET}"
            if index == selected:
                text = f"{ACCENT}▸ {TEXT}{BOLD}{label}{RESET}{hint}"
            else:
                text = f"  {item.color}{label}{RESET}{hint}"
            card.row(self.zone("suggest", item, text))
        card.row(self._prompt(width))
        overlay = card.close()
        return lines[: len(lines) - len(overlay)] + overlay

    def _full(self, width, spare):
        """Boxed remote; the key legend and spacing only appear if rows allow."""
        b, r = BORDER, RESET
        legend = [
            (("↑↓←→", "move"), ("enter", "ok"), ("esc", "back")),
            (("h", "home"), ("space", "play/pause"), ("[ ]", "skip")),
            (("O H B", "long press ok, home, back"),),
            (("/", "command"), ("P", "power"), ("q", "quit")),
        ]
        if self.available("volume up"):
            legend.insert(3, (("+ -", "volume"), ("m", "mute")))
        show_legend, roomy = spare > len(legend), spare > len(legend) + 3
        media = self._media()
        card = Card(width)

        name = gradient(clip(self.name, width - 10))
        card.row(f"{ACCENT}◆{r} {BOLD}{name}{r}", right=self._badge())
        card.row(f"  {MUTED}{clip(self.subtitle, width - 2)}{r}")
        card.rule()

        card.row(
            f"{media.color}{media.icon} {BOLD}{media.label}{r}",
            right=f"{MUTED}{clip(self.app or '', width - 14)}{r}",
        )
        card.row(self._title(media, width))
        card.row(f"{MUTED}{clip(media.artist, width)}{r}")
        if media.total:
            bar = self._bar(media, width - len(media.times) - 2)
            card.row(bar, right=f"{MUTED}{media.times}{r}")
        else:
            card.row(self._bar(media, width))
        card.rule()

        def edge(button, text):  # borders are clickable too
            return self.zone("button", button, f"{b}{text}{r}")

        dpad = (
            edge("up", "╭───────╮"),
            edge("up", "│") + self._cell("up", 7) + edge("up", "│"),
            edge("left", "╭───────")
            + edge("select", "┼───────┼")
            + edge("right", "───────╮"),
            edge("left", "│")
            + self._cell("left", 7)
            + edge("select", "│")
            + self._cell("select", 7)
            + edge("select", "│")
            + self._cell("right", 7)
            + edge("right", "│"),
            edge("left", "╰───────")
            + edge("select", "┼───────┼")
            + edge("right", "───────╯"),
            edge("down", "│") + self._cell("down", 7) + edge("down", "│"),
            edge("down", "╰───────╯"),
        )
        boxes = (
            *self._boxes("back", "home", "play/pause"),
            *self._boxes("skip back", "power", "skip forward"),
        )
        for block in (dpad, boxes):
            if roomy:
                card.row()
            for line in block:
                card.row(line, center=True)
        if roomy:
            card.row()
        card.rule()

        if show_legend:
            for pairs in legend:
                items = [f"{TEXT}{key}{r} {MUTED}{what}{r}" for key, what in pairs]
                line = "   ".join(items)
                card.row(line if vlen(line) <= width else "  ".join(items), center=True)
            card.rule()

        self._status_row(card)
        return card.close()

    def _compact(self, width):
        """Small card: no boxes, buttons next to the d-pad."""
        r = RESET
        media = self._media()
        cell = self._cell
        card = Card(width)

        name = gradient(clip(self.name, width - 8))
        card.row(f"{ACCENT}◆{r} {BOLD}{name}{r}", right=self._badge())
        times = f"{MUTED}{media.times}{r}"
        title = self._title(media, width - 2 - (len(media.times) + 2 if media.total else 0))
        card.row(f"{media.color}{media.icon}{r} {title}", right=times)
        card.row(self._bar(media, width))
        card.rule()

        card.row(
            f"    {cell('up', 4)}      {cell('back', 7)} {cell('home', 7)}",
            center=True,
        )
        card.row(
            f"{cell('left', 4)}{cell('select', 4)}{cell('right', 4)}"
            f"  {cell('play/pause', 7)} {cell('power', 7)}",
            center=True,
        )
        card.row(
            f"    {cell('down', 4)}      {cell('skip back', 7)} {cell('skip forward', 7)}",
            center=True,
        )
        card.rule()
        self._status_row(card)
        return card.close()

    def _tiny(self, cols):
        """Bare status lines for terminals too small for a card; keys still work."""
        media = self._media()
        state = f"{media.icon} {media.label}" + (f"  {media.times}" if media.total else "")
        message, color = self.status
        lines = [
            f"{ACCENT}{BOLD}{clip('◆ ' + self.name, cols)}{RESET}",
            f"{media.color}{clip(state, cols)}{RESET}",
            f"{color}{clip('› ' + message, cols)}{RESET}",
        ]
        if self.input is not None:
            items, selected, _ = self._suggestions()
            names = "  ".join(item.label for item in items[selected or 0 :])
            lines[1:] = [f"{MUTED}{clip(names, cols)}{RESET}", self._prompt(cols - 1)]
        return lines

    def _feature(self, feature):
        return self.atv.features.in_state(FeatureState.Available, feature)

    def _stream_rows(self):
        info = self.stream_info
        if not info:
            return [("Stream", "none")]
        rows = [("Stream", background.title(info))]
        if info.get("phase"):
            rows.append(("  state", info["phase"]))
        if info.get("via"):
            rows.append(("  route", info["via"]))
        if "started" in info:
            rows.append(("  running for", clock(time.time() - info["started"])))
        return rows

    def _info_rows(self):
        """Rows of the /info sheet: (label, value), or (heading, None)."""
        device, stats = self.atv.device_info, self.stats
        system = {"TvOS": "tvOS", "MacOS": "macOS"}.get(
            device.operating_system.name, device.operating_system.name
        )
        system = f"{system} {device.version or '?'}"
        if self._feature(FeatureName.SetVolume):
            volume = "yes, with level"
        elif self._feature(FeatureName.VolumeUp):
            volume = "yes, up and down only"
        else:
            volume = "not reported by the Apple TV"
        power = self._power_on()
        rows = [
            ("Device", None),
            ("Name", self.name),
            ("Model", device.model_str),
            ("System", system),
            ("Address", self.address),
            ("MAC", device.mac or "unknown"),
            ("Status", None),
            ("Power", "unknown" if power is None else "on" if power else "off"),
            ("App", self.app or "none"),
            ("Remote", "available" if self._feature(FeatureName.Select) else "no"),
            ("Volume", volume),
            ("Pairing", None),
        ]
        for service in self.conf.services:
            if service.protocol == Protocol.RAOP:  # the audio side of AirPlay
                continue
            state = "paired" if service.credentials else "not paired"
            if service.requires_password:
                state += ", password protected"
            rows.append((service.protocol.name, state))
        rows += [
            ("This session", None),
            ("Connected", f"for {clock(time.monotonic() - stats.connected_at)}"),
            ("Buttons sent", f"{stats.sent}" + (f" ({stats.failed} failed)" if stats.failed else "")),
            *self._stream_rows(),
        ]
        return rows

    def _nerd_rows(self):
        """Rows of the hidden /nerd sheet: the same, for people who like numbers."""
        device, stats, atv = self.atv.device_info, self.stats, self.atv
        features = atv.features.all_features(include_unsupported=True).values()
        available = sum(f.state == FeatureState.Available for f in features)
        cols, lines = shutil.get_terminal_size()
        updated = stats.updated_at
        rows = [
            ("Software", None),
            ("sofatui", __version__),
            ("pyatv", f"{package_version('pyatv')} (patched at startup)"),
            ("python", f"{platform.python_version()} on {platform.system()}"),
            ("pid", str(os.getpid())),
            ("Device", None),
            ("model id", device.raw_model or "?"),
            ("build", device.build_number or "?"),
            ("identifier", str(self.conf.identifier)),
            ("output device", device.output_device_id or "?"),
        ]
        for service in self.conf.services:
            properties = service.properties
            rows.append((service.protocol.name.lower(), f"port {service.port}, pairing {service.pairing.name}"))
            for key in ("srcvers", "features", "ft", "flags", "sf", "protovers", "rpFl"):
                if key in properties:
                    rows.append((f"  {key}", str(properties[key])))
        rows += [
            ("Connection", None),
            ("remote via", getattr(atv.remote_control.main_protocol, "name", "none")),
            ("metadata via", getattr(atv.metadata.main_protocol, "name", "none")),
            ("audio via", getattr(atv.audio.main_protocol, "name", "none")),
            ("power via", getattr(atv.power.main_protocol, "name", "none")),
            ("features", f"{available} of {len(features)} available"),
            ("uptime", f"{time.monotonic() - stats.connected_at:.0f} s"),
            ("Events", None),
            ("push updates", f"{stats.updates}"
             + (f", last {time.monotonic() - updated:.0f} s ago" if updated else "")),
            ("buttons", f"{stats.sent} sent, {stats.failed} failed"),
            ("latency", f"last {stats.latency * 1000:.0f} ms, mean "
             f"{stats.latency_total / stats.sent * 1000:.0f} ms" if stats.sent else "no button yet"),
            ("Rendering", None),
            ("terminal", f"{cols}x{lines}, {stats.layout} layout"),
            ("frames", f"{stats.frames} drawn, {stats.written / 1024:.0f} kB written"),
            ("click zones", str(len(self.hitboxes))),
            ("Stream", None),
        ]
        info = self.stream_info
        if info:
            rows += [
                ("source", info["file"]),
                ("phase", info.get("phase", "?")),
                ("route", info.get("via", "local file")),
                ("pid", str(info["pid"])),
                ("log", str(background.log_file(self.address))),
            ]
            if "started" in info:
                rows.append(("uptime", f"{time.time() - info['started']:.0f} s"))
        else:
            rows += [
                ("state", "none"),
                ("state file", str(background.state_file(self.address))),
            ]
        return rows

    def _sheet(self, width, height, title, rows):
        """A card listing label/value rows; scrolls when it does not fit."""
        r = RESET
        card = Card(width)
        name = gradient(clip(self.name, width - 10))
        card.row(f"{ACCENT}◆{r} {BOLD}{name}{r}", right=self._badge())
        card.rule(title)

        # Wrap long values onto following rows, so nothing is cut off
        labels = min(16, max(len(label) for label, value in rows if value is not None) + 2)
        space = width - labels
        rows = [
            (label if start == 0 else "", value and value[start : start + space])
            for label, value in rows
            for start in range(0, max(len(value or ""), 1), space)
        ]

        room = max(1, height - 6)  # borders, name, two rules and the status row
        headings = [i for i, (_, value) in enumerate(rows) if value is None and i]
        if len(rows) + len(headings) <= room:  # space out the sections if they fit
            for i in reversed(headings):
                rows.insert(i, ("", ""))
        self.scroll = max(0, min(self.scroll, len(rows) - room))
        for label, value in rows[self.scroll : self.scroll + room]:
            if value is None:
                card.row(f"{ACCENT}{BOLD}{label}{r}")
            else:
                card.row(f"{MUTED}{label.ljust(labels)}{r}{TEXT}{value}{r}")

        hint = "esc: back to the remote"
        if len(rows) > room:
            last = min(self.scroll + room, len(rows))
            hint += f" · ↑↓ {last}/{len(rows)}"
        card.rule(hint)
        self._status_row(card)
        return card.close()

    def _tiny_sheet(self, cols, height, rows):
        """A sheet as bare lines, for terminals too small for a card."""
        lines = [
            f"{ACCENT}{BOLD}{clip(label, cols)}{RESET}"
            if value is None
            else f"{MUTED}{label} {TEXT}{clip(value, max(1, cols - len(label) - 1))}{RESET}"
            for label, value in rows
        ]
        room = max(1, height - 1)
        self.scroll = max(0, min(self.scroll, len(lines) - room))
        hint = f"{MUTED}{clip('esc: back · ↑↓ scroll', cols)}{RESET}"
        return [*lines[self.scroll : self.scroll + room], hint][-height:]

    def frame(self, cols, rows):
        """Pick the largest layout that fits the terminal."""
        width = min(WIDTH, cols - 4)
        if self.view != "remote":
            title, sheet_rows = (
                ("info", self._info_rows())
                if self.view == "info"
                else ("stats for nerds", self._nerd_rows())
            )
            if rows >= 8 and width >= COMPACT_WIDTH:
                lines = self._sheet(width, rows, title, sheet_rows)
                return lines if self.input is None else self._overlay(lines, width)
            if self.input is None:
                return self._tiny_sheet(cols, rows, sheet_rows)

        if rows >= FULL_ROWS and width >= FULL_WIDTH:
            self.stats.layout = "full"
            lines = self._full(width, spare=rows - FULL_ROWS)
        elif rows >= COMPACT_ROWS and width >= COMPACT_WIDTH:
            self.stats.layout = "compact"
            lines = self._compact(width)
        else:
            # Keep the prompt visible when there is not even room for three lines
            self.stats.layout = "tiny"
            lines = self._tiny(cols)
            return lines[:rows] if self.input is None else lines[-rows:]
        return lines if self.input is None else self._overlay(lines, width)

    def _place_zones(self, lines, top, left):
        """Strip the zone markers and note where each zone ended up on screen."""
        self.hitboxes = []
        for y, line in enumerate(lines):
            column, start, action, shown = 0, 0, None, []
            for part in re.split(r"(\x1b\[[0-9;]*[mz])", line):
                if part.endswith("z") and part.startswith("\x1b"):
                    if part[2:-1]:
                        start, action = column, self._zones[int(part[2:-1])]
                    elif action:
                        box = (top + y + 1, left + start + 1, left + column, action)
                        self.hitboxes.append(box)
                elif part.startswith("\x1b"):
                    shown.append(part)
                else:
                    column += len(part)
                    shown.append(part)
            lines[y] = "".join(shown)

    def draw(self):
        if self.stop.is_set():  # a late flash must not draw over the restored terminal
            return
        cols, rows = shutil.get_terminal_size()
        self._zones = []
        lines = self.frame(cols, rows)
        left = max(0, (cols - max(map(vlen, lines))) // 2)
        margin = " " * left
        top = max(0, (rows - len(lines)) // 2)
        self._place_zones(lines, top, left)
        output = (
            "\x1b[H"
            + "\x1b[K\n" * top
            + "\x1b[K\n".join(margin + line for line in lines)
            + "\x1b[J"
        )
        if output != self._last_frame:
            self._last_frame = output
            sys.stdout.write(output)
            sys.stdout.flush()
            self.stats.frames += 1
            self.stats.written += len(output.encode())

    # --- main loop ---

    async def run(self):
        self.atv.listener = self
        self.atv.power.listener = self
        self.atv.push_updater.listener = self
        self.atv.push_updater.start()
        try:
            self.playstatus_update(None, await self.atv.metadata.playing())
        except Exception as ex:  # pylint: disable=broad-except
            self.status = (f"could not read state: {ex}", WARN)

        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l" + MOUSE_ON)
        loop.add_reader(fd, self.read_keys, fd)
        loop.add_signal_handler(signal.SIGINT, self.interrupt)
        tasks = [
            asyncio.ensure_future(self.worker()),
            asyncio.ensure_future(self.watch_stream()),
        ]
        try:
            while not self.stop.is_set():
                self.draw()
                try:
                    await asyncio.wait_for(self.stop.wait(), 0.1)
                except asyncio.TimeoutError:
                    pass
        finally:
            for task in tasks:
                task.cancel()
            loop.remove_reader(fd)
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write(MOUSE_OFF + "\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()


class Failure(Exception):
    """A problem to report to the user without a traceback."""


async def spinner(text, coro):
    """Show a spinner on the current line while a coroutine runs."""
    if not sys.stdout.isatty():
        return await coro
    task = asyncio.ensure_future(coro)
    for i in range(sys.maxsize):
        sys.stdout.write(f"\r{ACCENT}{'⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[i % 10]}{RESET} {text}")
        sys.stdout.flush()
        if (await asyncio.wait([task], timeout=0.08))[0]:
            break
    sys.stdout.write("\r\x1b[K")
    return task.result()


async def find(host):
    """Find the Apple TV, by address or as the only one on the network."""
    loop = asyncio.get_running_loop()
    storage = FileStorage.default_storage(loop)
    await storage.load()
    if host:
        found = await pyatv.scan(loop, hosts=[host], storage=storage)
    else:
        found = await pyatv.scan(loop, timeout=SCAN_TIMEOUT, storage=storage)
    if not found:
        raise Failure(f"No Apple TV found{f' at {host}' if host else ''}")
    if len(found) > 1:
        devices = "\n".join(f"  {conf.address}  {conf.name}" for conf in found)
        raise Failure(f"Several devices found, pass the address of one:\n{devices}")
    return found[0], storage


async def connect(conf, storage, password):
    if password:
        pyatv_patches.set_password(str(conf.address), password)
        for protocol in (Protocol.AirPlay, Protocol.RAOP):
            service = conf.get_service(protocol)
            if service:
                service.password = password
    return await pyatv.connect(conf, asyncio.get_running_loop(), storage=storage)


def needs_password(conf):
    """Whether the device announces that AirPlay is password protected."""
    service = conf.get_service(Protocol.AirPlay)
    return bool(service and service.requires_password)


def stored_password():
    if os.environ.get(PASSWORD_ENV):
        return os.environ[PASSWORD_ENV]
    for password_file in PASSWORD_FILES:
        if password_file.is_file():
            return password_file.read_text().strip()
    return None


def ask_password(name):
    if not sys.stdin.isatty():
        raise Failure(f"{name} needs the AirPlay password: use --password or ${PASSWORD_ENV}")
    print(f"{ACCENT}◆{RESET} {BOLD}{name}{RESET} asks for the AirPlay password")
    password = pwinput(f"{ACCENT}›{RESET} password: ", mask="•")
    if not password:
        raise Failure("No password entered")
    return password


def offer_to_save(password):
    path = PASSWORD_FILES[0]
    answer = input(f"{ACCENT}›{RESET} save it to {path}? [y/N] ")
    if answer.strip().lower() in ("y", "yes"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(password + "\n")


def pairing_removed(error):
    """Whether an error, or what caused it, says that the pairing is gone."""
    while error is not None:
        if isinstance(error, pyatv_patches.PairingRemoved) or (
            "no longer accepts the stored pairing" in str(error)
        ):
            return True
        error = error.__cause__ or error.__context__
    return False


async def login(conf, storage, password):
    """Connect, asking for the AirPlay password when it is needed or was wrong.

    Returns the connection and the password that worked.
    """
    asked = False
    for _ in range(PASSWORD_ATTEMPTS):
        if not password and needs_password(conf):
            password, asked = ask_password(conf.name), True
        try:
            atv = await spinner(
                f"Connecting to {conf.name}…", connect(conf, storage, password)
            )
        except Exception as ex:  # pylint: disable=broad-except
            if pairing_removed(ex):
                raise Failure(
                    f"{conf.name} no longer accepts the stored pairing; it may have "
                    "been removed on the Apple TV.\nPair again with: sofatui pair"
                ) from ex
            # A refused password surfaces as (or wrapped around) an authentication error
            refused = isinstance(ex, exceptions.AuthenticationError) or isinstance(
                ex.__cause__, exceptions.AuthenticationError
            )
            if not (refused and needs_password(conf) and password):
                raise Failure(f"Could not connect to {conf.name}: {ex}") from ex
            print(f"{BAD}✗{RESET} The AirPlay password was not accepted")
            password = None
        else:
            if asked:
                offer_to_save(password)
            return atv, password
    raise Failure(f"Giving up after {PASSWORD_ATTEMPTS} wrong passwords")


def parse_args():
    argv = sys.argv[1:]
    command = argv[0] if argv and argv[0] in ("stream", "stop", "pair") else "remote"

    def device_options(parser):
        parser.add_argument(
            "host",
            nargs="?",
            default=os.environ.get("ATV_HOST"),
            help="address of the Apple TV (default: $ATV_HOST, else scan the network)",
        )
        parser.add_argument(
            "--password",
            help=f"AirPlay password (default: ${PASSWORD_ENV}, else "
            f"{PASSWORD_FILES[0]} or ./{PASSWORD_FILES[1]}, else ask)",
        )

    if command == "stream":
        parser = argparse.ArgumentParser(
            prog="sofatui stream",
            description="Play a local file, or a web page's video through yt-dlp, on "
            "the Apple TV, until playback ends.",
        )
        parser.add_argument("source", help="media file, or URL of a page with a video")
        device_options(parser)
        parser.add_argument(
            "--via",
            choices=list(VIA_MODES),
            default="auto",
            help="how a web video reaches the Apple TV: "
            + "; ".join(f"{mode}: {text}" for mode, text in VIA_MODES.items()),
        )
        parser.add_argument(
            "-d",
            "--detach",
            action="store_true",
            help="keep streaming in the background and return",
        )
        args = parser.parse_args(argv[1:])
    elif command == "pair":
        parser = argparse.ArgumentParser(
            prog="sofatui pair",
            description="Pair with the Apple TV: AirPlay for the remote and streaming, "
            "Companion for turning it off the way the physical remote does.",
        )
        device_options(parser)
        parser.add_argument(
            "--again", action="store_true", help="pair even if already paired"
        )
        args = parser.parse_args(argv[1:])
    elif command == "stop":
        parser = argparse.ArgumentParser(
            prog="sofatui stop", description="Stop streams running in the background."
        )
        parser.add_argument("host", nargs="?", help="only the stream to this address")
        args = parser.parse_args(argv[1:])
    else:
        parser = argparse.ArgumentParser(
            prog="sofatui",
            description="Terminal remote control for the Apple TV. Keys and mouse "
            'clicks press remote buttons; "/" opens a command prompt (try /stream '
            "<file>).",
            epilog='Other commands: "sofatui pair [host]" pairs with the Apple TV, '
            '"sofatui stream <file> [host]" plays a file without the remote, '
            '"sofatui stop [host]" stops background streams.',
        )
        device_options(parser)
        parser.add_argument(
            "--version", action="version", version=f"sofatui {__version__}"
        )
        args = parser.parse_args(argv)
    args.command = command
    return args


async def run(args):
    logging.basicConfig(level=logging.CRITICAL)  # keep pyatv logs off the screen
    pyatv_patches.apply()

    conf, storage = await spinner(
        f"Looking for {args.host or 'an Apple TV'}…", find(args.host)
    )
    atv, password = await login_paired(conf, storage, args)
    remote = Remote(conf, password, atv)
    try:
        await remote.run()
    finally:
        await asyncio.gather(*atv.close(), return_exceptions=True)
    print(remote.exit_message or f"Disconnected from {conf.name}.")
    if remote.streaming:
        print(f"{remote.streaming} keeps streaming; stop it with: sofatui stop")


async def run_stream(args):
    """Play a file or page and hold the stream until it ends or we are told to stop."""
    logging.basicConfig(level=logging.CRITICAL)
    pyatv_patches.apply()
    url = args.source if ytdlp.is_url(args.source) else None
    path = None if url else Path(args.source).expanduser()
    try:
        if url:
            ytdlp.command()  # fail before connecting if yt-dlp is missing
        elif not path.is_file():
            raise Failure(f"No such file: {args.source}")
    except ytdlp.YtDlpError as ex:
        raise Failure(str(ex)) from ex
    source = url or str(path.resolve())

    conf, storage = await spinner(
        f"Looking for {args.host or 'an Apple TV'}…", find(args.host)
    )
    address = str(conf.address)
    atv, password = await login_paired(conf, storage, args)

    if args.detach:  # the password is known to work now; hand over to a new process
        await asyncio.gather(*atv.close(), return_exceptions=True)
        options = ["--via", args.via]
        try:
            await background.start(source, address, password, options)
        except background.StreamError as ex:
            raise Failure(f"Stream failed: {ex}") from ex
        print(f"Streaming to {conf.name} in the background; stop it with: sofatui stop")
        return

    task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
    info = {
        "pid": os.getpid(),
        "file": source,
        "title": url or path.name,
        "name": conf.name,
        "started": time.time(),
    }
    lock = relay = None
    via = None

    def phase(text):
        """Report progress to the log and to remotes watching this stream."""
        if info.get("phase") != text:
            info["phase"] = text
            background.update(lock, info)
            print(text.capitalize(), flush=True)

    try:
        await asyncio.to_thread(background.stop, address)  # one stream per device
        lock = background.claim(address, info)
        target = source
        if url:
            phase("looking up the video")
            media = await ytdlp.resolve(url)
            info["title"] = media.title
            via = args.via
            if media.kind == "download":
                via = "download"  # nothing the Apple TV could play as it is
            elif via == "auto":
                via = "relay" if ytdlp.needs_relay(media.headers) else "direct"
            info["via"] = via

            if via == "download":
                phase("downloading")
                downloaded = await ytdlp.download(url, lambda p: phase(f"downloading {p}"))
                target = str(downloaded)
            elif via == "relay":
                local = get_local_address_reaching(conf.address)
                relay = Relay(str(local), media.headers)
                await relay.start()
                target = relay.url_for(media.url)
            else:
                pyatv_patches.set_fetch_headers(media.headers)
                target = media.url

        phase("starting playback")
        pyatv_patches.set_playing_listener(lambda: phase("playing"))
        print(f"Streaming {info['title']} to {conf.name}", flush=True)
        await atv.stream.play_url(target)
        print("Playback ended", flush=True)
    except asyncio.CancelledError:
        print("Stopped", flush=True)
    except Exception as ex:  # pylint: disable=broad-except
        message = str(ex)
        if via in ("direct", "relay") and isinstance(ex, exceptions.PlaybackError):
            # The Apple TV got a playlist or file it could not load the video from
            other = "relay" if via == "direct" else "download"
            message = f"the Apple TV could not load the video, try --via {other}"
        print(f"Stream failed: {message}", flush=True)
        raise SystemExit(1) from ex
    finally:
        await asyncio.gather(*atv.close(), return_exceptions=True)
        if relay:
            await relay.close()
        if lock:
            background.release(address, lock)


async def run_pair(args):
    """Pair the protocols sofatui uses and store the credentials for pyatv."""
    logging.basicConfig(level=logging.CRITICAL)
    pyatv_patches.apply()
    conf, storage = await spinner(
        f"Looking for {args.host or 'an Apple TV'}…", find(args.host)
    )
    print(f"{ACCENT}◆{RESET} {BOLD}{conf.name}{RESET}")
    await pair_protocols(conf, storage, args, again=args.again, report=True)


async def pair_protocols(conf, storage, args, *, again=False, verify=True, report=False):
    """Pair what is not paired, or no longer accepted by the device.

    With verify, the device is asked whether it still accepts a stored pairing. With
    report, protocols that need nothing are listed too.
    """
    for protocol in (Protocol.AirPlay, Protocol.Companion):
        service = conf.get_service(protocol)
        name = protocol.name
        if service is None:
            if report:
                print(f"  {MUTED}{name}: not offered by this device{RESET}")
        elif (
            service.credentials
            and not again
            and not (verify and not await pyatv_patches.pairing_accepted(conf, protocol))
        ):
            if report:
                print(f"  {GOOD}✓{RESET} {name}: already paired")
        elif service.pairing in (PairingRequirement.Unsupported, PairingRequirement.Disabled):
            print(f"  {WARN}!{RESET} {name}: the device does not allow pairing (see its AirPlay access setting)")
        else:
            if service.credentials and not again:
                print(f"  {WARN}!{RESET} {name}: the pairing was removed on the device")
            await pair_protocol(conf, storage, protocol, args)
    await storage.save()


async def login_paired(conf, storage, args):
    """Connect, pairing first if that was never done or has been removed."""
    interactive = sys.stdin.isatty()
    password = args.password or stored_password()
    airplay = conf.get_service(Protocol.AirPlay)

    if airplay and not airplay.credentials:  # without it there is no remote control
        if not interactive:
            raise Failure(f"{conf.name} is not paired yet; run: sofatui pair")
        print(f"{ACCENT}◆{RESET} {BOLD}{conf.name}{RESET} is not paired yet")
        await pair_protocols(conf, storage, args, verify=False)
        if not airplay.credentials:
            raise Failure(f"{conf.name} is not paired; run sofatui pair when ready")

    try:
        return await login(conf, storage, password)
    except Failure as ex:
        if not (interactive and pairing_removed(ex)):
            raise
    print(f"{ACCENT}◆{RESET} {BOLD}{conf.name}{RESET} no longer accepts the stored pairing")
    await pair_protocols(conf, storage, args)
    return await login(conf, storage, password)


async def pair_protocol(conf, storage, protocol, args):
    name = protocol.name
    service = conf.get_service(protocol)
    stored = getattr((await storage.get_settings(conf)).protocols, name.lower())

    # When pairing again, the old credentials have to go first: pyatv would use them
    # for the pairing connection, which the device then drops (seen with Companion)
    previous = service.credentials
    service.credentials = stored.credentials = None

    pairing = await pyatv.pair(
        conf, protocol, asyncio.get_running_loop(), storage=storage, name="sofatui"
    )
    paired = False
    try:
        await pairing.begin()
        if needs_password(conf):
            # A password protected device shows no PIN: its AirPlay password is the
            # PIN, for AirPlay and Companion alike
            pin = args.password or stored_password() or ask_password(conf.name)
        elif pairing.device_provides_pin:
            pin = input(
                f"  {ACCENT}›{RESET} {name}: PIN shown on the TV (empty to skip): "
            ).strip()
            if not pin:
                print(f"  {MUTED}{name}: skipped{RESET}")
                return
        else:
            pin = "1111"
            input(f"  {ACCENT}›{RESET} {name}: enter {pin} on the device, then press Enter ")
        pairing.pin(pin)
        await pairing.finish()
        paired = pairing.has_paired
        if paired:
            print(f"  {GOOD}✓{RESET} {name}: paired")
        else:
            print(f"  {BAD}✗{RESET} {name}: the device did not accept the pairing")
    except Exception as ex:  # pylint: disable=broad-except
        reason = str(ex) or type(ex).__name__
        print(f"  {BAD}✗{RESET} {name}: pairing failed: {reason}")
    finally:
        await pairing.close()
        if not paired and previous:  # keep what worked before
            service.credentials = stored.credentials = previous


def run_stop(args):
    streams = background.all_running()
    if args.host:
        streams = {a: info for a, info in streams.items() if a == args.host}
    if not streams:
        print("No stream is running")
    for address, info in streams.items():
        background.stop(address)
        print(f"Stopped {background.title(info)} on {info.get('name', address)}")


def main():
    args = parse_args()
    if args.command == "stop":
        run_stop(args)
        return
    if args.command == "remote" and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit("sofatui needs an interactive terminal")
    try:
        runner = {"stream": run_stream, "pair": run_pair}.get(args.command, run)
        asyncio.run(runner(args))
    except KeyboardInterrupt:
        pass
    except Failure as ex:
        raise SystemExit(str(ex)) from None


if __name__ == "__main__":
    main()
