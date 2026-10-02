"""Find something the Apple TV can play behind a web page, using yt-dlp.

yt-dlp is optional: the "yt-dlp" command is used if it is on the PATH (so its own
configuration, cookies and updates apply), otherwise the module installed with the
"yt" extra. There are three outcomes for a page (see relay.py for sites that need more):

* an HLS playlist, which the Apple TV plays adaptively. It asks the sender for the
  playlist, so the request headers yt-dlp reports can be used (see pyatv_patches).
* one file with video and audio, which the Apple TV fetches by itself.
* neither, because video and audio only come separately. The video is then downloaded
  and merged into one file first, which needs ffmpeg.
"""

import asyncio
from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Callable, Dict, List, Optional

# H.264 with AAC in MP4 plays on every Apple TV; fall back to whatever is best
DOWNLOAD_FORMAT = "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]/b"
MIN_DIRECT_HEIGHT = 720  # below this a download is preferred if it gives more
PLAYABLE_EXTENSIONS = {"mp4", "m4v", "mov"}
# Request headers that a site checks, and that the Apple TV would not send by itself
SITE_HEADERS = {"x-forwarded-for", "referer", "cookie", "authorization", "origin"}


class YtDlpError(Exception):
    """yt-dlp is missing or could not handle the page."""


@dataclass
class Media:
    """What yt-dlp found for a page."""

    title: str
    kind: str  # "hls", "file" or "download"
    url: Optional[str] = None  # what to hand to the Apple TV, unless "download"
    headers: Dict[str, str] = field(default_factory=dict)


def needs_relay(headers: Dict[str, str]) -> bool:
    """Whether a site expects headers that only a relay can add to every request."""
    return any(name.lower() in SITE_HEADERS for name in headers)


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def command() -> List[str]:
    """The yt-dlp command line to run."""
    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    raise YtDlpError('yt-dlp not found: install it, or run "sofatui[yt]"')


def cache_dir() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "sofatui"


async def _run(*args: str, on_line: Optional[Callable[[str], None]] = None) -> str:
    """Run yt-dlp and return its standard output; errors become YtDlpError."""
    process = await asyncio.create_subprocess_exec(
        *command(),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read_errors() -> str:
        return (await process.stderr.read()).decode(errors="replace")

    errors = asyncio.ensure_future(read_errors())
    try:
        output = []
        async for raw in process.stdout:
            line = raw.decode(errors="replace").rstrip()
            output.append(line)
            if on_line:
                on_line(line)
        if await process.wait() != 0:
            lines = [x for x in (await errors).splitlines() if x.startswith("ERROR")]
            raise YtDlpError(lines[-1][len("ERROR: ") :] if lines else "yt-dlp failed")
        return "\n".join(output)
    except asyncio.CancelledError:
        process.kill()
        raise


def _has(codec: Optional[str]) -> bool:
    return codec not in (None, "none")


def choose(info: dict) -> Media:
    """Pick the best way to play what yt-dlp reports for a page."""
    title = info.get("title") or info.get("id") or "video"
    formats = info.get("formats") or [info]

    hls = [
        f
        for f in formats
        if "m3u8" in (f.get("protocol") or "") and f.get("manifest_url")
    ]
    if hls:
        # yt-dlp lists formats worst to best, which also ranks the original sound
        # above audio description and other alternative versions
        best = hls[-1]
        return Media(title, "hls", best["manifest_url"], best.get("http_headers") or {})

    def single_file(f: dict) -> bool:
        codecs_known = f.get("vcodec") is not None or f.get("acodec") is not None
        return (
            (f.get("protocol") or "http").startswith("http")
            and f.get("ext") in PLAYABLE_EXTENSIONS
            and f.get("url")
            and (_has(f.get("vcodec")) and _has(f.get("acodec")) or not codecs_known)
        )

    files = [f for f in formats if single_file(f)]
    best_height = max((f.get("height") or 0 for f in formats if _has(f.get("vcodec"))), default=0)
    if files:
        best = max(files, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        height = best.get("height") or 0
        if height >= min(MIN_DIRECT_HEIGHT, best_height) or not best_height:
            return Media(title, "file", best["url"], best.get("http_headers") or {})

    return Media(title, "download")


async def resolve(url: str) -> Media:
    """Ask yt-dlp what a page offers."""
    output = await _run("--no-warnings", "--no-playlist", "--dump-single-json", url)
    try:
        return choose(json.loads(output))
    except ValueError as ex:
        raise YtDlpError("yt-dlp returned no usable description") from ex


async def download(url: str, progress: Callable[[str], None]) -> Path:
    """Download a page's video as one MP4 into the cache and return its path."""
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = None

    def on_line(line: str) -> None:
        nonlocal path
        percent = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if percent:
            progress(f"{float(percent[1]):.0f}%")
        elif line.startswith(str(directory)):
            path = Path(line)

    await _run(
        "--no-warnings",
        "--no-playlist",
        "--format",
        DOWNLOAD_FORMAT,
        "--merge-output-format",
        "mp4",
        "--output",
        str(directory / "%(title).80B [%(id)s].%(ext)s"),
        "--print",
        "after_move:filepath",
        "--progress",
        "--newline",
        url,
        on_line=on_line,
    )
    if path is None or not path.is_file():
        raise YtDlpError("yt-dlp did not report the downloaded file")
    return path
