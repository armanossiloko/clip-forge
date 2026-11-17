"""The ClipForge pipeline: download → transcribe → Claude picks moments → render vertical clips."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import captions, claude_client, crop, tools
from .config import FONTS_DIR, JOBS_DIR, VIDEOS_DIR, load_settings

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts"}

# ----------------------------------------------------------------------------- job store

_jobs: dict[str, dict] = {}
_lock = threading.RLock()
_job_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job")      # one long job at a time
_render_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")  # quick re-renders


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def save(job: dict) -> None:
    with _lock:
        d = job_dir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(d / "job.json")


def update(job: dict, **fields) -> None:
    with _lock:
        job.update(fields)
        job["updated"] = time.time()
        save(job)


def load_all() -> None:
    for f in JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("status") not in ("done", "error"):
            job["status"] = "error"
            job["error"] = "ClipForge was closed while this was running. Press Retry to continue where it stopped."
        for c in job.get("clips", []):
            if c.get("status") in ("pending", "rendering"):  # an export was interrupted
                c["status"] = "ready" if c.get("file") else "draft"
            if c.get("file") and not c.get("export_spec"):   # exported before ClipForge tracked changes
                c["export_spec"] = export_key(c)
            if not c.get("file") and c.get("status") == "ready":
                c["status"] = "draft"
            c.pop("preview", None)
            c.pop("progress", None)
        if job.get("proxy") == "building":
            job["proxy"] = None
        if (job.get("prep") or {}).get("status") in ("queued", "running"):
            job["prep"] = None  # the scan restarts when the video is opened
        _jobs[job["id"]] = job


def list_jobs() -> list[dict]:
    with _lock:
        return sorted(_jobs.values(), key=lambda j: j.get("created", 0), reverse=True)


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def delete_job(job_id: str) -> None:
    with _lock:
        _jobs.pop(job_id, None)
        _analysis.pop(job_id, None)
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def normalize_brief(raw: dict | None, settings: dict) -> dict:
    """What the creator asked for on a video: mode, request text, optional time range, count and lengths.
    Missing values come from Settings. Raises ValueError with a message for the UI."""
    raw = raw or {}
    mode = raw.get("mode") if raw.get("mode") in claude_client.MODES else "top"
    instructions = str(raw.get("instructions") or "").strip()[:2000]
    if mode == "custom" and not instructions:
        raise ValueError("Tell Claude what to look for (the box under the mode).")

    def num(key: str, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(float(raw[key]))))
        except (KeyError, TypeError, ValueError):
            return max(lo, min(hi, int(settings[key])))

    min_s, max_s = num("min_seconds", 3, 180), num("max_seconds", 5, 180)
    if max_s < min_s:
        raise ValueError("Max length must be at least the min length.")

    def secs(key: str) -> float | None:
        v = raw.get(key)
        if v is None or v == "":
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError("Times must look like 1:23 or 83") from None
        if v != v or v < 0:  # NaN or negative
            raise ValueError("Times must look like 1:23 or 83")
        return round(v, 2)

    rs, re_ = secs("range_start"), secs("range_end")
    if re_ is not None and re_ - (rs or 0) < 1:
        raise ValueError("'To' must be after 'From'.")
    return {
        "mode": mode,
        "instructions": instructions,
        "range": [rs or 0.0, re_] if (rs or re_ is not None) else None,  # [from, to]; to=None means the end
        "num_clips": num("num_clips", 1, 30),
        "min_seconds": min_s,
        "max_seconds": max_s,
    }


def apply_brief(settings: dict, brief: dict) -> dict:
    for k in ("num_clips", "min_seconds", "max_seconds"):
        settings[k] = brief[k]
    return settings


def brief_label(brief: dict | None) -> str:
    """'Top moments · 5 reels · 20-60 s' / 'Cuts · 2:00-5:00 · up to 60 s'."""
    b = brief or {}
    mode = b.get("mode") or "top"
    parts = [claude_client.MODES.get(mode, {}).get("label", "Top moments")]
    if b.get("range"):
        rs, re_ = b["range"]
        parts.append(f"{_mmss(rs)}-{_mmss(re_) if re_ is not None else 'end'}")
    if mode == "cuts":
        parts.append(f"up to {b.get('max_seconds', 60)} s each")
    elif b:
        parts.append(f"{b.get('num_clips')} reels · {b.get('min_seconds')}-{b.get('max_seconds')} s")
    return " · ".join(parts)


def _mmss(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def create_job(source_type: str, value: str, title: str = "", brief: dict | None = None) -> dict:
    settings = load_settings()
    brief = normalize_brief(brief, settings)  # validate before creating anything
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    job = {
        "id": job_id,
        "created": time.time(),
        "updated": time.time(),
        "status": "queued",
        "stage": "queued",
        "progress": 0.0,
        "message": "Waiting to start…",
        "error": None,
        "source": {"type": source_type, "value": value, "title": title or Path(value).stem if source_type != "url" else title},
        "video": None,
        "clips": [],
        "brief": brief,
        "chat": [],
        "settings": apply_brief(settings, brief),
    }
    with _lock:
        _jobs[job_id] = job
    save(job)
    return job


def start_job(job: dict) -> None:
    update(job, status="queued", error=None, message="Waiting to start…")
    _job_worker.submit(_run_job, job["id"])


# ----------------------------------------------------------------------------- stages

def _run_job(job_id: str) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    try:
        update(job, status="running")
        if not job.get("video"):
            _stage_source(job)
        if not (job_dir(job_id) / "transcript.json").exists():
            _stage_transcribe(job)
        if job.get("find_clips") or not job.get("clips"):
            _stage_analyze(job)
        # Reels stay drafts (start/end/text) until the creator exports them; nothing is rendered here.
        n = len(job["clips"])
        update(job, status="done", stage="done", progress=1.0, message=f"{n} reel{'s' if n != 1 else ''} to review")
        ensure_prepared(job)
    except Exception as exc:  # noqa: BLE001 - surface every failure in the UI
        traceback.print_exc()
        update(job, status="error", error=str(exc), message="Failed")


def _stage_source(job: dict) -> None:
    src = job["source"]
    d = job_dir(job["id"])
    if src["type"] == "url":
        update(job, stage="downloading", progress=0.0, message="Downloading video…")
        path, title = _download(job, src["value"], d)
        job["source"]["title"] = title
    else:
        path = Path(src["value"])
        if not path.exists():
            raise RuntimeError(f"File not found: {path}")
    info = tools.probe(path)
    update(job, video={"path": str(path), **info})


def _download(job: dict, url: str, d: Path) -> tuple[Path, str]:
    import yt_dlp

    settings = job["settings"]
    max_h = int(settings.get("max_download_height", 1080))

    def hook(p):
        if p.get("status") == "downloading":
            total = p.get("total_bytes") or p.get("total_bytes_estimate") or 0
            frac = (p.get("downloaded_bytes", 0) / total) if total else 0
            speed = p.get("_speed_str", "").strip()
            update(job, progress=min(frac, 0.99), message=f"Downloading… {frac * 100:.0f}% {speed}")
        elif p.get("status") == "finished":
            update(job, message="Merging audio and video…")

    runtimes = {}
    for name in ("deno", "node", "bun"):
        exe = tools.find(name)
        if exe:
            runtimes[name] = {"path": exe}
    opts = {
        "format": f"bv*[height<={max_h}][ext=mp4]+ba[ext=m4a]/bv*[height<={max_h}]+ba/b[height<={max_h}]/bv*+ba/b",
        "outtmpl": str(d / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": str(Path(tools.ffmpeg()).parent),
        "js_runtimes": runtimes or {"deno": {}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        msg = str(exc)
        if not runtimes:
            msg += "\n→ YouTube needs a JavaScript runtime. Install Deno with:  winget install DenoLand.Deno"
        msg += "\n→ If YouTube changed something, update the downloader: run update.bat"
        raise RuntimeError(msg) from exc
    files = [f for f in d.glob("source.*") if f.suffix.lower() in VIDEO_EXTS]
    if not files:
        raise RuntimeError("Download finished but no video file was found")
    return max(files, key=lambda f: f.stat().st_size), info.get("title") or url


_whisper_cache: dict = {}


def _load_whisper(name: str, device: str):
    from faster_whisper import WhisperModel

    key = (name, device)
    if key not in _whisper_cache:
        _whisper_cache.clear()  # keep only one model in memory
        compute = "float16" if device == "cuda" else "int8"
        _whisper_cache[key] = WhisperModel(name, device=device, compute_type=compute)
    return _whisper_cache[key]


def _stage_transcribe(job: dict) -> None:
    settings = job["settings"]
    d = job_dir(job["id"])
    update(job, stage="transcribing", progress=0.0, message="Extracting audio…")
    if not job["video"].get("has_audio", True):
        raise RuntimeError("This video has no audio track, so there is nothing to transcribe.")
    audio = d / "audio.wav"
    r = tools.run([tools.ffmpeg(), "-y", "-i", job["video"]["path"], "-vn", "-ac", "1", "-ar", "16000", str(audio)])
    if r.returncode != 0:
        raise RuntimeError(f"Could not extract audio: {r.stderr[-500:]}")

    want = settings.get("whisper_device", "auto")
    devices = ["cuda", "cpu"] if want == "auto" else [want]
    if want == "auto":
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() == 0:
                devices = ["cpu"]
        except Exception:
            devices = ["cpu"]

    duration = job["video"]["duration"] or 1
    last_exc: Exception | None = None
    for device in devices:
        try:
            update(job, message=f"Loading speech model ({settings['whisper_model']}, {device})… first run downloads it")
            model = _load_whisper(settings["whisper_model"], device)
            update(job, message="Transcribing…")
            segments_iter, info = model.transcribe(
                str(audio),
                language=settings.get("language") or None,
                word_timestamps=True,
                vad_filter=True,
                beam_size=5,
            )
            segments = []
            started = time.time()
            for seg in segments_iter:
                words = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip()}
                         for w in (seg.words or []) if w.word.strip()]
                segments.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip(), "words": words})
                frac = min(seg.end / duration, 0.999)
                elapsed = time.time() - started
                eta = (elapsed / frac - elapsed) if frac > 0.02 else None
                eta_s = f" · ~{int(eta // 60)}m {int(eta % 60)}s left" if eta else ""
                update(job, progress=frac, message=f"Transcribing… {frac * 100:.0f}%{eta_s}")
            (d / "transcript.json").write_text(
                json.dumps({"language": info.language, "segments": segments}, ensure_ascii=False), encoding="utf-8")
            audio.unlink(missing_ok=True)
            return
        except Exception as exc:  # e.g. CUDA libraries missing → fall back to CPU
            last_exc = exc
            _whisper_cache.clear()
            if device != devices[-1]:
                update(job, message=f"GPU transcription failed ({exc}); falling back to CPU…")
    msg = f"Transcription failed: {last_exc}"
    if any(k in str(last_exc).lower() for k in ("forbidden", "connect", "huggingface", "resolve", "timed out", "ssl")):
        msg += ("\n→ The speech model is downloaded from huggingface.co the first time you use it. "
                "Check your internet connection / firewall and press Retry.")
    raise RuntimeError(msg)


def load_transcript(job_id: str) -> dict:
    return json.loads((job_dir(job_id) / "transcript.json").read_text(encoding="utf-8"))


def all_words(transcript: dict) -> list[dict]:
    return [w for s in transcript["segments"] for w in s.get("words", [])]


def snap_to_words(start: float, end: float, words: list[dict], settings: dict, duration: float) -> tuple[float, float]:
    """Move clip boundaries onto word edges so clips never start or end mid-word."""
    if not words:
        return max(0.0, start), min(duration, end)
    first = next((w for w in words if w["start"] >= start - 0.25), None)
    s = first["start"] if first else start
    prev = [w for w in words if w["end"] <= s]
    s = max(s - 0.12, prev[-1]["end"] if prev else 0.0, 0.0)

    max_s = float(settings.get("max_seconds", 60))
    end = min(end, s + max_s + 3)
    inside = [w for w in words if w["end"] <= end + 0.4 and w["start"] >= s]
    e = inside[-1]["end"] + 0.25 if inside else end
    nxt = [w for w in words if w["start"] >= e - 0.25]
    if nxt:
        e = min(e, nxt[0]["start"])
    return round(s, 2), round(min(e, duration), 2)


def _range(brief: dict, duration: float) -> tuple[float, float]:
    rng = brief.get("range") or [0.0, None]
    a = max(0.0, min(float(rng[0] or 0), duration))
    b = duration if rng[1] is None else max(a, min(float(rng[1]), duration))
    if b - a < 1:
        raise RuntimeError(f"The part you chose ({_mmss(a)}-{_mmss(b)}) is outside this video, which is {_mmss(duration)} long.")
    return a, b


def _stage_analyze(job: dict) -> None:
    brief = job.get("brief") or {}
    cuts = brief.get("mode") == "cuts"
    update(job, stage="analyzing", progress=0.0,
           message="Splitting into reels…" if cuts else "Claude is finding the best moments…")
    transcript = load_transcript(job["id"])
    settings = job["settings"]
    duration = job["video"]["duration"]
    a, b = _range(brief, duration)
    segments = [s for s in transcript["segments"] if s["end"] > a and s["start"] < b]
    words = all_words(transcript)
    if cuts:
        return _stage_cuts(job, segments, words, a, b)

    suggestions, model = claude_client.find_clips(segments, settings, job["source"].get("title", ""), brief)
    made_by = {"kind": "claude", "model": model, "label": claude_client.model_label(model)}

    clips: list[dict] = []
    for sug in sorted(suggestions, key=lambda c: -float(c.get("score", 0) or 0)):
        try:
            s, e = snap_to_words(float(sug["start"]), float(sug["end"]), words, settings, duration)
        except (KeyError, TypeError, ValueError):
            continue
        s, e = max(s, a), min(e, b)  # stay inside the part the creator chose
        s, e, peak = place_peak(s, e, sug.get("peak"), transcript["segments"], words, settings, a, b)
        sug = {**sug, "peak": peak}
        if e - s < 3:
            continue
        if any(s < c["end"] and e > c["start"] for c in clips):  # overlap with a better clip
            continue
        if any(_overlap(s, e, c) > 0.5 for c in _exported(job)):  # already exported from an earlier set
            continue
        clips.append(new_clip(sug, s, e, made_by))
        if len(clips) >= int(settings["num_clips"]):
            break
    if not clips:
        raise RuntimeError("Claude's suggestions could not be matched to the transcript")
    _merge_new(job, clips)


def place_peak(s: float, e: float, peak, segments: list[dict], words: list[dict], settings: dict,
               lo: float, hi: float) -> tuple[float, float, float | None]:
    """Make sure a reel builds up to its peak instead of opening on it (which leaves the rest with nothing).
    If the peak is in the first ~30 % (or first 5 s), start earlier at a sentence start; if the reel ends
    right on the peak, keep a little payoff. Returns (start, end, peak or None if Claude gave none)."""
    try:
        peak = float(peak)
    except (TypeError, ValueError):
        return s, e, None
    if not (s - 2 <= peak <= e + 2):  # nonsense peak: leave the reel as Claude made it
        return s, e, None
    peak = min(max(peak, s), e)
    length = e - s
    max_s = float(settings.get("max_seconds", 60))
    if peak - s < min(max(5.0, 0.3 * length), 0.5 * length):
        target = max(lo, peak - max(6.0, 0.35 * length))
        # the latest sentence start at or before the target (within 8 s), else the target on a word edge
        starts = [g["start"] for g in segments if target - 8 <= g["start"] <= target]
        new_s = max(starts) if starts else _word_edge(target, words, "start")
        s = max(lo, round(new_s - 0.1, 2))
    if e - peak < 2.5:  # no payoff after the peak: run on to the end of the next sentence (a few seconds)
        ends = [g["end"] for g in segments if peak + 1.5 <= g["end"] <= peak + 7]
        if ends:
            e = min(hi, round(min(ends) + 0.25, 2))
    if e - s > max_s + 3:  # too long now: trim the end, but keep at least 3 s after the peak
        cut = max(peak + 3, s + max_s)
        ends = [w["end"] for w in words if peak + 2 <= w["end"] <= cut]
        e = round(max(ends) + 0.2, 2) if ends else round(cut, 2)
    return s, e, round(peak, 2)


def _exported(job: dict) -> list[dict]:
    return [c for c in job.get("clips", []) if c.get("file")]


def _overlap(s: float, e: float, c: dict) -> float:
    """How much of the shorter of [s, e] and clip c they share (0-1)."""
    shared = min(e, c["end"]) - max(s, c["start"])
    return max(0.0, shared) / max(0.01, min(e - s, c["end"] - c["start"]))


def _merge_new(job: dict, new: list[dict]) -> None:
    """A new set of suggestions replaces the drafts. Exported reels stay, so nothing finished is ever lost."""
    kept = _exported(job)
    nxt = max([c["index"] for c in kept] + [0]) + 1
    for c in sorted(new, key=lambda c: c["start"]):
        c["index"] = nxt
        nxt += 1
    update(job, clips=sorted(kept + new, key=lambda c: c["start"]), find_clips=False)


def _word_edge(t: float, words: list[dict], side: str) -> float:
    """If `t` falls inside a word, move it to that word's edge so a reel never starts or ends mid-word."""
    for w in words:
        if w["start"] < t < w["end"]:
            return w["start"] - 0.05 if side == "start" else w["end"] + 0.1
    return t


