"""Framing: how a horizontal video becomes 9:16.

One *plan* per reel, used both by the export (ffmpeg) and by the live phone preview in the editor (browser):
  {"shots": [{"t0", "t1", "layout": "crop" | "fit"}], "keys": [[t, x, is_cut], ...], "crop_w", "crop_h", ...}
Times are relative to the reel start. "crop" shows a 9:16 window whose left edge follows `keys`
(holds, glides over 0.6 s, or jumps on a cut); "fit" shows the whole frame on a blurred copy of itself.

Faces and camera cuts come from a one-time scan of the whole video (`analyze_video`, stored as
analysis.json), so a plan for any part of the video is instant. Without the scan, `make_plan` samples
just the reel's part of the video (a few seconds).
"""
from __future__ import annotations

import re
import statistics
import subprocess
import threading

try:
    import cv2
    import numpy as np
except ImportError:  # no face detection: every plan becomes center crop / whole frame
    cv2 = None
    np = None

_detector = None
STEP = 0.5          # seconds between face samples
SCAN_WIDTH = 480    # frames are scaled to this width for face detection


def _get_detector():
    global _detector
    if _detector is None and cv2 is not None:
        try:
            det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            _detector = det if not det.empty() else False
        except Exception:
            _detector = False
    return _detector or None


def _faces(gray, scale: float) -> list[list[float]]:
    """Faces in a SCAN_WIDTH-wide grayscale frame → [[center_x, width]] in source pixels.
    Tuned params: scaleFactor 1.05, minNeighbors 6, no equalizeHist (OpenCV must stay < 5 for the cascades)."""
    det = _get_detector()
    if det is None:
        return []
    found = det.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=6, minSize=(24, 24))
    return [[round((x + w / 2) / scale, 1), round(w / scale, 1)] for x, y, w, h in found]


# ----------------------------------------------------------------------------- whole-video scan

def background_cmd(cmd: list[str]) -> list[str]:
    """Run a long background job at low priority (Linux / Docker), so the editor and exports stay responsive."""
    import os
    import shutil
    return (["nice", "-n", "15"] + cmd) if os.name != "nt" and shutil.which("nice") else cmd


