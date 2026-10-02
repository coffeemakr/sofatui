# sofatui

A terminal remote control for the Apple TV that can also stream local files and web
videos to it.

```
╭────────────────────────────────────────────────╮
│ ◆ Sofa TV                                 ● ON │
│   Apple TV 4K (gen 3) · tvOS 27.0 build 24J361 │
├────────────────────────────────────────────────┤
│ ▶ PLAYING                              AirPlay │
│ movie.mp4                                      │
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
│ › stream: playing                  ⇡ movie.mp4 │
╰────────────────────────────────────────────────╯
```

## Usage

With [uv](https://docs.astral.sh/uv/), nothing to install:

```sh
uvx sofatui                    # find the Apple TV and open the remote
uvx sofatui 192.168.1.100      # or give its address
uvx sofatui stream movie.mp4   # play a file without the remote (-d: in the background)
uvx sofatui stop               # stop a background stream
```

To keep it as a command: `uv tool install sofatui` or `pipx install sofatui`. Linux and
macOS.

The first start pairs with the Apple TV: enter the PINs it shows. If the Apple TV has
an AirPlay password, that password is asked for instead. `sofatui pair` does the same
on its own.

## Keys

| Key         | Action                      |
|-------------|-----------------------------|
| Arrow keys  | Move                        |
| Enter       | OK                          |
| Esc         | Back                        |
| `h`         | Home                        |
| Space       | Play / pause                |
| `[` `]`     | Skip back / forward         |
| `O` `H` `B` | Long press OK / Home / Back |
| `+` `-` `m` | Volume up / down, mute      |
| `P`         | Power on / off              |
| `/`         | Command prompt              |
| `q`         | Quit                        |

The buttons can be clicked too; turning off by mouse takes a click on POWER held for a
second. Volume and mute need the Apple TV to control your TV's volume over HDMI-CEC.

## Commands

Press `/`, then Tab to complete commands and file names.

| Command                 | Does                                             |
|-------------------------|--------------------------------------------------|
| `/stream <file or URL>` | Play a local file or the video of a web page     |
| `/stop`                 | Stop the stream                                  |
| `/info`                 | Show details about the Apple TV and this session |
| `/help`                 | List the commands                                |
| `/quit`                 | Leave sofatui                                    |

A stream runs in the background and keeps playing after you quit; `/stop` or
`sofatui stop` ends it.

Web pages are played through [yt-dlp](https://github.com/yt-dlp/yt-dlp): the `yt-dlp`
command if you have it, otherwise install with `uvx --from "sofatui[yt]" sofatui`. If a
site does not load, route it through your machine with `/stream --via relay <URL>`, or
fetch it first with `--via download`.

## Notes

- sofatui patches [pyatv](https://pyatv.dev) 0.18.0 at startup for things it does not
  handle yet on current tvOS (`src/sofatui/pyatv_patches.py`), so that version is pinned.
- Tested on one device: an Apple TV 4K (3rd generation) on tvOS 27, with H.264/AAC
  `.mp4` files.
- To release: `uv version --bump patch`, commit, and publish a GitHub release tagged
  `v<version>`.

## Thanks

sofatui stands on the shoulders of [pyatv](https://pyatv.dev) by
[@postlund](https://github.com/postlund) and its contributors. Discovery, pairing,
the remote control protocol and AirPlay are all pyatv; sofatui is only the couch-side
interface on top. If you want to talk to an Apple TV from Python, start there.

## License

MIT, see [LICENSE](LICENSE).