def plan_cuts(segments: list[dict], words: list[dict], a: float, b: float,
              min_s: float, max_s: float, duration: float) -> list[tuple[float, float]]:
    """Split [a, b] into consecutive reels of at most max_s seconds, as even as possible,
    cutting between sentences where it can (else between words, else exactly on time)."""
    a = max(0.0, _word_edge(a, words, "start"))
    b = min(duration, _word_edge(b, words, "end"))
    sentence_gaps = []
    for s, nxt in zip(segments, segments[1:]):
        t = (s["end"] + nxt["start"]) / 2 if nxt["start"] > s["end"] else s["end"]
        t = min(t, s["end"] + 0.3)
        if a < t < b:
            sentence_gaps.append((round(t, 2), s["text"].rstrip().endswith((".", "?", "!", "…"))))
    word_gaps = [round((w1["end"] + w2["start"]) / 2, 2) for w1, w2 in zip(words, words[1:]) if a < w1["end"] < b]

    parts: list[tuple[float, float]] = []
    cur = a
    while b - cur > max_s:
        left = b - cur
        k = -(-left // max_s)                                  # parts still needed (ceil)
        ideal = cur + left / k
        lo = max(cur + min(min_s, max_s / 2), b - (k - 1) * max_s)  # leave no more than fits in k-1 parts
        hi = cur + max_s
        options = [(abs(t - ideal) + (0 if full_stop else 4), t) for t, full_stop in sentence_gaps if lo <= t <= hi]
        if not options:
            options = [(abs(t - ideal), t) for t in word_gaps if lo <= t <= hi]
        cut = min(options)[1] if options else round(min(max(ideal, lo), hi), 2)
        parts.append((round(cur, 2), cut))
        cur = cut
    parts.append((round(cur, 2), round(b, 2)))
    return parts


def _stage_cuts(job: dict, segments: list[dict], words: list[dict], a: float, b: float) -> None:
    """'Cuts' mode: turn the chosen part of the video into consecutive reels, as they are.
    ClipForge picks the cut points; Claude only writes the titles, hooks and captions."""
    settings, brief = job["settings"], job.get("brief") or {}
    title = job["source"].get("title", "")
    parts = plan_cuts(segments, words, a, b, settings["min_seconds"], settings["max_seconds"], job["video"]["duration"])
    part_segs = [[s for s in segments if s["end"] > p0 + 0.1 and s["start"] < p1 - 0.1] for p0, p1 in parts]
    texts: list[dict] = [{} for _ in parts]
    model = None
    if any(part_segs):
        update(job, message=f"Claude is writing titles for {len(parts)} reel{'s' if len(parts) != 1 else ''}…")
        texts, model = claude_client.describe_cuts(part_segs, settings, title, brief)
    made_by = {"kind": "cuts", "model": model, "label": claude_client.model_label(model) if model else None}
    n = len(parts)
    clips = []
    for i, ((s, e), t) in enumerate(zip(parts, texts), 1):
        fallback = f"{title or 'Part'} ({i}/{n})" if n > 1 else (title or "Reel")
        clip = new_clip({**t, "title": t.get("title") or fallback, "score": 0,
                         "reason": f"Part {i} of {n}" if n > 1 else ""}, s, e, made_by)
        clip["part"] = [i, n]
        clips.append(clip)
    _merge_new(job, clips)


HAND_MADE = {"kind": "you", "model": None, "label": "You"}


def made_by_text(clip: dict) -> str:
    """'Generated by Claude Sonnet 5.5', plus ', edited by you' once the owner changed it. Empty for old clips."""
    mb = clip.get("made_by")
    if not mb:
        return ""
    if mb.get("kind") == "you":
        text = "Added by hand"
    elif mb.get("kind") == "cuts":
        text = "Cut by ClipForge" + (f", text by {mb['label']}" if mb.get("label") else "")
    else:
        text = f"Generated by {mb.get('label') or 'Claude'}" + (" on request" if mb.get("via") == "ask" else "")
    if clip.get("revised_by"):
        text += f", changed by {clip['revised_by']} on request"
    if clip.get("edited"):
        text += ", edited by you"
    return text


def norm_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = tags.split()
    if not isinstance(tags, list):
        return []
    return [t if t.startswith("#") else f"#{t}" for t in (str(x).strip().replace(" ", "") for x in tags) if t][:10]


def _num_or_none(v, lo: float, hi: float) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, 2) if lo <= v <= hi else None


