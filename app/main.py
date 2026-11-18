"""ClipForge local web server. Start with start.bat, then open http://localhost:8765"""
from __future__ import annotations

import io
import os
import subprocess
import zipfile
from pathlib import Path

import json

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import pipeline, tools
from .config import FONTS_DIR, IN_DOCKER, STATIC_DIR, load_settings, save_settings
from . import claude_client

app = FastAPI(title="ClipForge")
pipeline.load_all()


# ----------------------------------------------------------------------------- helpers

def _job(job_id: str) -> dict:
    job = pipeline.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def _clip(job: dict, clip_id: str) -> dict:
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not clip:
        raise HTTPException(404, "Clip not found")
    return clip


def _public(job: dict) -> dict:
    """Job as sent to the browser (no local file paths needed there)."""
    with pipeline._lock:  # the worker threads modify jobs while we read them
        import copy
        job = copy.deepcopy(job)
    out = {k: v for k, v in job.items() if k not in ("settings",)}
    out["clips"] = []
    for c in job.get("clips", []):
        pc = dict(c)
        v = int(c.get("rendered_at") or 0)
        pc["video_url"] = f"/media/{job['id']}/{Path(c['file']).name}?v={v}" if c.get("file") else None
        pc["thumb_url"] = f"/media/{job['id']}/{Path(c['thumb']).name}?v={v}" if c.get("thumb") else None
        pc["made_by_text"] = pipeline.made_by_text(c)
        pc["export_state"] = pipeline.export_state(c)  # draft | exporting | exported | changed | error
        pc.pop("file", None)
        pc.pop("thumb", None)
        out["clips"].append(pc)
    out["has_source"] = bool(job.get("video"))
    out["brief_label"] = pipeline.brief_label(job.get("brief"))
    out["has_transcript"] = (pipeline.job_dir(job["id"]) / "transcript.json").exists()
    return out


class Brief(BaseModel):
    """What to make from a video. Anything left out comes from Settings."""
    mode: str | None = None
    instructions: str | None = None
    num_clips: float | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None
    range_start: float | None = None
    range_end: float | None = None


def _brief(b: Brief | dict | None) -> dict | None:
    if b is None:
        return None
    return b.model_dump(exclude_none=True) if isinstance(b, Brief) else b


# ----------------------------------------------------------------------------- api

@app.get("/api/health")
def health():
    return tools.health()


@app.post("/api/claude-test")
def claude_test():
    """Quick check that Claude Code is installed and logged in."""
    try:
        reply, model = claude_client.ask_claude("Reply with exactly: OK", load_settings())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
    msg = reply.strip()[:300]
    label = claude_client.model_label(model) if model else None
    if label:
        msg += f" (answered by {label})"
    return {"ok": "OK" in reply.upper(), "message": msg, "model": model, "model_label": label}


@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.post("/api/settings")
def set_settings(data: dict):
    return save_settings(data)


@app.get("/api/jobs")
def jobs():
    return [_public(j) for j in pipeline.list_jobs()]


class NewJob(BaseModel):
    url: str | None = None
    path: str | None = None
    brief: Brief | None = None


@app.post("/api/jobs")
def new_job(body: NewJob):
    brief = _brief(body.brief)
    try:
        if body.url and body.url.strip().lower().startswith(("http://", "https://")):
            job = pipeline.create_job("url", body.url.strip(), brief=brief)
        elif body.path:
            p = pipeline.resolve_video_path(body.path)
            if not p:
                where = "the videos folder next to docker-compose.yml" if IN_DOCKER else "that location or the videos folder"
                raise HTTPException(400, f"Couldn't find {body.path.strip()} in {where}. Put the file there and type its name.")
            job = pipeline.create_job("path", str(p), p.stem, brief=brief)
        else:
            raise HTTPException(400, "Paste a video link (https://…) or a file path")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    pipeline.start_job(job)
    return _public(job)


