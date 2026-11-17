"""Locating and running external programs (ffmpeg, Claude Code, Deno)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

IS_WINDOWS = os.name == "nt"
# Prevent a console window from flashing up for every ffmpeg / claude call on Windows.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


def find(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if IS_WINDOWS:
        home = Path.home()
        candidates = {
            "claude": [home / ".local/bin/claude.exe", home / "AppData/Roaming/npm/claude.cmd"],
            "deno": [home / ".deno/bin/deno.exe"],
            "ffmpeg": [Path("C:/ffmpeg/bin/ffmpeg.exe")],
            "ffprobe": [Path("C:/ffmpeg/bin/ffprobe.exe")],
        }.get(name, [])
        for c in candidates:
            if c.exists():
                return str(c)
    return None


def run_ffmpeg_progress(cmd: list[str], *, cwd: str | Path | None, duration: float, on_progress) -> subprocess.CompletedProcess:
    """Run an ffmpeg command and report how far it is (0-1) through `on_progress`, from ffmpeg's `-progress` output.
    `cmd` must be a full ffmpeg command; `-progress pipe:1 -nostats` is added here."""
    import threading
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    err: list[str] = []
    reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    reader.start()
    last = -1.0
    for line in proc.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):  # both are microseconds
            try:
                frac = min(0.99, int(line.split("=", 1)[1]) / 1e6 / max(duration, 0.1))
            except ValueError:
                continue
            if frac - last >= 0.03:
                last = frac
                on_progress(frac)
    proc.wait()
    reader.join(timeout=5)
    return subprocess.CompletedProcess(cmd, proc.returncode, "", "".join(err))


def popen_flags() -> dict:
    """Extra Popen arguments for long-running tools we stream from (no console window on Windows)."""
    return {"creationflags": NO_WINDOW}


def run(cmd: list[str], *, cwd: str | Path | None = None, input_text: str | None = None,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
    )


def ffmpeg() -> str:
    path = find("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found. Install it with:  winget install Gyan.FFmpeg  (then restart ClipForge)")
    return path


def ffprobe() -> str:
    path = find("ffprobe")
    if not path:
        raise RuntimeError("ffprobe not found. Install ffmpeg with:  winget install Gyan.FFmpeg")
    return path


def probe(path: str | Path) -> dict:
    """Return width, height, duration and fps of a video file."""
    out = run([ffprobe(), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[-500:]}")
    info = json.loads(out.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("No video stream found in file")
    w, h = int(video["width"]), int(video["height"])
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = abs(int(sd["rotation"]))
    if rotation in (90, 270):
        w, h = h, w
    num, _, den = (video.get("avg_frame_rate") or "30/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return {"width": w, "height": h, "duration": duration, "fps": fps or 30.0, "has_audio": has_audio}


def health() -> dict:
    """Report which external tools are available (shown in the UI)."""
    status: dict = {"docker": os.environ.get("CLIPFORGE_DOCKER") == "1"}
    for name in ("ffmpeg", "ffprobe", "claude", "deno"):
        status[name] = find(name) is not None
    # yt-dlp needs a JavaScript runtime for YouTube: Deno, or Node 20+ (the Docker image uses Node)
    status["js_runtime"] = next((n for n in ("deno", "node", "bun") if find(n)), None)
    if status["docker"]:
        status["claude_login"] = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
    if status["claude"]:
        try:
            v = run([find("claude"), "--version"], timeout=30)
            status["claude_version"] = v.stdout.strip()
        except Exception:
            status["claude_version"] = ""
    return status