def new_clip(data: dict, start: float, end: float, made_by: dict | None = None) -> dict:
    tags = norm_tags(data.get("hashtags") or [])
    try:
        score = int(float(data.get("score", 0) or 0))
    except (TypeError, ValueError):
        score = 0
    return {
        "id": uuid.uuid4().hex[:8],
        "index": 0,
        "start": start,
        "end": end,
        "title": str(data.get("title") or "").strip()[:100],
        "hook": str(data.get("hook") or "").strip()[:80],
        "caption": str(data.get("caption") or "").strip(),
        "hashtags": tags,
        "score": score,
        "reason": str(data.get("reason") or "").strip(),
        "peak": _num_or_none(data.get("peak"), start, end),  # the moment the reel builds up to (shown on the timeline)
        "made_by": dict(made_by) if made_by else None,  # who picked this moment: a Claude model, or you
        "edited": False,       # True once you change its times or text
        "crop_mode": None,     # None = use the Settings default
        "show_hook": True,
        "status": "draft",     # draft → pending → rendering → ready (exported) | error
        "file": None,
        "thumb": None,
        "error": None,
        "rendered_at": None,
        "export_spec": None,   # what the exported file was made from, to spot "changed since export"
        # what was first suggested, for "Reset to suggestion"
        "suggested": {"start": start, "end": end, "title": str(data.get("title") or "").strip()[:100],
                      "hook": str(data.get("hook") or "").strip()[:80], "caption": str(data.get("caption") or "").strip(),
                      "hashtags": tags},
    }