@app.post("/api/jobs/upload")
async def upload(file: UploadFile = File(...), brief: str | None = Form(None)):
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    if ext not in pipeline.VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported file type {ext}")
    try:
        b = _brief(Brief(**json.loads(brief))) if brief else None
        job = pipeline.create_job("file", "", Path(file.filename or "video").stem, brief=b)
    except (ValueError, TypeError) as exc:  # bad JSON, bad fields, or a brief that doesn't make sense
        raise HTTPException(400, str(exc)) from None
    dest = pipeline.job_dir(job["id"]) / f"source{ext}"
    with dest.open("wb") as f:
        while chunk := await file.read(4 * 1024 * 1024):
            f.write(chunk)
    job["source"]["value"] = str(dest)
    pipeline.start_job(job)
    return _public(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _public(_job(job_id))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    _job(job_id)
    pipeline.delete_job(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str):
    job = _job(job_id)
    pipeline.start_job(job)
    return _public(job)


@app.post("/api/jobs/{job_id}/reanalyze")
def reanalyze(job_id: str, body: Brief | None = None):
    job = _job(job_id)
    if job["status"] in ("running", "queued"):
        raise HTTPException(409, "This video is still being processed")
    try:
        pipeline.reanalyze(job, _brief(body))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return _public(job)


class Ask(BaseModel):
    message: str


@app.post("/api/jobs/{job_id}/ask")
def ask(job_id: str, body: Ask):
    """'Ask ClipForge': a free-form request about a finished video."""
    job = _job(job_id)
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Type what you'd like Claude to do")
    if len(text) > 4000:
        raise HTTPException(400, "That's too long: keep it under 4000 characters")
    if job["status"] in ("running", "queued"):
        raise HTTPException(409, "Wait until ClipForge has finished with this video")
    if not job.get("video") or not (pipeline.job_dir(job_id) / "transcript.json").exists():
        raise HTTPException(409, "This video has no transcript yet: press Retry first")
    try:
        pipeline.start_ask(job, text)
    except RuntimeError as exc:  # another request got there first
        raise HTTPException(409, str(exc)) from None
    return _public(job)


@app.get("/api/jobs/{job_id}/transcript")
def transcript(job_id: str):
    _job(job_id)
    try:
        return pipeline.load_transcript(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "No transcript yet")


@app.get("/api/jobs/{job_id}/framing")
def framing(job_id: str, start: float, end: float, mode: str | None = None):
    """How a reel is framed (shots + crop keyframes), for the editor's live phone preview.
    Instant once the video has been scanned; a few seconds before that."""
    job = _job(job_id)
    if not job.get("video"):
        raise HTTPException(409, "No source video")
    duration = job["video"].get("duration") or 1e9
    start, end = max(0.0, start), min(duration, end)
    if end - start < 0.5:
        raise HTTPException(400, "Too short")
    if end - start > 900 and not pipeline.load_analysis(job):
        raise HTTPException(409, "Still scanning the video; long reels get their framing when that's done")
    return pipeline.framing_plan(job, start, end, mode, preview=True)


@app.get("/api/jobs/{job_id}/thumbs")
def thumbs(job_id: str):
    """Editor timeline thumbnails: {"base": "/media/<id>/thumbs/", interval, count, available, complete, …}.
    They appear while the one-time scan runs; opening a video also starts that scan for older videos."""
    job = _job(job_id)
    if not job.get("video"):
        raise HTTPException(409, "No source video")
    pipeline.ensure_prepared(job)
    info = pipeline.thumbs_info(job)
    if not info:
        return {"status": "building", "available": 0, "complete": False}
    return {"status": "ready", "base": f"/media/{job_id}/thumbs/", **info}


@app.post("/api/jobs/{job_id}/proxy")
def make_proxy(job_id: str):
    """Editor: build a browser-playable copy when the browser can't play the original."""
    job = _job(job_id)
    if not job.get("video"):
        raise HTTPException(409, "No source video")
    pipeline.start_proxy(job)
    return {"proxy": job.get("proxy")}


class ClipEdit(BaseModel):
    start: float | None = None
    end: float | None = None
    title: str | None = None
    hook: str | None = None
    caption: str | None = None
    hashtags: list[str] | str | None = None
    crop_mode: str | None = None
    show_hook: bool | None = None
    look: dict | None = None  # partial: merged into the reel's look; a key set to null goes back to Settings
    rerender: bool = False


@app.patch("/api/jobs/{job_id}/clips/{clip_id}")
def edit_clip(job_id: str, clip_id: str, body: ClipEdit):
    job = _job(job_id)
    clip = _clip(job, clip_id)
    data = body.model_dump(exclude_none=True)
    rerender = data.pop("rerender", False)
    if "hashtags" in data:
        tags = data["hashtags"].split() if isinstance(data["hashtags"], str) else data["hashtags"]
        data["hashtags"] = [t if t.startswith("#") else f"#{t}" for t in (x.strip() for x in tags) if t]
    if "crop_mode" in data and data["crop_mode"] not in ("smart", "track", "center", "fit"):
        data["crop_mode"] = None
    # The UI shows times to 0.1 s and sends them back rounded; a difference that small isn't an edit,
    # so keep the exact (word-snapped) time instead of nudging the clip by up to 0.05 s.
    for k in ("start", "end"):
        if k in data and abs(data[k] - clip[k]) < 0.051:
            data[k] = clip[k]
    duration = (job.get("video") or {}).get("duration") or 1e9
    start = max(0.0, data.get("start", clip["start"]))
    end = min(duration, data.get("end", clip["end"]))
    if end - start < 1:
        raise HTTPException(400, "A clip must be at least 1 second long")
    data["start"], data["end"] = round(start, 2), round(end, 2)
    if "look" in data:
        data["look"] = pipeline.normalize_look({**(clip.get("look") or {}), **data["look"]})
    with pipeline._lock:
        # The UI sends every field on Apply, so only count real changes to what Claude wrote.
        if any(k in data and data[k] != clip.get(k) for k in ("start", "end", "title", "hook", "caption", "hashtags")):
            data["edited"] = True
        clip.update(data)
        pipeline.save(job)
    if rerender:
        pipeline.queue_render(job, clip)
    elif clip.get("file"):
        pipeline._write_post_text(Path(clip["file"]).with_suffix(".txt"), clip)
    return _public(job)


class LookAll(BaseModel):
    look: dict
    show_hook: bool | None = None


@app.post("/api/jobs/{job_id}/look-all")
def look_all(job_id: str, body: LookAll):
    """Give every reel of this video the same look (from the editor's "Use for all reels")."""
    job = _job(job_id)
    look = pipeline.normalize_look(body.look)
    with pipeline._lock:
        for c in job["clips"]:
            c["look"] = dict(look)
            if body.show_hook is not None:
                c["show_hook"] = body.show_hook
        pipeline.save(job)
    return _public(job)


@app.post("/api/jobs/{job_id}/clips/{clip_id}/write-text")
def write_text(job_id: str, clip_id: str):
    """Claude writes the post text (title, caption, hashtags) for one reel, e.g. one made by hand.
    The on-screen hook is left alone, so a generated file stays up to date."""
    job = _job(job_id)
    clip = _clip(job, clip_id)
    try:
        transcript = pipeline.load_transcript(job_id)
    except FileNotFoundError:
        raise HTTPException(409, "This video has no transcript yet") from None
    segs = [s for s in transcript["segments"] if s["end"] > clip["start"] and s["start"] < clip["end"]]
    try:
        data, model = claude_client.write_post_text(segs, job["settings"], job["source"].get("title", ""), job.get("brief"))
    except Exception as exc:  # noqa: BLE001 - shown to the owner
        raise HTTPException(502, f"Claude couldn't write it: {exc}") from None
    fields = {k: str(data.get(k) or "").strip() for k in ("title", "caption")}
    fields["title"] = fields["title"][:100] or clip.get("title", "")
    fields["hashtags"] = pipeline.norm_tags(data.get("hashtags") or [])
    with pipeline._lock:
        clip.update(fields, text_by=claude_client.model_label(model))
        pipeline.save(job)
    if clip.get("file"):  # keep a generated reel's post text file in step
        pipeline._write_post_text(Path(clip["file"]).with_suffix(".txt"), clip)
    return _public(job)


@app.post("/api/jobs/{job_id}/clips/{clip_id}/render")
def render(job_id: str, clip_id: str):
    job = _job(job_id)
    pipeline.queue_render(job, _clip(job, clip_id))
    return _public(job)


@app.post("/api/jobs/{job_id}/render-all")
def render_all(job_id: str, body: dict | None = None):
    """Export all: every reel that isn't exported yet or changed since. {"all": true} re-exports everything
    (e.g. after changing the caption style)."""
    job = _job(job_id)
    everything = bool((body or {}).get("all"))
    for c in job["clips"]:
        state = pipeline.export_state(c)
        if state == "exporting" or (state == "exported" and not everything):
            continue
        pipeline.queue_render(job, c)
    return _public(job)


@app.delete("/api/jobs/{job_id}/clips/{clip_id}")
def delete_clip(job_id: str, clip_id: str):
    job = _job(job_id)
    clip = _clip(job, clip_id)
    with pipeline._lock:
        job["clips"].remove(clip)
        pipeline.save(job)
    pipeline.remove_clip_files(clip)
    return _public(job)


class NewClip(BaseModel):
    start: float
    end: float
    title: str = ""


@app.post("/api/jobs/{job_id}/clips")
def add_clip(job_id: str, body: NewClip):
    job = _job(job_id)
    if not job.get("video") or not (pipeline.job_dir(job_id) / "transcript.json").exists():
        raise HTTPException(409, "Wait until the video is transcribed")
    duration = job["video"].get("duration") or 1e9
    start, end = max(0.0, body.start), min(duration, body.end)
    if end - start < 1:
        raise HTTPException(400, "A reel must be at least 1 second long")
    clip = pipeline.new_clip({"title": body.title or "New reel"}, round(start, 2), round(end, 2), pipeline.HAND_MADE)
    with pipeline._lock:
        clip["index"] = max([c["index"] for c in job["clips"]] + [0]) + 1
        job["clips"].append(clip)
        job["clips"].sort(key=lambda c: c["start"])
        pipeline.save(job)
    out = _public(job)
    out["new_clip_id"] = clip["id"]  # a draft: nothing is rendered until it's exported
    return out


@app.get("/api/jobs/{job_id}/clips/{clip_id}/download")
def download_clip(job_id: str, clip_id: str):
    job = _job(job_id)
    clip = _clip(job, clip_id)
    if clip.get("status") in ("pending", "rendering"):  # the file on disk is still the previous export
        raise HTTPException(409, "This reel is still exporting. Download it when it's done.")
    if not clip.get("file") or not Path(clip["file"]).exists():
        raise HTTPException(404, "This reel hasn't been exported yet")
    name = f"{clip['index']:02d}-{pipeline.slug(clip['title'])}.mp4"
    # never let the browser hand back an older export of the same reel
    return FileResponse(clip["file"], media_type="video/mp4", filename=name, headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{job_id}/zip")
def download_zip(job_id: str):
    job = _job(job_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for c in job["clips"]:
            if c.get("file") and Path(c["file"]).exists():
                base = f"{c['index']:02d}-{pipeline.slug(c['title'])}"
                z.write(c["file"], f"{base}.mp4")
                txt = Path(c["file"]).with_suffix(".txt")
                if txt.exists():
                    z.write(txt, f"{base}.txt")
    buf.seek(0)
    title = pipeline.slug(job["source"].get("title") or job_id)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{title}-reels.zip"',
                                      "Cache-Control": "no-store"})


@app.post("/api/jobs/{job_id}/open-folder")
def open_folder(job_id: str):
    _job(job_id)
    folder = pipeline.job_dir(job_id) / "clips"
    folder.mkdir(parents=True, exist_ok=True)
    if IN_DOCKER:  # the container can't open a window on your desktop; tell the UI where the files are instead
        return {"path": f"data\\jobs\\{job_id}\\clips", "docker": True}
    if os.name == "nt":
        os.startfile(folder)  # type: ignore[attr-defined]
    elif tools.find("open"):
        subprocess.Popen(["open", str(folder)])
    elif tools.find("xdg-open"):
        subprocess.Popen(["xdg-open", str(folder)])
    return {"path": str(folder)}


# ----------------------------------------------------------------------------- media + ui

@app.get("/media/{job_id}/source")
def source_video(job_id: str):
    job = _job(job_id)
    if not job.get("video"):
        raise HTTPException(404, "No source video")
    return FileResponse(job["video"]["path"])


@app.get("/media/{job_id}/proxy")
def proxy_video(job_id: str):
    path = pipeline.job_dir(_job(job_id)["id"]) / "proxy.mp4"
    if not path.exists():
        raise HTTPException(404, "No playable copy yet")
    return FileResponse(path, media_type="video/mp4")


@app.get("/media/{job_id}/thumbs/{name}")
def thumb_image(job_id: str, name: str):
    if not (len(name) == 9 and name.endswith(".jpg") and name[:5].isdigit()):
        raise HTTPException(404)
    path = pipeline.job_dir(_job(job_id)["id"]) / "thumbs" / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/media/{job_id}/{name}")
def media(job_id: str, name: str):
    _job(job_id)
    path = (pipeline.job_dir(job_id) / "clips" / name).resolve()
    if path.parent != (pipeline.job_dir(job_id) / "clips").resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if FONTS_DIR.is_dir():  # the caption font, so the editor's live preview looks like the export
    app.mount("/fonts", StaticFiles(directory=FONTS_DIR), name="fonts")