def scan_threads() -> int:
    """Decoder threads for background work: about a quarter of the machine, so it never starves the browser."""
    import os
    return max(2, (os.cpu_count() or 8) // 4)


def thumb_plan(duration: float, src_w: int, src_h: int) -> dict:
    """Timeline thumbnails: one every couple of seconds, at most 600 per video."""
    interval = max(2.0, duration / 600)
    tw = 160
    th = max(2, int(round(tw * src_h / max(src_w, 1) / 2)) * 2)
    return {"interval": round(interval, 4), "count": int(duration / interval) + 1, "tile_w": tw, "tile_h": th}


def analyze_video(video_path: str, duration: float, src_w: int, src_h: int, progress=None,
                  proxy_out: str | None = None, thumbs_dir: str | None = None) -> dict:
    """One pass over the whole video. Decoding a long 1080p60 video is the expensive part, so one decode feeds:
      - hard camera cuts (ffmpeg scdet) and faces every STEP seconds (the result),
      - `proxy_out`: a light 540p/30 fps H.264 editing copy with a keyframe every second (fast seeking in the browser),
      - `thumbs_dir`: timeline thumbnails 00001.jpg… (see thumb_plan), written as the scan goes.
    `progress(fraction)` is called as it goes. Returns {"cuts": [t], "samples": [[t, faces]], "step"}."""
    from . import tools

    w = SCAN_WIDTH
    h = max(2, int(round(w * src_h / max(src_w, 1) / 2)) * 2)
    scale = w / max(src_w, 1)
    branches = [("s", "scale=320:-2,scdet=threshold=10:sc_pass=1"), ("f", f"fps={1 / STEP:g},scale={w}:{h},format=gray")]
    if proxy_out:
        branches.append(("p", "fps=30,scale=-2:540,format=yuv420p"))
    if thumbs_dir:
        tp = thumb_plan(duration, src_w, src_h)
        branches.append(("t", f"fps=1/{tp['interval']:g},scale={tp['tile_w']}:{tp['tile_h']}"))
    names = "".join(f"[i{k}]" for k in range(len(branches)))
    graph = f"[0:v]split={len(branches)}{names};" + ";".join(f"[i{k}]{flt}[{lbl}]" for k, (lbl, flt) in enumerate(branches))
    cmd = [tools.ffmpeg(), "-hide_banner", "-nostats", "-loglevel", "info", "-threads", str(scan_threads()),
           "-i", video_path, "-filter_complex_threads", "2", "-filter_complex", graph,
           "-map", "[s]", "-an", "-sn", "-f", "null", "-"]
    if proxy_out:
        cmd += ["-map", "[p]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-g", "30",
                "-threads", "2", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-y", proxy_out]
    if thumbs_dir:
        cmd += ["-map", "[t]", "-an", "-sn", "-q:v", "6", "-y", f"{thumbs_dir}/%05d.jpg"]
    cmd += ["-map", "[f]", "-an", "-sn", "-f", "rawvideo", "pipe:1"]
    cmd = background_cmd(cmd)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **tools.popen_flags())
    cuts: list[float] = []
    err_tail: list[str] = []

    def read_stderr():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace")
            m = re.search(r"lavfi\.scd\.time:\s*([\d.]+)", line)
            if m:
                cuts.append(round(float(m.group(1)), 3))
            else:
                err_tail.append(line)
                del err_tail[:-20]
    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()

    samples: list[list] = []
    size = w * h
    k = 0
    last_report = -1.0
    try:
        while True:
            buf = proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            t = round(k * STEP, 2)
            faces = _faces(np.frombuffer(buf, dtype=np.uint8).reshape(h, w), scale) if np is not None else []
            samples.append([t, faces])
            k += 1
            if progress and duration and t - last_report >= max(5.0, duration / 100):
                last_report = t
                progress(min(t / duration, 0.99))
    finally:
        proc.stdout.close()
        proc.wait()
        reader.join(timeout=5)
    if proc.returncode != 0 and not samples:
        raise RuntimeError("Scanning the video failed: " + "".join(err_tail)[-400:])
    return {"version": 1, "step": STEP, "cuts": sorted(set(cuts)), "samples": samples}


# ----------------------------------------------------------------------------- quick sampling (no scan yet)

def detect_cuts(video_path: str, start: float, dur: float) -> list[float]:
    """Hard camera cuts inside [start, start+dur], as times relative to the clip start (ffmpeg scdet)."""
    from . import tools
    try:
        r = tools.run([tools.ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                       "-i", video_path, "-an", "-vf", "scale=320:-2,scdet=threshold=10:sc_pass=1", "-f", "null", "-"],
                      timeout=max(120.0, dur * 4))
    except (subprocess.SubprocessError, OSError, RuntimeError):
        return []
    cuts = sorted({round(float(m), 3) for m in re.findall(r"lavfi\.scd\.time:\s*([\d.]+)", r.stderr or "")})
    return [t for t in cuts if 0.05 < t < dur - 0.05]


def _sample_at(video_path: str, start: float, times: list[float], src_w: int) -> list[list]:
    """Faces at the given clip-relative times, by seeking (fine for one reel, too slow for a whole video)."""
    if _get_detector() is None:
        return [[t, []] for t in times]
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return [[t, []] for t in times]
    scale = SCAN_WIDTH / max(src_w, 1)
    out = []
    try:
        for t in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + t) * 1000)
            ok, frame = cap.read()
            if not ok:
                out.append([t, []])
                continue
            small = cv2.resize(frame, (SCAN_WIDTH, max(1, int(frame.shape[0] * scale))))
            out.append([t, _faces(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), scale)])
    finally:
        cap.release()
    return out


# ----------------------------------------------------------------------------- planning

def _follow(face_samples: list, t0: float | None = None) -> list[tuple[float, float]]:
    """Face samples → [(t, center_x)]: start on the largest face, then stay with the nearest one; median-smoothed."""
    pts: list[tuple[float, float]] = []
    prev = None
    for t, faces in face_samples:
        if not faces:
            continue
        if prev is None:
            cx = max(faces, key=lambda f: f[1])[0]
        else:
            cx = min(faces, key=lambda f: abs(f[0] - prev) - f[1] * 0.5)[0]
        pts.append((t, cx))
        prev = cx
    if not pts:
        return []
    xs = [x for _, x in pts]
    smooth = [statistics.median(xs[max(0, i - 2): i + 3]) for i in range(len(xs))]
    out = [(pts[i][0], smooth[i]) for i in range(len(pts))]
    if t0 is not None:
        out = [(t0, smooth[0])] + [p for p in out if p[0] > t0]
    return out