def export_key(clip: dict) -> dict:
    """The parts of a clip that change the video file (title/caption/hashtags only change the post text)."""
    return {"start": round(float(clip["start"]), 2), "end": round(float(clip["end"]), 2),
            "hook": (clip.get("hook") or "") if clip.get("show_hook", True) else "", "crop_mode": clip.get("crop_mode")}


def export_state(clip: dict) -> str:
    """draft | exporting | exported | changed (exported, then edited) | error"""
    if clip.get("status") in ("pending", "rendering"):
        return "exporting"
    if clip.get("file"):
        return "exported" if clip.get("export_spec") == export_key(clip) else "changed"
    return "error" if clip.get("status") == "error" else "draft"


# ----------------------------------------------------------------------------- rendering (export)

def slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:50] or "clip"


def render_clip(job: dict, clip: dict) -> None:
    """Export one reel: render the 1080×1920 file from the clip as it is now."""
    d = job_dir(job["id"]) / "clips"
    d.mkdir(parents=True, exist_ok=True)
    name = f"clip_{clip['index']:02d}"
    out = d / f"{name}.mp4"
    key = export_key(clip)
    with _lock:
        clip.update(status="rendering", error=None, progress=0.0)
        save(job)
    try:
        def progress(frac: float) -> None:  # shown on the Export button
            with _lock:
                clip["progress"] = round(frac, 2)
                save(job)
        warning, layout_note = _encode(job, clip, d, name, progress)
        dur = key["end"] - key["start"]
        thumb = d / f"{name}.jpg"
        tools.run([tools.ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{min(1.5, dur / 2):.2f}", "-i", out.name,
                   "-frames:v", "1", "-vf", "scale=360:-2", thumb.name], cwd=d)
        _write_post_text(d / f"{name}.txt", clip)
        with _lock:
            clip.update(status="ready", file=str(out), thumb=str(thumb) if thumb.exists() else None,
                        rendered_at=time.time(), error=warning, layout_note=layout_note, export_spec=key, progress=None)
            save(job)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with _lock:
            clip.update(status="error", error=str(exc), progress=None)
            save(job)


