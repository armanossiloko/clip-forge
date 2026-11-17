"""Uses your local Claude Code CLI (`claude -p`) to find the best short-form moments.

This runs on whatever account Claude Code is logged into (your Claude subscription),
so no API key is needed. Run `claude` once in a terminal to log in first.
"""
from __future__ import annotations

import json
import os
import re
import tempfile

from . import tools

# What Claude looks for in each mode ("cuts" doesn't pick moments: it splits a stretch of the video into reels).
MODES: dict[str, dict] = {
    "top": {"label": "Top moments",
            "goal": "Pick the {n} BEST self-contained moments to cut into vertical short clips.",
            "prefer": "strong opinions, surprising facts, emotional moments, funny moments, clear actionable advice, stories with a payoff."},
    "funny": {"label": "Funniest bits",
              "goal": "Pick the {n} FUNNIEST moments: jokes, punchlines, banter, awkward or absurd moments.",
              "prefer": "a clear setup and a laugh: start at the setup, put the punchline in the middle, and keep the reaction after it."},
    "takes": {"label": "Hot takes",
              "goal": "Pick the {n} strongest OPINIONS: bold, surprising or debatable statements people will want to argue about in the comments.",
              "prefer": "a clear claim stated with conviction, plus the reasoning behind it."},
    "tips": {"label": "Tips & how-tos",
             "goal": "Pick the {n} most useful TIPS or explanations a viewer can act on.",
             "prefer": "self-contained, practical advice. The clip must contain the whole tip, not just the start of it."},
    "stories": {"label": "Stories",
                "goal": "Pick the {n} best STORIES or anecdotes.",
                "prefer": "a setup and a payoff. Include the whole payoff; never cut before the story lands."},
    "quotes": {"label": "Short quotes",
               "goal": "Pick the {n} most QUOTABLE lines: short, punchy statements that work completely on their own.",
               "prefer": "one strong idea per clip, said memorably. Keep clips tight."},
    "custom": {"label": "Custom",
               "goal": "Pick up to {n} moments that match the creator's request below.",
               "prefer": "exactly what the creator asked for. If fewer than {n} moments truly match, return fewer."},
    "cuts": {"label": "Cuts"},
}

# How every clip is shaped: build-up → peak → payoff. (Opening on the punchline leaves the rest of the clip with nothing.)
ARC = """- Build every clip around its PEAK: the punchline, reveal, fail, big reaction or payoff. Give its time as "peak".
- The peak must land INSIDE the clip, about 30-70% of the way in, never in its first few seconds. Start on the setup that
  makes people want to see what happens (the question, the claim, the start of the story or the attempt), keep the peak in the
  middle, and keep the reaction or payoff after it. End on a complete thought, not mid-sentence.
- Don't start mid-sentence or on filler ("so", "um", "anyway"). The on-screen hook text (below) teases the peak during the first
  seconds, so the clip itself should not open with the punchline.
- You only see the transcript. In gameplay, action or reaction videos the peak is often something that HAPPENS on screen
  (a death, an explosion, a jump scare) and the transcript shows the reactions to it. Those reactions ("is he dead?!", laughing,
  screaming) come right AFTER the event, so put the peak just before them and start the clip well before that."""

CLIP_SHAPE = """{{
  "start": 123.4,
  "peak": 141.0,
  "end": 165.2,
  "title": "Short punchy title, max 60 characters",
  "hook": "The on-screen hook text for the first seconds, max 8 words",
  "caption": "Post caption for TikTok/Shorts, 1-2 sentences, written to drive comments",
  "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4"],
  "score": 87,
  "reason": "One sentence on why this will perform"
}}"""

PROMPT = """You are an expert short-form video editor who turns long videos into viral TikTok / YouTube Shorts / Reels clips.

Below is a timestamped transcript of a video{title_part}. Each line is `[start-end] text`, times in seconds.

{goal}

Rules:
- Each clip must be between {min_s} and {max_s} seconds long.
- A clip must make sense on its own to someone who never saw the full video.
{arc}
- Prefer {prefer}
- Clips must not overlap.
- Use the transcript timestamps for start/end.
{extra}
Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly this shape:
{{"clips": [""" + CLIP_SHAPE + """]}}

Order clips from best to worst. score is 1-100 (predicted virality).

TRANSCRIPT:
{transcript}
"""

