# ClipForge

Turns long videos into vertical short-form clips for TikTok, YouTube Shorts and Instagram Reels. It runs on your own PC, in Docker or natively on Windows. It uses your Claude subscription through Claude Code to decide which moments are worth clipping.

```
YouTube link or video file
   → download (yt-dlp)
   → transcribe with word timings (faster-whisper, runs locally)
   → Claude picks the moments you asked for (or ClipForge cuts a part into reels) and writes title, hook, caption and hashtags
   → render 1080×1920 clips: the whole frame on a blurred background (or, if you choose, zoomed in on faces),
     animated word-by-word captions, hook banner, loudness levelled
   → review, tweak, ask Claude for changes, re-render, download
```

## Run it with Docker (recommended)

You need **Docker Desktop** (`winget install -e --id Docker.DockerDesktop`, then restart) and a Claude **Pro, Max, Team or Enterprise** plan.

1. **Get a Claude token for the container.** On your PC, in PowerShell:
   ```powershell
   claude setup-token
   ```
   Log in in the browser. It prints a token that starts with `sk-ant-oat01-` and lasts a year.
2. **Start ClipForge.** Double-click `docker-start.bat`. It starts Docker Desktop if needed, asks for the token the first time (saved in `.env`, which is never committed), builds the image and opens http://localhost:8765.

Or do it by hand:
```powershell
copy .env.example .env      # then paste your token into .env
docker compose up -d --build
```

