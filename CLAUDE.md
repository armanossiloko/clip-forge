# ClipForge: notes for Claude Code

ClipForge is a local web app that turns long videos (YouTube links or local files) into vertical short-form clips for TikTok / YouTube Shorts / Reels. The owner runs it on **Windows**, preferably in **Docker Desktop**, and develops it with Claude Code. Talk to the owner in plain language; they're a creator, not necessarily a developer.

## Status (handoff from the claude.ai session, 2026-11-17)

Done and tested in a Linux sandbox:
- The full pipeline with a fake `claude` and a hand-made transcript: clip selection, the track/center/fit framings, captions + hook banner, editing → re-render, add clip, upload, zip, Range requests.
- **The Docker image builds and runs**, but it was tested on a stand-in base (Ubuntu 24.04 + Node 22, via `--build-arg BASE_IMAGE=...`) because Docker Hub was blocked there. Inside the container: Claude Code 2.1.x installed; without a token it answers "Not logged in", and the app shows the `.env` hint. Node is detected as the yt-dlp JS runtime. Poppins captions render through fontconfig. A Windows-style path is resolved to `/videos/<name>`.

**Verified on the owner's PC (2026-11-17):** the real `node:22-bookworm-slim` build, the token in `.env` + Test Claude, a real YouTube download, CPU whisper, real `claude -p` picks, rendering. Also tested there with real Claude: the modes, Cuts, Ask ClipForge and smart framing (see below).
**Still untested:** the native path (`setup.bat` / `start.bat`). The PC has no Python/ffmpeg/Deno, so ask the owner before running it, and note that `run.py` uses port 8765 like the Docker container.

## How it works

`app/pipeline.py` runs each video ("job") through three stages in a single worker thread. **Reels ("clips" in the code) are drafts** (start/end/text, `status: "draft"`) until the owner exports them; exports go through a second worker. The owner wants nothing rendered before they confirm.

1. **Get video**: the `yt-dlp` Python API (`_download`), or a local file (`resolve_video_path`: a full path, else a lookup by file name in `VIDEOS_DIR`), or an upload. yt-dlp needs a JS runtime for YouTube (Deno, or Node ≥ 20). `_download` passes whichever it finds via `js_runtimes`.
2. **Transcribe**: `faster-whisper`, `word_timestamps=True`, VAD on. Tries CUDA first, falls back to CPU int8. Writes `transcript.json` (segments + words). Models are cached in `HF_HOME` (`./models` in Docker).
3. **Claude picks moments**: `app/claude_client.py` pipes the prompt + timestamped transcript to `claude -p --output-format json` **via stdin** (Windows command lines max out at ~8k chars), with an empty temp cwd. It parses `result`, strips code fences, and retries once on bad JSON (`_ask_json`). `snap_to_words` moves boundaries onto word edges; overlapping clips are dropped.
   - **Peaks**: every clip is built around a `peak` (punchline / fail / payoff) that must land ~30-70 % in (`claude_client.ARC`); the old "open on the punchline" rule left reels with nothing after the first seconds, and in gameplay videos the transcript only has the reactions *after* the event. `place_peak` enforces it: a peak in the first ~30 % (or 5 s) moves the start back to a sentence start; a reel ending on its peak gets a few seconds of payoff. The peak is drawn on the timeline.
   - Each job has a **brief** (`job["brief"]`, from `normalize_brief`): `mode` (top, funny, takes, tips, stories, quotes, custom, cuts; prompts in `claude_client.MODES`), `instructions` (this video only; Settings → `extra_instructions` is the channel-wide text), an optional `range` [from, to], and `num_clips`/`min_seconds`/`max_seconds`, which `apply_brief` copies into `job["settings"]`. `POST /api/jobs/{id}/reanalyze` takes a new brief ("New set…").
   - **Cuts** (`_stage_cuts`) doesn't ask Claude to pick: `plan_cuts` splits the range into even back-to-back parts ≤ max length, cutting between sentences (else between words), then `describe_cuts` asks Claude only for titles/hooks/captions. Clips get `part: [i, n]`.
   - **Ask ClipForge** (`POST /api/jobs/{id}/ask`, `start_ask` → `_run_ask` on the job worker, `stage: "asking"`): Claude gets the transcript, the numbered clips and the last 8 chat messages, and answers `{"reply", "add", "update", "delete"}`; `_apply_ask` applies it (drafts; nothing is rendered). The chat lives in `job["chat"]` (roles you / claude / error / note). Failures go into the chat, not the job's error.
   - **Who made it**: `clip["made_by"]` = `{kind: claude | cuts | you, model, label, via?}` from Claude Code's `modelUsage` (`_model_used`: the model with the most output tokens), plus `revised_by` (Claude changed it on request) and `edited` (the owner changed times/text). `made_by_text` goes on the card, into `clip_XX.txt` and into the MP4 `comment` tag.