CUTS_PROMPT = """You are an expert short-form video editor. A video{title_part} has been cut into {n} consecutive vertical reels.
Write the post text for each reel. Below is the transcript of each part. Times are in seconds.
{extra}
Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly this shape:
{{"parts": [{{
  "part": 1,
  "title": "Short punchy title, max 60 characters",
  "hook": "The on-screen hook text for the first seconds, max 8 words",
  "caption": "Post caption for TikTok/Shorts, 1-2 sentences, written to drive comments",
  "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4"]
}}]}}
One entry per part, in order. If a part only makes sense after the previous one, say so in its caption (e.g. "Part 2").

{parts}
"""

ASK_PROMPT = """You are the editor inside ClipForge, an app that cuts long videos into vertical short clips for TikTok / YouTube Shorts / Reels.
The creator is talking to you about a video{title_part}. Below are the clips it has now, your recent conversation, the creator's new
request, and the video's timestamped transcript (`[start-end] text`, times in seconds).

You can add clips, change clips, delete clips, or just answer a question. Rules for clips you add or change:
- Clips are normally {min_s}-{max_s} seconds long, but follow the creator if they ask for a different length or exact times.
- Unless the creator gives exact times:
{arc}
- Use the transcript timestamps. Clips you add may overlap existing ones (e.g. a shorter version of a clip).
- Refer to existing clips by their number.
{extra}
CURRENT CLIPS:
{clips}

RECENT CONVERSATION:
{history}

CREATOR'S REQUEST:
{request}

Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly this shape:
{{"reply": "1-3 sentences to the creator: what you did, or the answer to their question",
  "add": [""" + CLIP_SHAPE + """],
  "update": [{{"clip": 2, "start": 61.0, "end": 88.5, "title": "...", "hook": "...", "caption": "...", "hashtags": ["#a"]}}],
  "delete": [3]}}
Use empty lists for anything you don't do. In "update", include only the fields you change.

TRANSCRIPT:
{transcript}
"""


def _login_hint() -> str:
    if os.environ.get("CLIPFORGE_DOCKER") == "1":
        return ("\n→ Claude Code in Docker logs in with a token. On your PC run `claude setup-token`, put the token in the "
                ".env file as CLAUDE_CODE_OAUTH_TOKEN=..., then run `docker compose up -d`.")
    return "\n→ Open a terminal and run `claude` once to log in."


def format_transcript(segments: list[dict]) -> str:
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text'].strip()}" for s in segments)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("Claude did not return JSON")


def model_label(model_id: str | None) -> str:
    """'claude-sonnet-5-5' → 'Claude Sonnet 5.5', 'claude-3-5-sonnet-20241022' → 'Claude 3.5 Sonnet'."""
    if not model_id:
        return "Claude"
    mid = re.sub(r"\[.*?\]$", "", model_id.strip())       # e.g. a "[1m]" context suffix
    mid = re.sub(r"-\d{8}$", "", mid)                      # release date
    parts = [p for p in mid.split("-") if p]
    if parts and parts[0].lower() == "claude":
        parts = parts[1:]
    words: list[str] = []
    for p in parts:
        if p.isdigit() and words and re.fullmatch(r"[\d.]+", words[-1]):
            words[-1] += "." + p                            # join version numbers: 5-5 → 5.5
        else:
            words.append(p if p.isdigit() else p.capitalize())
    return " ".join(["Claude", *words])


def _model_used(envelope: dict) -> str | None:
    """The model that wrote the answer, from Claude Code's JSON output (`modelUsage` is keyed by model id).
    If several models show up (Claude Code can use a small one for housekeeping), take the one that wrote the most."""
    usage = envelope.get("modelUsage")
    if isinstance(usage, dict) and usage:
        def out_tokens(item):
            v = item[1]
            return (v.get("outputTokens") or 0) if isinstance(v, dict) else 0
        return max(usage.items(), key=out_tokens)[0]
    return envelope.get("model") if isinstance(envelope.get("model"), str) else None