| Folder (next to `docker-compose.yml`) | What's in it |
|---|---|
| `videos\` | Put local video files here, then type the file name in ClipForge |
| `data\jobs\<id>\clips\` | Finished clips (`clip_01.mp4`) + post text (`clip_01.txt`) |
| `models\` | Speech models, downloaded once on first use |

Useful commands: `docker compose logs -f` (watch it work), `docker compose down` (stop), `docker compose up -d --build` (rebuild after an update; this also updates the YouTube downloader).

**Prebuilt image:** every push to `master` publishes `ghcr.io/armanossiloko/clip-forge` (tags `latest`, the release version, and `sha-…`). To use it instead of building, replace `build: .` in `docker-compose.yml` with `image: ghcr.io/armanossiloko/clip-forge:latest`, then `docker compose pull && docker compose up -d`.

## Run it natively on Windows (no Docker)

Double-click `setup.bat` once. It installs Python, ffmpeg, Deno and Claude Code with winget. Then run `claude` once in a terminal to log in, and double-click `start.bat` to use it. Run `update.bat` if YouTube downloads break.

## Using it

- **Paste a YouTube link**, or the name of a file in `videos\`, and press **Go**. You can also **drag a video file** onto the drop area.
- Under the link box, choose **what to make**: *Top moments*, *Funniest bits*, *Hot takes*, *Tips & how-tos*, *Stories*,
  *Short quotes*, *Custom* (describe what you want, e.g. "every time she talks about Mars"), or *Cuts*: no picking, the part of
  the video you choose (or all of it) becomes reels as it is, split between sentences when it's longer than the max length.
  The box under the choice is for anything specific to this video. The line below it sets the number of reels, their length,
  and an optional From / To part of the video.
- Three steps run: *Get video → Transcribe → Claude finds the reels*. Each suggested reel is built around a **peak** (the
  punchline, fail or payoff) that lands in the middle of the reel, with build-up before it; the peak is marked on the timeline.
- **Reels are drafts until you press Generate.** Nothing is rendered while you edit, and every change saves by itself.
- The video page is the editor:
  - the **original video** on the left, a **live phone preview** of the selected reel in the middle (framing, captions and
    hook, drawn as you play or scrub; the generated file looks the same), and the reel's details on the right;
  - a **timeline** over the whole video with **every reel highlighted**. Click a reel to select it, drag its edges (they snap
    to words; hold Alt to stop that) or drag its middle to move it. The strip under it is the whole video at a glance.
  - **Make your own reels**: press **Mark start** (**I**) where a reel should begin, play or scrub to its end, and press
    **Mark end** (**O**): the new reel appears, titled after its first words. **Esc** cancels. **N** adds a 30 s reel at the playhead.
  - Keys: **Space** play/pause, **Shift+I** / **Shift+O** move the selected reel's start / end to the playhead, **← →** step
    (Shift: finer), **+ −** zoom. Double-click a sentence on the timeline to start the selected reel there.
  - **Framing**: *Whole frame* (the default: the full video, never zoomed in, on a blurred copy of itself). If you want close-ups,
    pick *Zoom in on faces when found* (it decides per shot; face detection can be fooled by games and dark scenes),
    *Always zoom in and follow the face* or *Always zoom in on the middle*, in Settings or for one reel.
  - **Generate** makes the 1080×1920 video file and downloads it to your computer as soon as it's ready (it shows
    *Generating… 45%* meanwhile). Each reel shows *Draft*, *Generated* or *Changed since generated*; a generated reel's button
    becomes **Download again**, and an edited one **Generate again**. **Generate all** (top) does every reel that needs it and
    downloads them together as one zip; when all are generated it becomes **Download all (zip)**. The files are also kept in
    the video's folder (**Open folder**).
- The first time a video is opened, ClipForge scans it once in the background for faces and camera cuts. After that the live
  preview's framing is instant for any part of the video; before that it takes a few seconds per reel.
- **Ask ClipForge** (under the timeline) is a chat with Claude about the video: *"give me 3 more reels about food in space"*,
  *"make reel 2 end on the punchline"*, *"turn 1:00 to 1:45 into a reel"*, *"delete the weakest reel"*. Its changes are drafts too.
- Each reel says who made it: *Generated by Claude Opus 5.5*, *changed by … on request*, *edited by you*, *Added by hand*,
  or *Cut by ClipForge* for Cuts. The same line is in the reel's post text file and in the MP4's file details.
- **New set…** replaces the draft reels with a fresh set, of the same kind or a different one. **Generated reels are kept**,
  so you can always come back to the video and see everything made from it.
- **Settings → Test Claude** checks that Claude is connected.

### Settings worth knowing

| Setting | What it does |
|---|---|
| About your channel | The most useful one. Claude sees it on every video: your channel, audience and language, e.g. *"Fitness channel for beginners. Write captions in Bosnian."* What to look for in one particular video goes in the box in the **＋ New video** window. |
| Clips per video / min / max length | The starting values for new videos (you can change them per video). 20–60 s works well everywhere. |
| Framing | Whole frame, never zoomed in (recommended), or one of the zoomed-in modes (faces when found, always the face, the middle). They cut off the sides. |
| Claude model | Default, Opus (best judgement) or Haiku (fastest). |
| Speech model | `small` is a good balance on CPU. `medium` is more accurate but slower. |
| Language | Leave empty to auto-detect, or set it (`bs`, `en`, `de`…). |
| Caption look | Font (Poppins Bold is bundled), size, colors, words per caption, height, uppercase. Use **Re-render all** to apply a new look to existing clips. |

**Speed on CPU (`small` model):** transcription takes about ¼–½ of the video's length, Claude takes 20–90 s, and each clip renders in 10–40 s.

## Troubleshooting

- **"Claude login missing" (red pill):** put `CLAUDE_CODE_OAUTH_TOKEN=...` in `.env` and run `docker compose up -d`. If the token is rejected (401), make a new one with `claude setup-token`, or use `ANTHROPIC_API_KEY` instead (see `.env.example`).
- **YouTube download fails:** run `docker compose build --no-cache` then `docker compose up -d` to get the latest downloader. Native install: `update.bat`. Then press **Retry**.
- **Transcription fails on first run:** the speech model downloads from huggingface.co the first time. Check your internet connection or firewall.
- **File not found:** in Docker, ClipForge can only see the `videos\` folder. Move the file there and type its name.

## For developers

See [CLAUDE.md](CLAUDE.md) for architecture, testing tricks and the roadmap.

## A note on content

Only clip and repost videos you own or have permission to use. Reposting other people's content can get your accounts struck.

Poppins font © The Poppins Project Authors, SIL Open Font License 1.1 (`fonts/OFL.txt`).
