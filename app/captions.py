"""Builds animated word-by-word captions (ASS subtitle format) for a 1080x1920 clip."""
from __future__ import annotations

from pathlib import Path

W, H = 1080, 1920
# The gap between caption words: a space stretched to 160 %. A plain space looks stitched together once the thick
# outline eats into it. The editor's live preview uses the same gap (.pv-cap .w in style.css).
WORD_GAP = "{\\fscx160} {\\r}"


def ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clean(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def chunk_words(words: list[dict], max_words: int) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            too_long = w["end"] - current[0]["start"] > 1.8
            if len(current) >= max_words or gap > 0.6 or too_long or current[-1]["word"].rstrip()[-1:] in ".?!,":
                chunks.append(current)
                current = []
        current.append(w)
    if current:
        chunks.append(current)
    return chunks


def build_ass(words: list[dict], clip_start: float, clip_end: float, settings: dict,
              hook: str = "", out_path: Path | None = None) -> str:
    duration = clip_end - clip_start
    rel = []
    for w in words:
        if w["end"] <= clip_start or w["start"] >= clip_end:
            continue
        text = clean(w["word"])
        if not text:
            continue
        rel.append({"word": text, "start": max(0.0, w["start"] - clip_start), "end": min(duration, w["end"] - clip_start)})

    size = int(settings.get("caption_size", 84))
    font = settings.get("caption_font", "Arial Black")
    primary = ass_color(settings.get("caption_color", "#FFFFFF"))
    highlight = ass_color(settings.get("caption_highlight", "#FFD400"))
    y = int(H * float(settings.get("caption_position", 0.7)))
    upper = bool(settings.get("caption_uppercase", True))

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Caption,{font},{size},{primary},{primary},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,5,70,70,0,1",
        f"Style: Hook,{font},{int(size * 0.72)},&H00000000,&H00000000,{ass_color(settings.get('caption_highlight', '#FFD400'))},"
        f"&H00000000,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,230,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if hook:
        hook_text = clean(hook).upper() if upper else clean(hook)
        lines.append(f"Dialogue: 1,{ts(0)},{ts(min(3.0, duration))},Hook,,0,0,0,,{{\\fad(150,250)}}{hook_text}")

    if settings.get("captions", True):
        chunks = chunk_words(rel, max(1, int(settings.get("caption_words", 3))))
        for ci, chunk in enumerate(chunks):
            next_start = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else duration
            chunk_end = chunk[-1]["end"]
            if next_start - chunk_end < 0.4:  # avoid flicker between close chunks
                chunk_end = next_start
            texts = [w["word"].upper() if upper else w["word"] for w in chunk]
            for wi, w in enumerate(chunk):
                start = chunk[0]["start"] if wi == 0 else w["start"]
                end = chunk[wi + 1]["start"] if wi + 1 < len(chunk) else chunk_end
                if end <= start:
                    continue
                parts = []
                for k, t in enumerate(texts):
                    if k == wi:
                        parts.append(f"{{\\c{highlight}\\fscx112\\fscy112}}{t}{{\\r}}")
                    else:
                        parts.append(t)
                pop = "{\\t(0,80,\\fscx104\\fscy104)\\t(80,160,\\fscx100\\fscy100)}" if wi == 0 else ""
                lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Caption,,0,0,0,,{{\\pos({W // 2},{y})}}{pop}{WORD_GAP.join(parts)}")

    content = "\n".join(lines) + "\n"
    if out_path:
        out_path.write_text(content, encoding="utf-8")
    return content