def _plan_shots(bounds: list[float], samples: list, crop_w: float) -> list[dict]:
    """Give every shot a layout: "crop" when one person (or people close together) is on screen most of
    the time, else "fit" (slides, title cards, screens, wide shots, people too far apart)."""
    def is_face(faces) -> bool:
        if not faces:
            return False
        xs = [cx for cx, _ in faces]
        return max(xs) - min(xs) + max(w for _, w in faces) <= crop_w * 0.95

    # 1. inside each hard-cut shot, also split where the content changes for good (crossfades, slides
    #    sliding in): runs of >= 3 samples; shorter runs are detector noise and fold into their neighbours
    segments: list[dict] = []
    for t0, t1 in zip(bounds, bounds[1:]):
        s = [(t, f) for t, f in samples if t0 <= t < t1]
        runs: list[list] = []  # [is_face, first index, last index + 1]
        for i, (_, f) in enumerate(s):
            v = is_face(f)
            if runs and runs[-1][0] == v:
                runs[-1][2] = i + 1
            else:
                runs.append([v, i, i + 1])
        changed = True
        while changed and len(runs) > 1:
            changed = False
            for k, r in enumerate(runs):
                if r[2] - r[1] < 3:
                    if k > 0:
                        runs[k - 1][2] = r[2]
                    else:
                        runs[1][1] = r[1]
                    runs.pop(k)
                    merged = [runs[0]]
                    for q in runs[1:]:
                        if q[0] == merged[-1][0]:
                            merged[-1][2] = q[2]
                        else:
                            merged.append(q)
                    runs = merged
                    changed = True
                    break
        cut_at = [t0] + [(s[r[1] - 1][0] + s[r[1]][0]) / 2 for r in runs[1:]] + [t1]
        for a, b in zip(cut_at, cut_at[1:]):
            seg = [(t, f) for t, f in s if a <= t < b]
            face_samples = [(t, f) for t, f in seg if is_face(f)]
            crop = bool(seg) and len(face_samples) >= max(1, 0.5 * len(seg))
            segments.append({"t0": a, "t1": b, "layout": "crop" if crop else "fit", "faces": face_samples,
                             "hard": a in bounds})

    # 2. very short segments (< 0.6 s) take the layout of the one before, so the frame doesn't flicker
    for k in range(1, len(segments)):
        if segments[k]["t1"] - segments[k]["t0"] < 0.6:
            segments[k]["layout"] = segments[k - 1]["layout"]
            if segments[k]["layout"] == "crop" and not segments[k]["faces"]:
                segments[k]["faces"] = segments[k - 1]["faces"][-1:]

    # 3. merge neighbours with the same layout that aren't separated by a hard cut; build face tracks
    shots: list[dict] = []
    for seg in segments:
        if shots and shots[-1]["layout"] == seg["layout"] and not seg["hard"]:
            shots[-1]["t1"] = seg["t1"]
            shots[-1]["faces"] += seg["faces"]
        else:
            shots.append(dict(seg))
    for sh in shots:
        sh["track"] = _follow(sh["faces"], sh["t0"]) if sh["layout"] == "crop" else []
        if sh["layout"] == "crop" and not sh["track"]:
            sh["layout"] = "fit"
    return shots


def crop_keys(track: list[tuple[float, float]], crop_w: int, src_w: int,
              cuts: set[float] | None = None) -> list[list]:
    """Keyframes [t, x, is_cut] for the crop window's left edge. The window holds while the face stays in a
    dead zone, glides (0.6 s) for small moves, and jumps on shot changes."""
    max_x = src_w - crop_w

    def clamp(cx: float) -> float:
        return round(max(0.0, min(max_x, cx - crop_w / 2)), 1)

    if not track:
        return [[0.0, round(max_x / 2, 1), True]]
    dead_zone = crop_w * 0.18
    current = clamp(track[0][1])
    keys = [[0.0, current, True]]
    for t, cx in track[1:]:
        target = clamp(cx)
        if cuts and round(t, 3) in cuts:
            keys.append([round(t, 3), target, True])
            current = target
        elif abs(target - current) > crop_w * 0.6:
            keys.append([round(t, 3), target, True])
            current = target
        elif abs(target - current) > dead_zone:
            keys.append([round(t, 3), target, False])
            current = target
    return keys