def ask_claude(prompt: str, settings: dict) -> tuple[str, str | None]:
    """Returns (Claude's answer, the model id that wrote it, or None if Claude Code didn't say)."""
    exe = tools.find("claude")
    if not exe:
        raise RuntimeError(  # noqa: E501
            "Claude Code CLI not found. Install it in PowerShell with:  irm https://claude.ai/install.ps1 | iex  "
            "then run `claude` once to log in, and restart ClipForge."
        )
    cmd = [exe, "-p", "--output-format", "json"]
    if settings.get("claude_model"):
        cmd += ["--model", settings["claude_model"]]
    # The prompt goes through stdin: Windows limits command-line length to ~8k characters,
    # and transcripts are much longer than that. Run in an empty folder so Claude has no files to look at.
    with tempfile.TemporaryDirectory(prefix="clipforge_claude_") as tmp:
        result = tools.run(cmd, cwd=tmp, input_text=prompt, timeout=float(settings.get("claude_timeout", 600)))
    raw = (result.stdout or "").strip()
    if result.returncode != 0 and not raw:
        err = (result.stderr or "").strip()[-800:]
        if any(k in err.lower() for k in ("login", "auth", "401", "token", "api key")):
            err += _login_hint()
        raise RuntimeError(f"Claude Code failed (exit {result.returncode}): {err}")
    requested = settings.get("claude_model") or None
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return raw, requested  # older CLI versions / text output
    if isinstance(envelope, list):  # some versions emit a list of messages
        envelope = next((m for m in reversed(envelope) if isinstance(m, dict) and m.get("type") == "result"), {})
    if not isinstance(envelope, dict):
        return raw, requested
    if envelope.get("is_error"):
        msg = str(envelope.get("result") or envelope)
        if any(k in msg.lower() for k in ("login", "auth", "401", "token", "api key")):
            msg += _login_hint()
        raise RuntimeError(f"Claude Code returned an error: {msg}")
    return envelope.get("result") or "", _model_used(envelope) or requested


def _extra(settings: dict, brief: dict | None = None, chat: bool = False) -> str:
    """The creator's standing instructions (Settings) plus what they asked for on this video.
    In a chat request the new message wins, so the original request is only background there."""
    lines = []
    channel = (settings.get("extra_instructions") or "").strip()
    if channel:
        lines.append(f"- About the creator's channel (always follow): {channel}")
    request = ((brief or {}).get("instructions") or "").strip()
    if request and chat:
        lines.append(f"- What the creator first asked for on this video (background only; their new request below wins): {request}")
    elif request:
        lines.append(f"- The creator's request for this video (most important, follow it): {request}")
    return "\n".join(lines) + ("\n" if lines else "")


def _ask_json(prompt: str, settings: dict, what: str, accept) -> tuple[dict, str | None]:
    """Ask Claude, parse its JSON and check it with `accept(data) -> bool`. One retry on a bad answer."""
    last_error: Exception | None = None
    for _attempt in range(2):
        text, model = ask_claude(prompt, settings)
        try:
            data = _extract_json(text)
            if isinstance(data, list):
                data = {"clips": data}
            if isinstance(data, dict) and accept(data):
                return data, model
            last_error = ValueError(f"Claude's answer had no {what}")
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        prompt += "\n\nIMPORTANT: Your previous answer was not valid JSON in the shape above. Reply with the JSON object only."
    raise RuntimeError(f"Could not read Claude's {what}: {last_error}")


def find_clips(segments: list[dict], settings: dict, title: str = "",
               brief: dict | None = None) -> tuple[list[dict], str | None]:
    """Returns (Claude's clip suggestions, the model id that picked them)."""
    if not segments:
        raise RuntimeError("The transcript is empty: no speech was detected in this video"
                           + (" (or in the part you chose)." if (brief or {}).get("range") else "."))
    mode = MODES.get((brief or {}).get("mode") or "top") or MODES["top"]
    if "goal" not in mode:
        mode = MODES["top"]
    n = int(settings["num_clips"])
    prompt = PROMPT.format(
        title_part=f' titled "{title}"' if title else "",
        goal=mode["goal"].format(n=n), arc=ARC,
        prefer=mode["prefer"].format(n=n),
        min_s=int(settings["min_seconds"]),
        max_s=int(settings["max_seconds"]),
        extra=_extra(settings, brief),
        transcript=format_transcript(segments),
    )
    data, model = _ask_json(prompt, settings, "clip suggestions", lambda d: bool(d.get("clips")))
    return data["clips"], model


