"""Streams that keep running in the background, detached from the terminal.

A stream has to be held by a process for as long as it plays: the Apple TV pulls the
file from this machine and stops as soon as the AirPlay session closes. That process
is "sofatui stream", started detached, so the remote can quit without ending it.

Each device has one state file, which the streaming process keeps locked while it
runs. The lock is what marks a stream as alive, so a crashed stream never leaves a
stale entry that could point at an unrelated process.
"""

import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

PASSWORD_ENV = "AIRPLAY_PASSWORD"
START_TIMEOUT = 45  # seconds for a background stream to connect
STOP_TIMEOUT = 8  # seconds to wait for a stream to end before killing it


class StreamError(Exception):
    """A background stream could not be started."""


def state_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    path = (
        Path(runtime) / "sofatui"
        if runtime
        else Path(tempfile.gettempdir()) / f"sofatui-{os.getuid()}"
    )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def state_file(address: str) -> Path:
    return state_dir() / f"stream-{address}.json"


def log_file(address: str) -> Path:
    return state_dir() / f"stream-{address}.log"


def claim(address: str, info: dict):
    """Mark this process as the stream for a device; keep the result until release."""
    handle = open(state_file(address), "a+", encoding="utf-8")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    handle.seek(0)
    handle.truncate()
    json.dump(info, handle)
    handle.flush()
    return handle


def release(address: str, handle) -> None:
    state_file(address).unlink(missing_ok=True)
    handle.close()


def running(address: str):
    """Return the info of the live stream for a device, or None."""
    path = state_file(address)
    try:
        handle = open(path, "r+", encoding="utf-8")
    except FileNotFoundError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # held by a live stream
            try:
                return json.load(handle)
            except ValueError:  # still being written
                return None
        path.unlink(missing_ok=True)  # its process died without cleaning up
        return None


def all_running() -> dict:
    """Return {address: info} for every live stream."""
    streams = {}
    for path in state_dir().glob("stream-*.json"):
        address = path.stem[len("stream-") :]
        info = running(address)
        if info:
            streams[address] = info
    return streams


def stop(address: str):
    """Stop the stream for a device and return its info, or None if there was none."""
    info = running(address)
    if not info:
        return None
    for sig, patience in ((signal.SIGTERM, STOP_TIMEOUT), (signal.SIGKILL, 2)):
        try:
            os.kill(info["pid"], sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + patience
        while time.monotonic() < deadline:
            if not running(address):
                return info
            time.sleep(0.1)
    return info


def last_log_line(address: str) -> str:
    try:
        lines = log_file(address).read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


def spawn(path: Path, address: str, password) -> subprocess.Popen:
    """Start "sofatui stream" for a file, detached from this terminal."""
    env = dict(os.environ)
    if password:
        env[PASSWORD_ENV] = password  # not on the command line, where ps shows it
    with open(log_file(address), "w", encoding="utf-8") as log:
        return subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-m", "sofatui", "stream", str(path), address],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )


async def start(path: Path, address: str, password) -> subprocess.Popen:
    """Start a background stream and wait until it is connected and playing."""
    process = spawn(path.resolve(), address, password)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        info = running(address)
        if info and info.get("pid") == process.pid:
            return process
        if process.poll() is not None:
            raise StreamError(last_log_line(address) or "the stream exited at once")
        await asyncio.sleep(0.25)
    process.terminate()
    raise StreamError("the stream did not start in time")
