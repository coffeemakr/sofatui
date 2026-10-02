# sofatui

A terminal remote control for the Apple TV. Press keys or click buttons to navigate,
and stream local video files to the TV from a `/` command prompt with tab completion.

```
╭────────────────────────────────────────────────╮
│ ◆ Sofa TV                                 ● ON │
│   Apple TV 4K (gen 3) · tvOS 27.0 build 24J361 │
├────────────────────────────────────────────────┤
│ ▶ PLAYING                              AirPlay │
│ Untitled media                                 │
│                                                │
│ ━━━━━───────────────────────────  5:36 / 38:33 │
├────────────────────────────────────────────────┤
│                   ╭───────╮                    │
│                   │   ▲   │                    │
│           ╭───────┼───────┼───────╮            │
│           │   ◀   │   OK  │   ▶   │            │
│           ╰───────┼───────┼───────╯            │
│                   │   ▼   │                    │
│                   ╰───────╯                    │
│        ╭────────╮ ╭────────╮ ╭────────╮        │
│        │  BACK  │ │  HOME  │ │  ▶ II  │        │
│        ╰────────╯ ╰────────╯ ╰────────╯        │
│        ╭────────╮ ╭────────╮ ╭────────╮        │
│        │ « SKIP │ │ POWER  │ │ SKIP » │        │
│        ╰────────╯ ╰────────╯ ╰────────╯        │
├────────────────────────────────────────────────┤
│ › sent up                                      │
╰────────────────────────────────────────────────╯
```

The layout shrinks with the terminal: boxed remote, compact card, or three status
lines.

## Run

With [uv](https://docs.astral.sh/uv/), no installation needed:

```sh
uvx sofatui                  # find the Apple TV on the network
uvx sofatui 192.168.1.100    # or give its address
```

Or install it as a command with `uv tool install sofatui` (or `pipx install sofatui`)
and run `sofatui`.

The latest development version runs straight from GitHub, or from a checkout:

```sh
uvx --from git+https://github.com/coffeemakr/sofatui sofatui
uv run sofatui [host]        # in the repository
```

Without a host, sofatui scans the network and connects if it finds exactly one device.
`$ATV_HOST` sets a default address. Linux and macOS only.

## Setup

sofatui uses [pyatv](https://pyatv.dev) and its stored credentials (`~/.pyatv.conf`).
Pair once over AirPlay, which also enables the remote control buttons:

```sh
uvx --from pyatv atvremote -s <address> --protocol airplay pair
```

If the Apple TV has an AirPlay password (Settings → AirPlay and HomeKit), enter that
password when asked for the PIN. sofatui notices that the device wants a password and
asks for it, offering to save it for next time. To skip the question, provide it in
one of these ways:

- `--password <password>`
- the `AIRPLAY_PASSWORD` environment variable
- the file `~/.config/sofatui/password`, or `.airplay-password` in the current directory

## Keys

| Key         | Action                  |
|-------------|-------------------------|
| Arrow keys  | Move                    |
| Enter       | OK / select             |
| Esc         | Back (menu)             |
| `h`         | Home                    |
| Space       | Play / pause            |
| `[` and `]` | Skip back / forward     |
| `+` and `-` | Volume up / down        |
| `m`         | Mute                    |
| `P`         | Power on / off          |
| `/`         | Open the command prompt |
| `q`, Ctrl+C | Quit                    |

The volume keys only work while the Apple TV reports that it controls
the volume of your TV or receiver. That needs HDMI-CEC (Settings → Remotes and Devices
→ Volume Control → Auto); with volume control over infrared, only the physical remote
can change the volume.

Buttons can also be clicked. Because the mouse is captured, select text with
Shift+drag.

## Commands

| Command          | Does                                                 |
|------------------|------------------------------------------------------|
| `/stream <file>` | Play a local file on the Apple TV, in the background |
| `/stop`          | Stop the background stream                           |
| `/info`          | Show details about the Apple TV and this session     |
| `/help`          | List the commands                                    |
| `/quit`          | Leave sofatui                                        |

In the prompt, Tab (or ↓) completes commands and file paths and then steps through the
matches, Shift+Tab (or ↑) steps backwards, and Esc closes it. Suggestions can be
clicked.

The Apple TV must be able to play the format; only H.264/AAC `.mp4` has been tested.

## Background streams

The Apple TV pulls the file from your machine and stops as soon as the AirPlay session
that started it closes, so a process has to stay alive while it plays. `/stream` starts
that process detached: you can quit sofatui or close the terminal and the video keeps
playing. A remote started later shows the running stream (`⇡ file`) and can `/stop` it.

The same is available without the remote:

```sh
sofatui stream film.mp4 [host]      # play and wait until it ends (Ctrl+C stops)
sofatui stream -d film.mp4 [host]   # play in the background and return
sofatui stop [host]                 # stop background streams
```

There is one stream per device; starting another replaces it. The stream ends when the
machine sleeps or shuts down.

## Releasing

Bump the version with `uv version --bump patch` (or `minor`, `major`), commit, and
publish a GitHub release tagged `v<version>`. The Publish workflow builds the package
and uploads it to PyPI; it stops if the tag and the version differ.

## Known issues

- **pyatv is patched at startup.** pyatv 0.18.0 does not yet handle AirPlay passwords
  on AirPlay 2, the way current tvOS starts URL playback, or the mute key, so sofatui
  patches these in when it starts (`src/sofatui/pyatv_patches.py`, based on
  [pyatv#2846](https://github.com/postlund/pyatv/pull/2846)). The pyatv version is
  pinned for that reason, and the patches should go away once pyatv supports this
  itself.
- **Tested on one device only:** an Apple TV 4K (3rd generation) on tvOS 27.

## Thanks

sofatui stands on the shoulders of [pyatv](https://pyatv.dev) by
[@postlund](https://github.com/postlund) and its contributors. Discovery, pairing,
the remote control protocol and AirPlay are all pyatv; sofatui is only the couch-side
interface on top. If you want to talk to an Apple TV from Python, start there.

Thanks also to [@jlacivita](https://github.com/jlacivita), whose pull request
[pyatv#2846](https://github.com/postlund/pyatv/pull/2846) showed how current tvOS
expects URL playback to be started.

## License

MIT, see [LICENSE](LICENSE).