# whole frame, centered on a blurred, zoomed copy of itself (inputs [a] and [b] = the same video)
FIT_GRAPH = ("[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:2,eq=brightness=-0.06[bg];"
             "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")


def _encode(job: dict, clip: dict, d: Path, name: str, progress=None) -> tuple[str | None, str | None]:
    """Render `clip` to d/<name>.mp4 (through a temp file). Returns (warning, layout note)."""
    settings = job["settings"]
    video = job["video"]
    out = d / f"{name}.mp4"
    tmp_out = d / f"{name}.rendering.mp4"
    ass = d / f"{name}.ass"
    try:
        start, end = float(clip["start"]), float(clip["end"])
        dur = end - start
        words = all_words(load_transcript(job["id"]))
        hook = (clip.get("hook") or "") if clip.get("show_hook", True) else ""
        captions.build_ass(words, start, end, settings, hook=hook, out_path=ass)

        mode = clip.get("crop_mode") or settings.get("crop_mode", "smart")
        plan = framing_plan(job, start, end, mode)  # the same plan the editor's live preview shows
        shots = plan["shots"]
        close = [s for s in shots if s["layout"] == "crop"]
        layout_note = (f"{len(close)} of {len(shots)} shot{'s' if len(shots) != 1 else ''} close-up"
                       if mode == "smart" and len(shots) > 0 else None)
        closeup = (f"crop=w={plan['crop_w']}:h={plan['crop_h']}:x='{crop.keys_to_expr(plan['keys'])}':y=0,"
                   "scale=1080:1920:flags=lanczos,setsar=1") if close else ""
        if not close:
            vf = "[0:v]split[a][b];" + FIT_GRAPH
        elif len(close) == len(shots):
            vf = f"[0:v]{closeup}"
        else:  # render both layouts and show the close-up only during its shots
            enable = "+".join(f"between(t,{s['t0']:.3f},{s['t1'] - 0.002:.3f})" for s in close)
            vf = ("[0:v]split=3[a][b][c];" + FIT_GRAPH + "[wide];"
                  f"[c]{closeup}[close];[wide][close]overlay=0:0:enable='{enable}'")

        use_subs = ass.read_text(encoding="utf-8").count("Dialogue:") > 0
        graph = vf + (f",subtitles=filename={ass.name}{_fontsdir_opt(d)}" if use_subs else "") + "[v]"
        credit = made_by_text(clip)
        cmd = [tools.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start:.3f}", "-i", video["path"], "-t", f"{dur:.3f}",
               "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
               "-map_metadata", "-1",  # don't carry over the source video's tags
               "-metadata", f"title={clip.get('title') or ''}",
               # invisible to viewers; shows in file properties / ffprobe
               "-metadata", f"comment=ClipForge{' · ' + credit if credit else ''}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", str(settings.get("video_crf", 20)),
               "-pix_fmt", "yuv420p", "-r", str(min(60, round(video.get("fps") or 30))),
               "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
               "-movflags", "+faststart", tmp_out.name]
        r = (tools.run_ffmpeg_progress(cmd, cwd=d, duration=dur, on_progress=progress) if progress
             else tools.run(cmd, cwd=d))
        warning = None
        if r.returncode != 0 and use_subs:
            # most common cause: ffmpeg build without libass / font issue → render without captions
            cmd[cmd.index("-filter_complex") + 1] = vf + "[v]"
            r2 = tools.run(cmd, cwd=d)
            if r2.returncode == 0:
                warning = "Captions could not be burned in (ffmpeg error): " + r.stderr.strip()[-300:]
                r = r2
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-800:] or "ffmpeg failed")
        tmp_out.replace(out)
        return warning, layout_note
    except Exception:
        tmp_out.unlink(missing_ok=True)
        raise


