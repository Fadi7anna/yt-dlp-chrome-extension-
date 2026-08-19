# yt-dlp Chrome Extension

A Chrome extension + local helper server for downloading videos with yt-dlp, with a full
download-history dashboard.

Chrome extensions can't run yt-dlp directly (no filesystem or process access), so this ships a small
local Python server that the extension talks to over `http://127.0.0.1:4599`.

## Requirements

| | |
|---|---|
| Python | 3.9+ (3.11+ recommended — yt-dlp deprecates 3.10) |
| yt-dlp | `pip install yt-dlp` |
| **ffmpeg** | **required for any best-quality download** — see below |
| A JS engine | `deno`, `bun`, or `node` — solves YouTube's JS challenges locally |
| PO-token provider | **not required** — see [YouTube notes](#youtube-notes) |

### ffmpeg is not optional

YouTube serves high-quality video and audio as *separate* streams. Combining them needs ffmpeg, so
without it every "best quality" download fails. Install it once:

```bash
winget install ffmpeg
```

The server finds ffmpeg on PATH and also probes the usual Windows install locations (winget, conda
envs, chocolatey, `C:\ffmpeg\bin`).

Not every build is equally capable, so it prefers one that has the encoders the presets need. The
conda-forge build, for example, ships **without `libmp3lame`** — with that one selected, the
"Audio only (MP3)" preset downloads the whole file and only then fails with `Encoder not found`.
Where several builds are installed, the server picks one that can actually do the job and names it
in the startup banner.

If yours lives somewhere unusual, point it there directly:

```bash
set YTDLP_FFMPEG=D:\tools\ffmpeg\bin\ffmpeg.exe
```

The startup banner tells you which ffmpeg it found. If it says `NOT FOUND`, fix that before
anything else — note that a **running** server never picks up later PATH changes, so install ffmpeg
first and start the server after.

## 1. Start the local server

```bash
python server/server.py
```

It prints what it resolved:

```
yt-dlp helper server listening on http://127.0.0.1:4599
  yt-dlp    2026.07.04  (auto-update every 6h)
  ffmpeg    ffmpeg version N-123074-g4e32fb4c2a-20260228
            C:\Users\you\...\ffmpeg-...-win64-gpl\bin
  saving to C:\Users\you\Downloads\yt-dlp-extension
  2 concurrent download slot(s)
```

Leave it running. Downloads land in `~/Downloads/yt-dlp-extension`.

## 2. Load the extension in Chrome

1. Go to `chrome://extensions`
2. Enable **Developer mode** (top right)
3. Click **Load unpacked**
4. Select the `extension/` folder

## 3. Use it

Open a page with a video, click the extension icon, pick a quality, click **Download**.

Every option in the dropdown shows what you'll actually get — resolution, codec, container, and the
**real file size** — computed by running yt-dlp's own format selection against the video before you
commit to anything:

```
Best quality — 1216p · vp9 · 1.1GB · mkv
Best quality, smaller file — 1216p · av01 · 740.5MB · mkv
Best MP4 (most compatible) — 1080p · avc1 · 623.0MB · mp4
Up to 720p — 720p · vp9 · 276.9MB · mkv
Audio only (MP3) — audio only · 140kbps · opus · 43.3MB
```

That matters on a slow connection: the top two presets are the *same resolution*, but one is a third
smaller because AV1 is a more efficient codec.

**Presets**

| Preset | What it picks |
|---|---|
| Best quality | Highest resolution, then highest bitrate — the most data available |
| Best quality, smaller file | Same resolution, but lets yt-dlp prefer AV1/VP9 — typically ~⅓ the bytes |
| Best MP4 (most compatible) | H.264 + AAC in mp4 — plays in anything, including old TVs and phones |
| Up to 2160p / 1440p / 1080p / 720p / 480p | Caps the resolution |
| Audio only (original) | Best audio stream, untouched |
| Audio only (MP3) | Best audio, transcoded to MP3 |

You can also pick an **exact format** by ID from the same dropdown. If you choose a video-only
stream, the best audio track is merged in automatically rather than silently giving you a mute file.

Under **Options**: embed cover art, embed English subtitles, write title/artist metadata. Metadata is
on by default; the other two are opt-in because a post-processing failure fails the whole download.

**File naming.** Files are saved as `Title [videoid] 1080p 137+140.mp4` — the quality and the
exact yt-dlp format ids are part of the name. That is deliberate rather than decorative: yt-dlp
will not overwrite an existing output, so if every quality shared one filename, asking for 1080p
after already having 720p would return the 720p file instantly and record it as a successful
1080p download. Tagging the name keeps each quality a separate file, and makes it obvious which
is which. (**Re-download** does overwrite, since that is the point of it.)

Downloads keep running if you close the popup, and reopening it reconnects to the download in
progress. **Cancel** stops one mid-flight and cleans up the partial files it wrote.

A retry that transfers nothing is treated as a bad resume offset — a `.part` left by an earlier
failed run makes yt-dlp send a Range request that a freshly signed URL rejects with a 403 before
any data arrives — so the file is started over instead of retrying the same doomed resume. A retry
that *did* move bytes resumes normally and keeps its progress.

## 4. Download history dashboard

Click **History** in the popup for a dashboard of every past download:

- Thumbnail, title, channel, duration, file size, live progress, and the current stage
  ("Merging video and audio…", "Writing metadata…")
- Search by title/channel, filter by status, sort by date/title/size
- **Open** the file, **Show in folder**, **Cancel**, **Re-download**, or **Delete** (removes the file
  from disk too), plus **Clear failed** to tidy up
- Polling pauses while the tab is in the background

History persists across restarts in `server/data/history.json` (capped at the 500 most recent).

## 5. Staying up to date

The server checks PyPI for a newer `yt-dlp` every 6 hours. If there is one it upgrades and restarts
itself, so YouTube-side breakage (403s, broken extraction) gets picked up automatically. It asks PyPI
for the version first and only runs `pip install` when there's actually something newer.

A restart **waits for in-flight downloads to finish** rather than killing them. The dashboard's
**Check for Updates** button runs the same check on demand.

Turn it off with `YTDLP_AUTO_UPDATE=0`.

## YouTube notes

**Match the CLI.** Plain `yt-dlp <url>` downloads these videos start to finish at full speed. Every
option this server adds on top is a deviation from the configuration upstream actually tests, and
every deviation here has caused an outage at some point. Two in particular:

### Don't chunk the download

`http_chunk_size` makes yt-dlp fetch a file as a series of separate ranged requests. googlevideo
rejects those with **HTTP 403 once past the first chunk or two**, so downloads died at 10–20 MB —
and crawled at ~30 KB/s until they did. Removing it took the same 1.08 GB download from "fails after
20 MB" to **1.3 MB/s, start to finish**.

The symptom is misleading: a 403 mid-transfer reads like an expired URL or throttling, and retrying
re-extracts a fresh URL that fails at exactly the same place. Short videos hide it completely,
because they finish inside the first chunk.

### Don't pin a player client

yt-dlp chooses its client list per release to track whatever YouTube is currently doing. Pinning one
overrides that judgement with a snapshot that rots:

| Pin | What it did |
|---|---|
| `mweb` | Requires a PO token. With no provider running, YouTube served **only format 18 (360p)** |
| `default,android_vr` | Full format list, but URLs that 403 past ~20 MB |
| *(nothing)* | What the CLI does. Works. |

So this server pins nothing, and gets each release's fix for free through the auto-updater.

### Optional: the bgutil PO-token provider

The [bgutil provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) is used when it is
running, and can help on videos that demand a token. It is **not** required for normal use.

Its `canvas` dependency has no prebuilt binary for current Node and needs a C++ toolchain to
compile, so the install that works without one is:

```bash
cd ~/bgutil-ytdlp-pot-provider/server
npm install --omit=dev --ignore-scripts     # skip the native build
npm install --no-save @napi-rs/canvas       # prebuilt drop-in replacement
npx tsc                                     # needs typescript installed
node build/main.js --port 4416
```

`GET /health` reports `pot_provider_reachable`.

> A half-installed provider is worse than none: yt-dlp finds the checkout, spawns deno, and the
> 15-second probe timeout escapes as an exception that kills the entire extraction. The server
> checks that the provider's dependencies are actually installed and, if not, deliberately points
> the plugin at an empty path so it is skipped instead.

### JavaScript challenges

YouTube signs media URLs with a JavaScript challenge. yt-dlp runs it with a **local** engine
(`deno`, `bun`, or `node`) or downloads its own EJS solver from GitHub releases
(`remote_components: ["ejs:github"]`).

Prefer a local engine. The GitHub download sits behind Cloudflare, which blocks it unless the
optional `curl_cffi` package is installed, and yt-dlp *retries* the failed fetch before extraction
can continue — turning a 10-second extraction into a 4-minute one. The server only asks for the
remote component when no local engine exists. If you have none and can't install one:

```bash
pip install curl_cffi
```

### Staying current

YouTube breaks things faster than stable releases ship. When a download fails in a way that smells
server-side, try the nightly:

```bash
pip install --pre --upgrade yt-dlp
```

## Configuration

All optional, all environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `YTDLP_SERVER_PORT` | `4599` | Listening port |
| `YTDLP_DOWNLOAD_DIR` | `~/Downloads/yt-dlp-extension` | Where files land |
| `YTDLP_FFMPEG` | auto-detected | Explicit ffmpeg path |
| `YTDLP_MAX_CONCURRENT` | `2` | Simultaneous downloads; the rest queue |
| `YTDLP_POT_PROVIDER_URL` | `http://127.0.0.1:4416` | bgutil provider |
| `YTDLP_POT_SERVER_HOME` | `~/bgutil-ytdlp-pot-provider/server` | bgutil checkout, for the script provider |
| `YTDLP_AUTO_UPDATE` | `1` | `0` disables background upgrades |
| `YTDLP_EXTRA_ORIGINS` | — | Extra allowed CORS origins, comma-separated |

If the port is taken the server says so plainly instead of dumping a traceback.

## Security

The server binds `127.0.0.1` only, and accepts requests **only from browser extensions**
(`chrome-extension://`, `moz-extension://`) or non-browser clients. Any web page you visit can reach
`127.0.0.1:4599`, so a wide-open `Access-Control-Allow-Origin: *` would let any site start downloads,
open files, or delete them. Web-page origins get a 403.

File operations are additionally confined to the download directory, so a tampered history record
can't make the server open or delete something elsewhere on disk.

There's no auth or multi-user support — it's single-user and local-only by design.

## Tests

```bash
python server/test_server.py    # behaviour
python server/test_docs.py      # this README vs the code
```

**60 behaviour tests**, all offline — no network, no downloads. They cover the failure modes that
actually bit: explicit ffmpeg resolution and encoder capability, container fallback, video-only
audio merging, quality-tagged filenames, the resume-vs-restart decision, error-message translation,
the origin guard, path confinement, partial cleanup, and history handling.

**22 documentation tests** check this README against the code — that every documented endpoint is
routed and every route documented, that the preset and configuration tables match what the server
actually has, that the stated defaults are the real ones, and that the extension manifest lines up
with the files and permissions it uses. Docs drift silently; these fail loudly instead.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/ping` | Liveness, yt-dlp/ffmpeg status |
| GET | `/health` | Verbose diagnostics incl. PO-token provider |
| GET | `/formats?url=` | Formats, metadata, and resolved presets with sizes (cached 5 min; `&refresh=1` to bypass) |
| POST | `/download` | Start a download |
| GET | `/progress/<job_id>` | Progress of one job |
| POST | `/cancel` | Cancel by `job_id` or record `id` |
| GET | `/history` | All records |
| POST | `/open_file`, `/open_folder`, `/open_downloads_folder` | File manager integration |
| POST | `/delete`, `/clear_history` | Remove records |
| POST | `/redownload` | Re-run a past download |
| GET | `/version`, POST `/update` | yt-dlp version and upgrades |

## Possible next steps

Native messaging (auto-start the server with Chrome), whole-playlist support, a context-menu entry
for links, and a bandwidth cap.