def keys_to_expr(keys: list[list]) -> str:
    """ffmpeg expression for x(t) from crop_keys(). The editor's preview evaluates the same keys in JS."""
    expr = f"{keys[-1][1]:.1f}"
    for i in range(len(keys) - 1, 0, -1):
        t1, x1, cut = keys[i]
        t0, x0, _ = keys[i - 1]
        ts = max(t0, t1 - 0.6)
        if cut or t1 - ts < 0.05:
            seg = f"{x0:.1f}"
        else:
            seg = f"if(lt(t,{ts:.2f}),{x0:.1f},{x0:.1f}+({x1 - x0:.1f})*(t-{ts:.2f})/{t1 - ts:.2f})"
        expr = f"if(lt(t,{t1:.2f}),{seg},{expr})"
    return expr


def make_plan(mode: str, start: float, end: float, src_w: int, src_h: int,
              analysis: dict | None = None, video_path: str | None = None) -> dict:
    """The framing plan for one reel. Uses the whole-video scan if there is one, else samples the reel."""
    dur = end - start
    cw = int(src_h * 9 / 16) // 2 * 2
    ch = src_h // 2 * 2
    landscape = src_w / max(src_h, 1) > 9 / 16 + 0.01
    base = {"crop_w": cw, "crop_h": ch, "src_w": src_w, "src_h": src_h, "duration": dur, "mode": mode,
            "source": "scan" if analysis else "quick"}
    if mode == "fit" or not landscape or (mode != "center" and _get_detector() is None):
        if mode in ("track", "smart") and landscape:  # no face detection available: center crop
            return {**base, "shots": [{"t0": 0.0, "t1": dur, "layout": "crop"}], "keys": crop_keys([], cw, src_w)}
        return {**base, "shots": [{"t0": 0.0, "t1": dur, "layout": "fit"}], "keys": []}
    if mode == "center":
        return {**base, "shots": [{"t0": 0.0, "t1": dur, "layout": "crop"}], "keys": crop_keys([], cw, src_w)}

    if analysis:
        cuts = [round(c - start, 3) for c in analysis.get("cuts", []) if start + 0.05 < c < end - 0.05]
        samples = [[round(t - start, 2), f] for t, f in analysis.get("samples", []) if start <= t < end]
    else:
        cuts = detect_cuts(video_path, start, dur) if mode == "smart" else []
        bounds = [0.0, *cuts, dur]
        times = []
        for t0, t1 in zip(bounds, bounds[1:]):  # a little away from shot edges
            n = max(1, int((t1 - t0) / STEP))
            times += [t0 + min(0.15, (t1 - t0) / 2) + i * (t1 - t0 - 0.3) / max(n - 1, 1) if n > 1 else (t0 + t1) / 2
                      for i in range(n)]
        samples = _sample_at(video_path, start, times, src_w)

    if mode == "track":  # always a close-up, following faces wherever they are
        faced = [s for s in samples if s[1]]
        track = _follow(samples) if len(faced) >= max(2, len(samples) * 0.15) else []
        return {**base, "shots": [{"t0": 0.0, "t1": dur, "layout": "crop"}], "keys": crop_keys(track, cw, src_w)}

    shots = _plan_shots([0.0, *cuts, dur], samples, cw)
    track: list[tuple[float, float]] = []
    starts: set[float] = set()
    for sh in shots:
        if sh["layout"] == "crop":
            starts.add(round(sh["track"][0][0], 3))
            track += sh["track"]
    return {**base, "shots": [{"t0": round(s["t0"], 3), "t1": round(s["t1"], 3), "layout": s["layout"]} for s in shots],
            "keys": crop_keys(track, cw, src_w, starts)}