# ----------------------------------------------------------------------------- framing plans + the one-time scan

_prep_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prepare")  # one scan at a time
_analysis: dict[str, dict] = {}
_plans: dict[tuple, dict] = {}


def load_analysis(job: dict) -> dict | None:
    jid = job["id"]
    if jid not in _analysis:
        f = job_dir(jid) / "analysis.json"
        if not f.exists():
            return None
        try:
            _analysis[jid] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
    return _analysis[jid]


def framing_plan(job: dict, start: float, end: float, mode: str | None = None, preview: bool = False) -> dict:
    """How the reel [start, end] is framed (see crop.py). Instant once the video has been scanned.
    For the editor's preview while the scan is still running, a provisional whole-frame plan: working the framing
    out separately would decode the same video a second time and slow everything down."""
    v = job["video"]
    mode = mode if mode in ("smart", "track", "center", "fit") else load_settings().get("crop_mode", "smart")
    an = load_analysis(job)
    scanning = (job.get("prep") or {}).get("status") in ("queued", "running")
    if preview and not an and scanning and mode in ("smart", "track"):
        plan = crop.make_plan("fit", round(start, 2), round(end, 2), v["width"], v["height"])
        return {**plan, "mode": mode, "source": "provisional"}
    key = (job["id"], round(start, 2), round(end, 2), mode, bool(an))
    if key not in _plans:
        if len(_plans) > 200:
            _plans.clear()
        _plans[key] = crop.make_plan(mode, round(start, 2), round(end, 2), v["width"], v["height"], an, v["path"])
    return _plans[key]


def ensure_prepared(job: dict) -> None:
    """Thumbnails for the timeline + the one-time face/cut scan, in the background."""
    if not job.get("video") or (load_analysis(job) and (job_dir(job["id"]) / "thumbs.json").exists()):
        return
    with _lock:
        if (job.get("prep") or {}).get("status") in ("queued", "running"):
            return
        job["prep"] = {"status": "queued", "progress": 0.0}
        save(job)
    _prep_worker.submit(_prepare, job["id"])


def _prepare(job_id: str) -> None:
    job = _jobs.get(job_id)
    if not job or not job.get("video"):
        return
    d = job_dir(job_id)
    try:
        update(job, prep={"status": "running", "progress": 0.0})
        v = job["video"]
        # timeline thumbnails come out of the same pass, as it goes (the editor shows the ones that exist)
        thumbs = d / "thumbs"
        shutil.rmtree(thumbs, ignore_errors=True)
        thumbs.mkdir(parents=True, exist_ok=True)
        plan = crop.thumb_plan(v["duration"], v["width"], v["height"])
        (d / "thumbs.json").write_text(json.dumps({**plan, "version": int(time.time())}), encoding="utf-8")

        started = time.time()

        def progress(frac: float) -> None:
            left = (time.time() - started) / frac * (1 - frac) if frac > 0.02 else None
            update(job, prep={"status": "running", "progress": round(frac, 3), "eta": round(left) if left else None})
        # the light editing copy comes out of the same pass too (unless there already is one)
        proxy_tmp = d / "proxy.tmp.mp4" if not (d / "proxy.mp4").exists() else None
        result = crop.analyze_video(v["path"], v["duration"], v["width"], v["height"], progress,
                                    str(proxy_tmp) if proxy_tmp else None, str(thumbs))
        tmp = d / "analysis.tmp.json"
        tmp.write_text(json.dumps(result), encoding="utf-8")
        tmp.replace(d / "analysis.json")
        _analysis[job_id] = result
        if proxy_tmp and proxy_tmp.exists() and proxy_tmp.stat().st_size > 0:
            proxy_tmp.replace(d / "proxy.mp4")
        update(job, prep={"status": "done", "progress": 1.0},
               proxy="ready" if (d / "proxy.mp4").exists() else job.get("proxy"))
    except Exception as exc:  # noqa: BLE001 - the editor still works without it (slower framing)
        traceback.print_exc()
        update(job, prep={"status": "error", "progress": 0.0, "error": str(exc)[-300:]})