POST_TEXT_PROMPT = """You write the post text for a short vertical video (TikTok / YouTube Shorts / Reels) cut from a longer video{title_part}.
Below is what's said in the clip (`[start-end] text`, times in seconds).
{extra}
Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly this shape:
{{"title": "Short punchy title, max 60 characters",
  "caption": "Post caption, 1-2 sentences, written to drive comments",
  "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4"]}}
If nothing is said in the clip, base it on the video's title.

CLIP:
{transcript}
"""


def write_post_text(segments: list[dict], settings: dict, title: str = "",
                    brief: dict | None = None) -> tuple[dict, str | None]:
    """Title, caption and hashtags for one reel (e.g. one made by hand). Returns (dict, model id)."""
    prompt = POST_TEXT_PROMPT.format(title_part=f' titled "{title}"' if title else "", extra=_extra(settings, brief),
                                     transcript=format_transcript(segments) or "(no speech in this clip)")
    data, model = _ask_json(prompt, settings, "post text", lambda d: bool(d.get("caption") or d.get("title")))
    return data, model


def describe_cuts(parts: list[list[dict]], settings: dict, title: str = "",
                  brief: dict | None = None) -> tuple[list[dict], str | None]:
    """Post text for consecutive parts. `parts` holds each part's transcript segments.
    Returns (one dict per part, in order; missing ones are {}), the model id)."""
    blocks = []
    for i, segs in enumerate(parts, 1):
        body = format_transcript(segs) if segs else "(no speech in this part)"
        blocks.append(f"PART {i}:\n{body}")
    prompt = CUTS_PROMPT.format(title_part=f' titled "{title}"' if title else "", n=len(parts),
                                extra=_extra(settings, brief), parts="\n\n".join(blocks))
    data, model = _ask_json(prompt, settings, "titles for the parts", lambda d: isinstance(d.get("parts"), list))
    by_part: dict[int, dict] = {}
    for i, p in enumerate(data["parts"], 1):
        if isinstance(p, dict):
            try:
                by_part[int(p.get("part") or i)] = p
            except (TypeError, ValueError):
                by_part[i] = p
    return [by_part.get(i, {}) for i in range(1, len(parts) + 1)], model


def ask_editor(segments: list[dict], clips: list[dict], history: list[dict], request: str, settings: dict,
               title: str = "", brief: dict | None = None) -> tuple[dict, str | None]:
    """A free-form request about a finished video. Returns ({"reply", "add", "update", "delete"}, model id)."""
    def clip_line(c: dict) -> str:
        peak = f" peak {c['peak']:.1f}" if isinstance(c.get("peak"), (int, float)) else ""
        return (f"#{c['index']} [{c['start']:.1f}-{c['end']:.1f}]{peak} ({c['end'] - c['start']:.0f}s) \"{c.get('title', '')}\""
                f" | hook: {c.get('hook', '')} | caption: {c.get('caption', '')} | {' '.join(c.get('hashtags') or [])}")
    hist = "\n".join(f"{'Creator' if m.get('role') == 'you' else 'You'}: {m.get('text', '')}" for m in history[-8:])
    prompt = ASK_PROMPT.format(
        title_part=f' titled "{title}"' if title else "",
        min_s=int(settings["min_seconds"]), max_s=int(settings["max_seconds"]), arc=ARC,
        extra=_extra(settings, brief, chat=True),
        clips="\n".join(clip_line(c) for c in sorted(clips, key=lambda c: c["index"])) or "(none yet)",
        history=hist or "(none)",
        request=request.strip(),
        transcript=format_transcript(segments) or "(no speech detected)",
    )
    data, model = _ask_json(prompt, settings, "answer", lambda d: "reply" in d or "add" in d or "update" in d)
    out = {"reply": str(data.get("reply") or "").strip()}
    for k in ("add", "update", "delete"):
        v = data.get(k)
        out[k] = v if isinstance(v, list) else []
    return out, model
