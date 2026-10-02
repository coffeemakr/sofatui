"""Interactive terminal remote for the Apple TV, built on pyatv.

Keys act as remote buttons, and the buttons can be clicked with the mouse. Typing
"/" opens a command prompt with tab completion, e.g. "/stream <file>" plays a local
file on the Apple TV.
"""

import argparse
import asyncio
import logging
import os
from pathlib import Path
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
from pyatv.const import DeviceState, FeatureName, FeatureState, PowerState, Protocol
from pyatv.interface import DeviceListener, PowerListener, PushListener
from pyatv.storage.file_storage import FileStorage

from sofatui import __version__, background, pyatv_patches
from sofatui.background import PASSWORD_ENV

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
    # Volume: hotkeys only, usable while the Apple TV reports that it has the controls
    "volume down": ("VOL -", FeatureName.VolumeDown, lambda r: r.atv.audio.volume_down()),
    "mute": ("MUTE", FeatureName.VolumeUp, lambda r: pyatv_patches.press_mute(r.atv)),
    "volume up": ("VOL +", FeatureName.VolumeUp, lambda r: r.atv.audio.volume_up()),
}

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
    "stream": ("<file>", "play a local file"),
    "stop": ("", "stop the stream"),
    "help": ("", "list commands"),
    "quit": ("", "leave the remote"),
}
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mp3", ".m4a", ".aac", ".wav"}
MAX_SUGGESTIONS = 6  # completion rows shown above the command prompt
CURSOR = "\x1b[7m \x1b[27m"