# ----------------------------------------------------------------------------- editor: thumbnails + playable copy

_building: set[str] = set()


def thumbs_info(job: dict) -> dict | None:
    """Timeline thumbnails (thumbs/00001.jpg …, one every `interval` s): how many exist so far, or None before
    the one-time scan has started. They appear while the scan runs."""
    d = job_dir(job["id"])
    meta = d / "thumbs.json"
    if not meta.exists():
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None
    done = (job.get("prep") or {}).get("status") == "done"
    files = sum(1 for _ in (d / "thumbs").glob("*.jpg")) if (d / "thumbs").is_dir() else 0
    # the newest file may still be being written while the scan runs
    return {**info, "available": files if done else max(0, files - 1), "complete": done}


def _in_background(key: str, fn, *args) -> None:
    with _lock:
        if key in _building:
            return
        _building.add(key)

    def run():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            with _lock:
                _building.discard(key)
    threading.Thread(target=run, daemon=True, name=key).start()


def start_proxy(job: dict) -> None:
    """The browser can't play the original (HEVC, AV1 without support, MKV…): make the editing copy now.
    The one-time scan makes it anyway, so while that runs there's nothing extra to do."""
    if (job_dir(job["id"]) / "proxy.mp4").exists():
        with _lock:
            job["proxy"] = "ready"
            save(job)
        return
    with _lock:
        job["proxy"] = "building"
        save(job)
    if (job.get("prep") or {}).get("status") in ("queued", "running"):
        return
    _in_background(f"proxy:{job['id']}", _build_proxy, job)


def _build_proxy(job: dict) -> None:
    d = job_dir(job["id"])
    tmp = d / "proxy.tmp.mp4"
    r = tools.run(crop.background_cmd([
        tools.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-threads", str(crop.scan_threads()),
        "-i", job["video"]["path"], "-vf", "fps=30,scale=-2:540", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(tmp)]),
        timeout=6 * 3600)
    ok = r.returncode == 0 and tmp.exists()
    if ok:
        tmp.replace(d / "proxy.mp4")
    else:
        tmp.unlink(missing_ok=True)
    with _lock:
        job["proxy"] = "ready" if ok else "error"
        save(job)


def _fontsdir_opt(cwd: Path) -> str:
    """Point libass at the bundled fonts with a relative path (avoids Windows drive-letter escaping)."""
    if not FONTS_DIR.is_dir():
        return ""
    try:
        rel = os.path.relpath(FONTS_DIR, cwd).replace("\\", "/")
    except ValueError:  # different drive on Windows
        return ""
    return f":fontsdir={rel}"


def resolve_video_path(text: str) -> Path | None:
    """Find a local video from what the user typed: a full path, or a file name in the videos folder.

    In Docker, Windows paths like C:\\ClipForge\\videos\\talk.mp4 don't exist inside the container,
    so we fall back to looking the file name up in the mounted videos folder.
    """
    text = text.strip().strip('"').strip("'")
    if not text:
        return None
    p = Path(text)
    if p.is_file():
        return p
    for candidate in (VIDEOS_DIR / text, VIDEOS_DIR / PureWindowsPath(text).name, VIDEOS_DIR / PurePosixPath(text).name):
        if candidate.is_file():
            return candidate
    return None


def _write_post_text(path: Path, clip: dict) -> None:
    text = (f"TITLE:\n{clip['title']}\n\n"
            f"CAPTION:\n{clip['caption']}\n\n{' '.join(clip['hashtags'])}\n\n"
            f"(Source {clip['start']:.1f}s - {clip['end']:.1f}s"
            + (f", part {clip['part'][0]} of {clip['part'][1]}" if clip.get("part") and clip["part"][1] > 1 else "")
            + (f", score {clip['score']}" if clip.get("score") else "") + ")\n")
    credit = made_by_text(clip)
    if credit:
        text += f"({credit})\n"
    path.write_text(text, encoding="utf-8")


RENDER_KEYS = ("crop_mode", "captions", "caption_words", "caption_uppercase", "caption_font", "caption_size",
               "caption_color", "caption_highlight", "caption_position", "video_crf")


def queue_render(job: dict, clip: dict) -> None:
    """Re-render one clip, using the current caption/crop style from Settings."""
    current = load_settings()
    with _lock:
        for k in RENDER_KEYS:
            job["settings"][k] = current[k]
        clip["status"] = "pending"
        save(job)
    _render_worker.submit(render_clip, job, clip)


def reanalyze(job: dict, brief: dict | None = None) -> None:
    """Replace the clips with a fresh set. `brief` (what to make) defaults to what this video asked for last time."""
    current = load_settings()
    old = job.get("brief")
    brief = normalize_brief(brief if brief is not None else old, current)  # ValueError → shown to the user
    with _lock:
        job["brief"] = brief
        job["settings"] = apply_brief(current, brief)
        job["clips"] = _exported(job)  # drafts are replaced; exported reels stay
        job["find_clips"] = True
        kept = len(job["clips"])
        note = f"New set of reels: {brief_label(brief)}" + (f" (kept {kept} exported)" if kept else "")
        job.setdefault("chat", []).append({"role": "note", "text": note, "at": time.time()})
        save(job)
    start_job(job)


