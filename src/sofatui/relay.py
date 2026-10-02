"""Relay a web video through this machine to the Apple TV.

Some sites only hand out their video to requests that carry certain headers: a
referrer, cookies, or the address yt-dlp sends to get around a region check. The Apple
TV fetches playlists and segments by itself and sends none of them, so it gets nothing.

The relay is a small web server the Apple TV is pointed at instead. Every request is
fetched upstream with the headers yt-dlp reported, and HLS playlists are rewritten so
that everything they refer to comes through the relay as well.
"""

import base64
import re
import secrets
from typing import Dict, Optional
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from aiohttp import web
from pyatv.support.net import unused_port

CHUNK_SIZE = 64 * 1024
PASSED_ON = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"


class Relay:
    """Web server that fetches what the Apple TV asks for with the right headers."""

    def __init__(self, address: str, headers: Dict[str, str]) -> None:
        self._address = address  # of this machine, as the Apple TV reaches it
        self._port = 0
        # The path starts with a secret, so the relay cannot be used by others
        self._secret = secrets.token_urlsafe(12)
        self._headers = {
            key: value
            for key, value in headers.items()
            if key.lower() not in ("accept-encoding", "host", "range")
        }
        self._session: Optional[aiohttp.ClientSession] = None
        self._runner: Optional[web.AppRunner] = None
        self.requests = 0
        self.failures = 0

    async def start(self) -> None:
        # Send exactly the headers yt-dlp reported: some sites refuse requests that
        # carry the Accept-Encoding header aiohttp would add (seen with Dailymotion)
        self._session = aiohttp.ClientSession(skip_auto_headers=["Accept-Encoding"])
        app = web.Application()
        app.router.add_route("*", f"/{self._secret}/{{token}}/{{name}}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._port = unused_port()
        await web.TCPSite(self._runner, self._address, self._port).start()

    async def close(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        if self._session:
            await self._session.close()

    def url_for(self, url: str) -> str:
        """The address on the relay that stands for an upstream URL."""
        token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        # Keeping the file name lets the player tell a playlist from a segment
        name = quote(urlsplit(url).path.rsplit("/", 1)[-1] or "media", safe=".")
        return f"http://{self._address}:{self._port}/{self._secret}/{token}/{name}"

    def _rewrite(self, playlist: str, base: str) -> str:
        """Point every URL of an HLS playlist at the relay."""

        def relayed(reference: str) -> str:
            url = urljoin(base, reference)
            return self.url_for(url) if url.startswith(("http://", "https://")) else reference

        lines = []
        for line in playlist.splitlines():
            if line.startswith("#"):
                line = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{relayed(m[1])}"', line)
            elif line.strip():
                line = relayed(line.strip())
            lines.append(line)
        return "\n".join(lines) + "\n"

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        try:
            url = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        except ValueError:
            return web.Response(status=400)

        headers = dict(self._headers)
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        self.requests += 1
        try:
            async with self._session.get(url, headers=headers) as upstream:
                if upstream.status >= 400:
                    self.failures += 1
                content_type = upstream.headers.get("Content-Type", "")
                is_playlist = "mpegurl" in content_type.lower() or urlsplit(
                    url
                ).path.endswith((".m3u8", ".m3u"))
                if is_playlist and upstream.status < 400:
                    text = await upstream.text(errors="replace")
                    return web.Response(
                        text=self._rewrite(text, str(upstream.url)),
                        content_type=PLAYLIST_TYPE,
                    )

                response = web.StreamResponse(status=upstream.status)
                # A compressed body is unpacked on the way, so its length is unknown
                unpacked = "Content-Encoding" in upstream.headers
                for name in PASSED_ON:
                    if name in upstream.headers and not (unpacked and "Length" in name):
                        response.headers[name] = upstream.headers[name]
                await response.prepare(request)
                if request.method != "HEAD":
                    async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                        await response.write(chunk)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, ConnectionError, TimeoutError):
            # Upstream trouble, or the Apple TV dropped the request (it does on seeks)
            self.failures += 1
            return web.Response(status=502)