WIDTH = 46  # widest inner width of the card
# Smallest terminal rows / card width for the boxed and the compact layout
FULL_ROWS, FULL_WIDTH = 25, 36
COMPACT_ROWS, COMPACT_WIDTH = 11, 30
FLASH_TIME = 0.18  # seconds a pressed button stays lit


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
        self.pressed = {}
        self.status = ("ready", MUTED)
        self.exit_message = None
        self.stop = asyncio.Event()
        self.queue = asyncio.Queue(maxsize=8)
        self.input = None  # text typed after "/", None when not in command mode
        self.cycle = None  # (candidates, index) while stepping through completions
        self.streaming = None  # name of the file streaming in the background
        self._tasks = set()  # running command tasks
        self._processes = []  # background streams started here, to reap them
        self._candidates = (None, [], "")  # input, completions, heading
        self._zones = []  # click actions of the frame being built
        self.hitboxes = []  # (row, first column, last column, action) on screen
        self._last_frame = None

    # --- pyatv listeners ---

    def playstatus_update(self, updater, playstatus):
        self.playing = playstatus
        self.playing_at = time.monotonic()
        self.app = self._app_name()

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
            await self.atv.power.turn_off()
        else:
            await self.atv.power.turn_on()

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
                if mouse[4] == "M":  # press, not release
                    self.mouse(int(mouse[1]), int(mouse[2]), int(mouse[3]))
            elif self.input is not None:
                self.input_key(key)
            elif key == "/":
                self.input = ""
            elif key in ("q", "Q", "\x03", "\x04"):
                self.stop.set()
            elif key in KEYS:
                self.press(KEYS[key])
        self.draw()

    def interrupt(self):
        """Ctrl+C leaves the command prompt first, then the remote."""
        if self.input is None:
            self.stop.set()
        else:
            self.input = self.cycle = None
            self.draw()

    def mouse(self, button, column, row):
        if button in (64, 65):  # wheel steps through completions
            if self.input is not None:
                self.complete(1 if button == 65 else -1)
        elif button == 0:  # left click
            for line, first, last, (kind, target) in self.hitboxes:
                if line == row and first <= column <= last:
                    if kind == "button":
                        self.press(target)
                    elif kind == "prompt":
                        self.input = ""
                    else:
                        self.pick(target)
                    break

    def pick(self, item):
        """Click on a completion: fill it in, or run it if nothing is left to add."""
        complete = not item.value.endswith(("/", " "))
        if complete and (item.final or item.value == self.input):
            self.input = self.cycle = None
            self.run_command(item.value)
        else:
            self.input, self.cycle = item.value, None

    def press(self, button):
        if not self.available(button):
            self.status = (f"{button} is not available right now", WARN)
            return
        self.pressed[button] = time.monotonic()
        try:
            self.queue.put_nowait(button)
        except asyncio.QueueFull:
            pass

    async def worker(self):
        while True:
            button = await self.queue.get()
            try:
                await BUTTONS[button][2](self)
                self.status = (f"sent {button}", ACCENT)
            except Exception as ex:  # pylint: disable=broad-except
                self.status = (f"{button} failed: {ex}", BAD)

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
        if name not in COMMANDS:
            self.status = (f"unknown command /{name} (try /help)", WARN)
            return
        getattr(self, f"cmd_{name}")(arg.strip())

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cmd_stream(self, arg):
        path = Path(os.path.expanduser(arg))
        if not arg:
            self.status = ("usage: /stream <file>", WARN)
        elif not path.is_file():
            self.status = (f"no such file: {arg}", BAD)
        else:
            self._spawn(self._start_stream(path))

    def cmd_stop(self, arg):
        self._spawn(self._stop_stream())

    def cmd_help(self, arg):
        names = "  ".join(f"/{name} {usage}".strip() for name, (usage, _) in COMMANDS.items())
        self.status = (names, TEXT)

    def cmd_quit(self, arg):
        self.stop.set()

    async def _start_stream(self, path):
        """Play a file from a detached process, so it survives quitting the remote."""
        self.status = (f"starting stream of {path.name}…", WARN)
        try:
            process = await background.start(path, self.address, self.password)
        except background.StreamError as ex:
            self.status = (f"stream failed: {ex}", BAD)
        else:
            self._processes.append(process)
            self.streaming = path.name
            self.status = ("stream runs in the background", GOOD)

    async def _stop_stream(self):
        info = await asyncio.to_thread(background.stop, self.address)
        self.streaming = None
        if info:
            self.status = (f"stopped stream of {Path(info['file']).name}", MUTED)
        else:
            self.status = ("no stream is running", MUTED)

    async def watch_stream(self):
        """Follow the background stream, which may start or end outside this remote."""
        while True:
            info = background.running(self.address)
            name = Path(info["file"]).name if info else None
            if self.streaming and not name:
                self.status = (f"stream of {self.streaming} ended", MUTED)
            self.streaming = name
            self._processes = [p for p in self._processes if p.poll() is None]
            await asyncio.sleep(1)

    # --- rendering ---

    def zone(self, kind, target, text):
        """Make text clickable; draw() turns the markers into screen hitboxes."""
        self._zones.append((kind, target))
        return f"\x1b[{len(self._zones) - 1}z{text}\x1b[z"

    def _cell(self, button, width):
        label = BUTTONS[button][0].center(width)
        if time.monotonic() - self.pressed.get(button, 0) < FLASH_TIME:
            text = f"{FLASH}{label}{RESET}"
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
            title=playing.title if playing else None,
            placeholder="Nothing playing" if idle else "Untitled media",
            artist=" · ".join(x for x in (playing.artist, playing.album) if x)
            if playing
            else "",
            position=position,
            total=total,
            times=f"{clock(position)} / {clock(total)}" if total else "",
        )

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
            (("/", "command"), ("P", "power"), ("q", "quit")),
        ]
        if self.available("volume up"):
            legend.insert(2, (("+ -", "volume"), ("m", "mute")))
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

    def frame(self, cols, rows):
        """Pick the largest layout that fits the terminal."""
        width = min(WIDTH, cols - 4)
        if rows >= FULL_ROWS and width >= FULL_WIDTH:
            lines = self._full(width, spare=rows - FULL_ROWS)
        elif rows >= COMPACT_ROWS and width >= COMPACT_WIDTH:
            lines = self._compact(width)
        else:
            # Keep the prompt visible when there is not even room for three lines
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
    command = argv[0] if argv and argv[0] in ("stream", "stop") else "remote"

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
            description="Play a local file on the Apple TV, until playback ends.",
        )
        parser.add_argument("file", help="media file to play")
        device_options(parser)
        parser.add_argument(
            "-d",
            "--detach",
            action="store_true",
            help="keep streaming in the background and return",
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
            epilog='Other commands: "sofatui stream <file> [host]" plays a file '
            'without the remote, "sofatui stop [host]" stops background streams.',
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
    atv, password = await login(conf, storage, args.password or stored_password())
    remote = Remote(conf, password, atv)
    try:
        await remote.run()
    finally:
        await asyncio.gather(*atv.close(), return_exceptions=True)
    print(remote.exit_message or f"Disconnected from {conf.name}.")
    if remote.streaming:
        print(f"{remote.streaming} keeps streaming; stop it with: sofatui stop")


async def run_stream(args):
    """Play a file and hold the stream until playback ends or we are told to stop."""
    logging.basicConfig(level=logging.CRITICAL)
    pyatv_patches.apply()
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise Failure(f"No such file: {args.file}")

    conf, storage = await spinner(
        f"Looking for {args.host or 'an Apple TV'}…", find(args.host)
    )
    address = str(conf.address)
    atv, password = await login(conf, storage, args.password or stored_password())

    if args.detach:  # the password is known to work now; hand over to a new process
        await asyncio.gather(*atv.close(), return_exceptions=True)
        try:
            await background.start(path, address, password)
        except background.StreamError as ex:
            raise Failure(f"Stream failed: {ex}") from ex
        print(f"Streaming {path.name} to {conf.name}; stop it with: sofatui stop")
        return

    task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
    lock = None
    try:
        await asyncio.to_thread(background.stop, address)  # one stream per device
        lock = background.claim(
            address, {"pid": os.getpid(), "file": str(path.resolve()), "name": conf.name}
        )
        print(f"Streaming {path.name} to {conf.name}", flush=True)
        await atv.stream.play_url(str(path))
        print("Playback ended", flush=True)
    except asyncio.CancelledError:
        print("Stopped", flush=True)
    except Exception as ex:  # pylint: disable=broad-except
        raise Failure(f"Stream failed: {ex}") from ex
    finally:
        await asyncio.gather(*atv.close(), return_exceptions=True)
        if lock:
            background.release(address, lock)


def run_stop(args):
    streams = background.all_running()
    if args.host:
        streams = {a: info for a, info in streams.items() if a == args.host}
    if not streams:
        print("No stream is running")
    for address, info in streams.items():
        background.stop(address)
        print(f"Stopped {Path(info['file']).name} on {info.get('name', address)}")


def main():
    args = parse_args()
    if args.command == "stop":
        run_stop(args)
        return
    if args.command == "remote" and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit("sofatui needs an interactive terminal")
    try:
        asyncio.run(run_stream(args) if args.command == "stream" else run(args))
    except KeyboardInterrupt:
        pass
    except Failure as ex:
        raise SystemExit(str(ex)) from None


if __name__ == "__main__":
    main()