4. **Export** — the UI calls it **Generate** (the owner wanted one button: make the file *and* download it). `POST …/clips/{id}/render`; the page downloads the file by itself when it's ready (`ws.autoDownload`), then the button says *Download again* (no re-render). *Generate all* = `…/render-all` (drafts + changed) and then one zip (`ws.zipWhenDone`). Progress comes from ffmpeg `-progress` (`tools.run_ffmpeg_progress` → `clip.progress`). Downloads are refused mid-export and sent `no-store` with a `?v=rendered_at` link (a stale cached download once handed the owner an old version). `render_clip` → ffmpeg 1080×1920, H.264 CRF 20, loudnorm −14 LUFS; stores `export_spec` so `export_state` can say draft / exporting / exported / changed. "New set…" (`reanalyze`) replaces drafts and keeps exported clips (`find_clips` flag).
   - **Look** (per reel, `clip["look"]`, the editor's Look tab): `zoom` 0..1 (whole frame ↔ fill), `pos_x`/`pos_y` 0..1, `background` blur|black, and caption overrides (`captions`, `caption_size`, `caption_position`, `caption_words`, `caption_uppercase`, `caption_color`, `caption_highlight`). `normalize_look` validates; `effective_look` = Settings + `LOOK_DEFAULTS` + the reel's look feeds `build_ass` and `fit_graph`/`fit_rect` (the whole-frame layout: crop the visible part of the source, scale, overlay). The preview draws the same with `fitRect` in workspace.js on the canvas, so tweaks cost nothing until Generate; edits are kept in `ws.lookPending` and PATCHed 400 ms later. `export_key` includes the look only when set (older exports stay "exported"). `POST /api/jobs/{id}/look-all` copies one look to every reel. Verified: preview frame = exported frame.
   - **Framing plan** (`app/crop.py`, `pipeline.framing_plan`): `{"shots": [{t0, t1, layout: crop|fit}], "keys": [[t, x, is_cut]], crop_w, …}`, times relative to the reel start. The export turns it into an ffmpeg crop x-expression (`keys_to_expr`: hold, 0.6 s glides, jumps) + `overlay=enable='between(t,…)'` for mixed shots; the editor's phone preview evaluates the **same** keys in JS (`keyX` in workspace.js), so preview = export. **OpenCV must stay < 5** (5.x dropped the Haar cascades). Tuned: 480 px wide, scaleFactor 1.05, minNeighbors 6, no equalizeHist.
   - **Default framing is `fit`** (whole frame on a blurred copy, never zoomed). The owner insists nothing is cut off: Smart zoomed into Haar false positives on dark gameplay and they were furious. The zoomed modes are opt-in only.
   - **Smart** (`crop_mode: "smart"`, opt-in) gives each shot `crop` (one face, or faces close enough for one close-up) or `fit` (whole frame on a blurred copy): hard cuts from ffmpeg `scdet` (threshold 10), faces every 0.5 s, extra splits where face/no-face changes for ≥ 3 samples (crossfades), shorter runs folded into neighbours. The owner disliked plain cropping (slides unreadable).
   - **One-time scan** (`ensure_prepared` → `_prepare` on its own worker, `job["prep"]` progress): timeline thumbnails (`filmstrip.jpg`, keyframes only) + `crop.analyze_video`, one ffmpeg pass that both runs scdet and pipes 2 fps gray frames to the face detector → `analysis.json`. Then any plan is instant (~20 ms). Before it, `make_plan` samples just the reel (a few seconds). It starts after Claude's step, or when an older video is opened.
   - `app/captions.py`: ASS subtitles, 3-word chunks, current word highlighted + scale pop, hook banner for the first 3 s (BorderStyle 3 box). Words are separated by `WORD_GAP` (a space stretched to 160 %; a plain space looked stitched together under the thick outline). The live preview mirrors it (`.pv-cap .w + .w`) and converts ASS sizes with `ASS_EM = 0.568` (libass sizes Poppins by winAscent+winDescent), so preview = export within ~2 %. Burned in with `subtitles=filename=clip_XX.ass:fontsdir=<relative path to ./fonts>`, **run with cwd = the clips folder** to avoid Windows drive-letter escaping in filtergraphs. If burning fails, it re-renders without captions and stores a warning.

Other files:
- `app/main.py`: FastAPI routes + static UI. `POST /api/claude-test` checks the Claude login. In Docker, `open-folder` returns the host-relative path instead of opening Explorer.
- `app/static/`: vanilla JS, no build step. Polls `/api/jobs` (1.5 s while busy, otherwise 6 s). `workspace.js` is the video page / editor (Steam-style): source `<video>`, a canvas phone preview drawn from it (plan + HTML captions in the bundled font, served at `/fonts`), a timeline with all reels (drag edges/move, snap to word edges, Alt = no snap), an overview strip, the selected reel's details (autosave PATCH), keyboard shortcuts. Sources the browser can't play get an H.264 `proxy.mp4` (`POST /api/jobs/{id}/proxy`). `app.js` holds the rest (the "＋ New video" modal `#newDlg` + brief, which also opens when a file is dragged onto the page; job list, Ask ClipForge, settings).
- Manual reels: I = mark start, O = mark end → `POST /api/jobs/{id}/clips` (`made_by` kind you, titled from its first words); Shift+I/O move the selected reel's edges.
- Page-only changes (`app/static/*`) can go live without restarting (a restart re-runs an unfinished scan): `docker cp app/static/<file> clipforge:/app/app/static/<file>`. The next `docker compose up -d --build` bakes them in anyway.
- Browser test: `mcr.microsoft.com/playwright:v1.49.0-jammy` (install `playwright@1.49.0` in a scratch folder), against a throwaway container; the NASA AV1 source plays in its Chromium.
- `app/config.py`: settings live in `data/settings.json`. Env vars: `CLIPFORGE_DOCKER`, `CLIPFORGE_VIDEOS_DIR`, `CLIPFORGE_CAPTION_FONT`. Clip re-renders pick up the current caption/crop settings (`RENDER_KEYS`).
- `app/tools.py`: finds external tools (with Windows fallback paths) and uses `CREATE_NO_WINDOW` for subprocesses. `health()` feeds the status pills.
- Jobs persist as `data/jobs/<id>/job.json`. On restart, interrupted jobs become "error", and Retry resumes from the first missing stage.
- Docker: `Dockerfile` (`ARG BASE_IMAGE=node:22-bookworm-slim`; runs as root to avoid bind-mount permission problems; Claude Code installed with npm, `DISABLE_AUTOUPDATER=1`, `/root/.claude.json` onboarding flag), `docker-compose.yml` (127.0.0.1:8765, volumes `data`/`models`/`videos`, optional `.env`), `docker-start.ps1`/`.bat` (starts Docker Desktop, asks for the token, builds, opens the browser).
- Native: `setup.bat` (winget: Python 3.12, ffmpeg, Deno, Claude Code), `start.bat` → `run.py`, `update.bat`.
- `fonts/Poppins-Bold.ttf` (OFL): the default caption font in both modes.

## Repo and CI

- GitHub: `armanossiloko/clip-forge` (public), default branch `master`, Conventional Commits.
- `.github/workflows/build.yml` (mirrors the owner's azure-admin repo): a quick `check` job (Python compiles, page scripts parse), then semantic-release (`.releaserc.json`: tag `vX.Y.Z` + GitHub release from the commits; chore/docs/ci don't release), then the image goes to `ghcr.io/armanossiloko/clip-forge` as `latest`, `sha-…` and the version. Pull requests only run `check`.

## Auth for Claude inside Docker

The owner runs `claude setup-token` on Windows (browser login, prints a one-year `sk-ant-oat01-…` token) and puts it in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`. The fallback is `ANTHROPIC_API_KEY`. **Never commit `.env`, and never ask the owner to paste the token into the chat**; have them edit `.env` themselves or run `docker-start.bat`.

## Testing without network or real Claude

- Fake Claude: put an executable named `claude` first on PATH (or bind-mount it to `/usr/local/bin/claude`). It reads stdin and prints `{"type":"result","is_error":false,"modelUsage":{"claude-sonnet-5-5":{"outputTokens":100}},"result":"{\"clips\":[{\"start\":2,\"end\":24,\"title\":\"…\",\"hook\":\"…\",\"caption\":\"…\",\"hashtags\":[\"#a\"],\"score\":90,\"reason\":\"…\"}]}"}`. To fake all three prompt types, grep stdin: `CREATOR'S REQUEST` → Ask (`{"reply","add","update","delete"}`), `consecutive vertical reels` → Cuts (`{"parts":[…]}`), else clips. Note the health check runs `claude --version` every 30 s.
- Test new code without touching the owner's running `clipforge` container: run a throwaway one from `clipforge:latest` on another port with `-v <repo>/app:/app/app:ro`, a scratch `data` folder, and the fake `claude` mounted (or `--env-file .env` for real Claude).
- Skip whisper: copy a hand-made `transcript.json` (`{"language":"en","segments":[{"start","end","text","words":[{"start","end","word"}]}]}`) into the job folder, then press Retry.
- Synthetic face video: paste `skimage.data.astronaut()` onto a 1920×1080 canvas and move it around.

## Roadmap (ask the owner which comes next)

1. **Verify on the real machine** (the list above). Fix whatever breaks.
2. **Auto-posting**: YouTube Shorts via YouTube Data API v3 (OAuth desktop client, `videos.insert`, `#Shorts` in the title/description). TikTok via the Content Posting API (inbox/draft upload; direct posts stay private until TikTok audits the app). Store OAuth tokens in `data/` and never commit them. Add a "Post" button per clip and a posted/scheduled status.
3. Optional GPU in Docker: a `docker-compose.gpu.yml` with an NVIDIA device reservation + `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` in the image.
4. Nice-to-haves: better face detection (YuNet/MediaPipe), multiple caption style presets, B-roll/emoji, batch processing a whole channel or playlist, scheduled posting.

## Conventions

- The UI stays dependency-free (no npm build). Keep Windows paths, quoting and subprocess quirks in mind.
- Batch files must keep CRLF line endings (`.gitattributes` enforces this).
- Only clip content the owner has rights to; the README says so.