def remove_clip_files(clip: dict) -> None:
    for key in ("file", "thumb"):
        if clip.get(key):
            Path(clip[key]).unlink(missing_ok=True)
    if clip.get("file"):
        Path(clip["file"]).with_suffix(".txt").unlink(missing_ok=True)
        Path(clip["file"]).with_suffix(".ass").unlink(missing_ok=True)


# ----------------------------------------------------------------------------- "Ask ClipForge"

def start_ask(job: dict, request: str) -> None:
    """A free-form request about a finished video ("3 more funny ones", "make clip 2 shorter"). Runs on the job worker."""
    with _lock:  # check and claim in one step, so two quick clicks can't start two requests
        if job.get("status") in ("running", "queued"):
            raise RuntimeError("Wait until ClipForge has finished with this video")
        job.setdefault("chat", []).append({"role": "you", "text": request, "at": time.time()})
        update(job, status="running", stage="asking", progress=0.0, error=None, message="Claude is working on your request…")
    _job_worker.submit(_run_ask, job["id"], request)


def _run_ask(job_id: str, request: str) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    try:
        transcript = load_transcript(job_id)
        history = [m for m in job.get("chat", [])[:-1] if m.get("role") in ("you", "claude")]
        result, model = claude_client.ask_editor(transcript["segments"], job["clips"], history, request,
                                                 job["settings"], job["source"].get("title", ""), job.get("brief"))
        label = claude_client.model_label(model)
        changes = _apply_ask(job, transcript, result, model, label)
        reply = result["reply"] or changes or "Done."
        with _lock:
            job["chat"].append({"role": "claude", "text": reply, "changes": changes, "model": model,
                                "label": label, "at": time.time()})
        update(job, status="done", stage="done", progress=1.0, message=changes or "Claude answered")
    except Exception as exc:  # noqa: BLE001 - the video itself is fine; show the problem in the chat
        traceback.print_exc()
        with _lock:
            job["chat"].append({"role": "error", "text": str(exc), "at": time.time()})
        update(job, status="done", stage="done", progress=1.0, message="Claude couldn't do that")


def _apply_ask(job: dict, transcript: dict, result: dict, model: str | None, label: str) -> str:
    """Apply Claude's add / update / delete. Returns a short summary like 'Added 2 clips · changed #1'."""
    words = all_words(transcript)
    duration = job["video"]["duration"]
    loose = {**job["settings"], "max_seconds": 600}  # the creator may ask for any length here
    made_by = {"kind": "claude", "model": model, "label": label, "via": "ask"}

    def times(s, e) -> tuple[float, float] | None:
        try:
            s, e = snap_to_words(float(s), float(e), words, loose, duration)
        except (TypeError, ValueError):
            return None
        return (s, e) if e - s >= 1 else None

    def num(v) -> int | None:
        try:
            return int(str(v).lstrip("#"))
        except (TypeError, ValueError):
            return None

    deleted, changed, added, render, text_only = [], [], [], [], []
    with _lock:
        by_index = {c["index"]: c for c in job["clips"]}
        next_index = max(list(by_index) + [0]) + 1  # never reuse a deleted clip's number
        for n in result["delete"]:
            c = by_index.get(num(n))
            if c and c in job["clips"]:
                job["clips"].remove(c)
                deleted.append(c)
        for u in result["update"]:
            c = by_index.get(num(u.get("clip"))) if isinstance(u, dict) else None
            if not c or c in deleted:
                continue
            fields: dict = {}
            for k, limit in (("title", 100), ("hook", 80), ("caption", 2000)):
                if isinstance(u.get(k), str):
                    fields[k] = u[k].strip()[:limit]
            if "hashtags" in u:
                fields["hashtags"] = norm_tags(u["hashtags"])
            if "start" in u or "end" in u:
                t = times(u.get("start", c["start"]), u.get("end", c["end"]))
                if t:
                    fields["start"], fields["end"] = t
            if "peak" in u:
                fields["peak"] = _num_or_none(u["peak"], fields.get("start", c["start"]), fields.get("end", c["end"]))
            fields = {k: v for k, v in fields.items() if v != c.get(k)}
            if not fields:
                continue
            needs_render = any(k in fields for k in ("start", "end", "hook"))
            c.update(fields, revised_by=label)
            changed.append(c)
            (render if needs_render else text_only).append(c)
        for a in result["add"]:
            if not isinstance(a, dict):
                continue
            t = times(a.get("start"), a.get("end"))
            if not t:
                continue
            clip = new_clip(a, t[0], t[1], made_by)
            clip["index"] = next_index
            next_index += 1
            job["clips"].append(clip)
            added.append(clip)
        job["clips"].sort(key=lambda c: c["start"])
        save(job)

    # Nothing is rendered here: new and changed reels are drafts until exported.
    for c in deleted:
        remove_clip_files(c)
    for c in render + text_only:
        if c.get("file"):  # keep an exported reel's post text in step with its new title/caption
            _write_post_text(Path(c["file"]).with_suffix(".txt"), c)

    out = []
    if added:
        out.append(f"Added {len(added)} reel{'s' if len(added) != 1 else ''} ({', '.join('#' + str(c['index']) for c in added)})")
    if changed:
        out.append("changed " + ", ".join(f"#{c['index']}" for c in changed))
    if deleted:
        out.append("deleted " + ", ".join(f"#{c['index']}" for c in deleted))
    s = " · ".join(out)
    return s[:1].upper() + s[1:]
