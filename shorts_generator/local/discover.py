"""Full-video highlight discovery (MODE 1).

Pipeline, all resumable (every step caches to disk with atomic writes):
  1. inspect   — yt-dlp metadata (platform, duration, chapters, formats, subs).
  2. lowres    — one 160p download (has audio) or the local file itself.
  3. chunks    — 10-min chunks: faster-whisper transcript + per-second loudness
                 + per-2-second frame motion/brightness (tiny grey frames).
  4. windows   — Gemini reads 20-min windows (transcript + signal summary) and
                 proposes 0-4 genuine moments each, with sub-scores.
  5. shortlist — dedupe, rank by text scores + local visual penalties.
  6. raw clips — download only the shortlisted ranges at 1080p (or cut local).
  7. visual    — Gemini looks at 6 frames per raw clip and scores the gameplay;
                 OpenCV checks a face is inside the campaign facecam box.
  8. rank      — explainable rubric, threshold, manifest + review.html.

Cached analysis lives in work/runs/<run_id>/analysis;
everything a run creates lives in work/runs/<run_id>/ (deleted with the run).
"""
import hashlib
import html
import json
import os
import tempfile
import re
import shutil
import subprocess
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import studio_events as studio

ROOT = os.getenv("CLIP_ROOT", r"D:\GTA-VI-Clipping")
CHUNK = 600.0          # seconds per analysis chunk
WIN, WIN_STEP = 1200.0, 1080.0   # LLM windows overlap by 2 minutes


class QuotaExhausted(RuntimeError):
    pass


# ---------------------------------------------------------------- utilities

def hms(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}"


def parse_hms(v: str) -> float:
    total = 0.0
    for part in str(v).split(":"):
        total = total * 60 + float(part or 0)
    return total


def slug(text: str, n: int = 32) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return s[:n].strip("_") or "clip"


def write_json(path: str, data) -> None:
    """Atomic write: a cancelled run never leaves a half-written cache file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ytdlp():
    import yt_dlp  # type: ignore
    return yt_dlp


def llm(prompt: str, images: Optional[list] = None) -> Dict:
    from ..highlights import _parse_json_loose
    from .llm import call_gemini_llm
    try:
        try:
            return _parse_json_loose(call_gemini_llm(prompt, images))
        except json.JSONDecodeError:
            print("[discover] Gemini returned malformed JSON; retrying once", flush=True)
            return _parse_json_loose(call_gemini_llm(prompt + "\nReturn one valid JSON object only.", images))
    except RuntimeError as e:
        if "HTTP 429" in str(e):  # a real quota reply, not just any failure
            raise QuotaExhausted(str(e)) from e
        raise


# ---------------------------------------------------------------- 1. inspect

def inspect_source(src: str) -> Dict:
    if os.path.isfile(src):
        stem = os.path.splitext(os.path.basename(src))[0]
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src],
            capture_output=True, text=True, check=True).stdout.strip())
        return {"platform": "local", "key": f"local_{slug(stem, 40)}", "url": os.path.abspath(src),
                "title": stem, "duration": dur, "chapters": [], "heights": [], "channel": None}
    try:
        with ytdlp().YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
            info = y.extract_info(src, download=False)
    except Exception as e:  # yt-dlp explains private / removed / region / sub-only
        raise RuntimeError(f"Source not accessible: {str(e).splitlines()[0][:300]}") from e
    platform = (info.get("extractor_key") or "web").lower().replace("vod", "").replace(":", "")
    return {
        "platform": platform,
        "key": f"{platform}_{slug(str(info.get('id')), 40)}",
        "url": info.get("webpage_url") or src,
        "title": info.get("title"),
        "duration": float(info.get("duration") or 0),
        "channel": (info.get("uploader_id") or info.get("channel") or info.get("uploader") or "").lower(),
        "chapters": [{"start": c["start_time"], "end": c["end_time"], "title": c.get("title")}
                     for c in info.get("chapters") or []],
        "heights": sorted({f["height"] for f in info.get("formats", []) if f.get("height")}),
        "subtitles": sorted((info.get("subtitles") or {}).keys()),
    }


# ---------------------------------------------------------------- 2. lowres

def get_lowres(src_info: Dict, source_dir: str) -> str:
    if src_info["platform"] == "local":
        return src_info["url"]
    os.makedirs(source_dir, exist_ok=True)
    for name in os.listdir(source_dir):
        if os.path.splitext(name)[0] == "lowres" and os.path.isfile(os.path.join(source_dir, name)):
            return os.path.join(source_dir, name)
    print("[discover] downloading low-resolution copy for analysis (resumable)…", flush=True)
    opts = {"format": "worst[height>=144][acodec!=none]/worst", "outtmpl": os.path.join(source_dir, "lowres.%(ext)s"),
            "quiet": True, "no_warnings": True, "continuedl": True, "noprogress": True,
            "concurrent_fragment_downloads": 4, "progress_hooks": [studio.download_hook]}
    with ytdlp().YoutubeDL(opts) as y:
        y.download([src_info["url"]])
    return get_lowres(src_info, source_dir)


# ---------------------------------------------------------------- 3. chunks

def _decode_audio(path: str, start: float, dur: float, sr: int = 16000) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", path,
                          "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def _decode_frames(path: str, start: float, dur: float, every: float = 2.0, w: int = 96, h: int = 54) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", path,
                          "-an", "-vf", f"fps=1/{every},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(np.float32)


def analyse_chunks(lowres: str, duration: float, cache_dir: str, rng: Tuple[float, float], cam_box) -> List[Dict]:
    from faster_whisper import WhisperModel  # type: ignore

    model = None
    starts = [s for s in np.arange(0, duration, CHUNK) if s + CHUNK > rng[0] and s < rng[1]]
    chunks = []
    for i, s in enumerate(starts, 1):
        path = os.path.join(cache_dir, f"chunk_{int(s):06d}.json")
        data = read_json(path)
        if data is None:
            dur = min(CHUNK, duration - s)
            t0 = time.time()
            audio = _decode_audio(lowres, s, dur + 5)  # 5 s tail so the last sentence isn't cut
            if model is None:
                model = WhisperModel(os.getenv("LOCAL_WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
            segs, _ = model.transcribe(audio, language=os.getenv("CLIP_LANGUAGE", "en"), beam_size=1,
                                       vad_filter=True, condition_on_previous_text=False)
            segments = [{"s": round(s + x.start, 2), "e": round(s + x.end, 2), "t": x.text.strip()}
                        for x in segs if x.start < dur and x.text.strip()]
            a = audio[: int(dur * 16000)]
            n = len(a) // 16000
            loud = (20 * np.log10(np.sqrt((a[: n * 16000].reshape(n, 16000) ** 2).mean(1)) + 1e-6)).round(1)
            fr = _decode_frames(lowres, s, dur)
            for fx, fy, fw, fh in corner_boxes(cam_box) if cam_box else []:
                H, W = fr.shape[1:]  # ignore possible facecam corners when measuring gameplay motion
                fr[:, int(fy * H):int((fy + fh) * H) + 1, int(fx * W):int((fx + fw) * W) + 1] = 0
            motion = np.r_[0, np.abs(np.diff(fr, axis=0)).mean((1, 2))].round(2) if len(fr) else np.zeros(0)
            data = {"start": s, "dur": dur, "segments": segments, "loud": loud.tolist(),
                    "motion": motion.tolist(), "bright": fr.mean((1, 2)).round(1).tolist() if len(fr) else []}
            write_json(path, data)
            print(f"[chunk {i}/{len(starts)}] {hms(s)}–{hms(s + dur)}: {len(segments)} lines "
                  f"({time.time() - t0:.0f}s)", flush=True)
        chunks.append(data)
        studio.progress("transcription chunks", i, len(starts))
    return chunks


def signal_tracks(chunks: List[Dict]) -> Dict[str, np.ndarray]:
    """Concatenate per-chunk signals into whole-video arrays indexed by seconds."""
    end = int(max(c["start"] + c["dur"] for c in chunks)) + 1
    loud, motion, bright = np.full(end, np.nan), np.full(end, np.nan), np.full(end, np.nan)
    for c in chunks:
        s = int(c["start"])
        loud[s:s + len(c["loud"])] = c["loud"]
        for j, (m, b) in enumerate(zip(c["motion"], c["bright"])):
            motion[s + 2 * j: s + 2 * j + 2] = m
            bright[s + 2 * j: s + 2 * j + 2] = b
    med = np.nanmedian(loud)
    mad = np.nanmedian(np.abs(loud - med)) or 1.0
    return {"loud": loud, "loud_z": (loud - med) / (1.4826 * mad), "motion": motion, "bright": bright,
            "motion_med": float(np.nanmedian(motion))}


def local_visual(tr: Dict, a: float, b: float) -> Dict:
    sl = slice(int(a), max(int(a) + 1, int(b)))
    m, br = tr["motion"][sl], tr["bright"][sl]
    m, br = m[~np.isnan(m)], br[~np.isnan(br)]
    static = float((m < 0.35 * tr["motion_med"]).mean()) if len(m) else 1.0
    black = float((br < 12).mean()) if len(br) else 0.0
    return {"static_ratio": round(static, 2), "black_ratio": round(black, 2),
            "motion_rel": round(float(m.mean() / (tr["motion_med"] or 1)), 2) if len(m) else 0.0}


# ---------------------------------------------------------------- 4. windows

WINDOW_PROMPT = """You are a senior short-form editor scouting a {kind} recording for Shorts/TikTok clips.
Below is one window of the transcript (absolute times, h:mm:ss) and a 30-second signal summary:
  loud = speech loudness vs this streamer's normal (z-score; >2 is shouting), wps = words/sec,
  motion = on-screen change vs normal (1.0 = typical gameplay; <0.35 static/menu/AFK), black = loading/black share.

Find ONLY genuinely entertaining, self-contained moments (0-4). Good: clutch plays, fights, deaths,
escapes, funny fails, big surprised/angry/laughing reactions WITH a visible cause, strong opinions
with a punchline. Bad: routine chat, reading donations, menus/inventory/loadout, loading, silence,
reactions with no understandable cause. Loudness alone is not quality. Return [] if nothing is good.
Each moment: 15-75 s, start a little before the setup, end right after the payoff.

JSON only:
{{"moments": [{{"start": seconds, "end": seconds, "title": "...", "hook": "exact quote or on-screen event to open with",
  "description": "what happens", "why": "why a viewer would care",
  "hook_score": 0-10, "reaction_score": 0-10, "payoff_score": 0-10, "gameplay_guess": 0-10, "confidence": 0-10}}]}}

Signals:
{signals}

Transcript:
{transcript}"""


def window_candidates(chunks: List[Dict], tr: Dict, cache_dir: str, rng: Tuple[float, float], kind: str) -> List[Dict]:
    segs = [x for c in chunks for x in c["segments"]]
    end = max(c["start"] + c["dur"] for c in chunks)
    out = []
    starts = [w for w in np.arange(0, end, WIN_STEP) if w + WIN > rng[0] and w < rng[1]]
    for i, w0 in enumerate(starts, 1):
        w1 = min(w0 + WIN, end)
        lines = [f"[{hms(x['s'])}] {x['t']}" for x in segs if w0 <= x["s"] < w1]
        transcript = "\n".join(lines)
        sig = []
        for b in np.arange(w0, w1, 30):
            sl = slice(int(b), int(b + 30))
            words = sum(len(x["t"].split()) for x in segs if b <= x["s"] < b + 30)
            sig.append(f"{hms(b)} loud={np.nanmax(tr['loud_z'][sl]):.1f} wps={words / 30:.1f} "
                       f"motion={np.nanmean(tr['motion'][sl]) / (tr['motion_med'] or 1):.2f} "
                       f"black={np.nanmean(tr['bright'][sl] < 12):.1f}")
        key = hashlib.sha1((WINDOW_PROMPT + transcript + "".join(sig)).encode()).hexdigest()[:12]
        path = os.path.join(cache_dir, f"window_{int(w0):06d}_{key}.json")
        data = read_json(path)
        if data is None:
            if not lines:
                data = {"moments": []}
            else:
                data = llm(WINDOW_PROMPT.format(kind=kind, signals="\n".join(sig), transcript=transcript))
            write_json(path, data)
            print(f"[window {i}/{len(starts)}] {hms(w0)}–{hms(w1)}: {len(data.get('moments', []))} moments", flush=True)
        studio.progress("analysis windows", i, len(starts))
        for m in data.get("moments", []):
            try:
                a, b = float(m["start"]), float(m["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if w0 - 5 <= a < b <= w1 + 5 and 7 <= b - a <= 120 and rng[0] <= a <= rng[1]:
                out.append({**m, "start": a, "end": b})
    return out


def dedupe(cands: List[Dict], key: str) -> List[Dict]:
    kept = []
    for c in sorted(cands, key=lambda x: -x[key]):
        if all(min(c["end"], k["end"]) - max(c["start"], k["start"]) < 0.3 * (c["end"] - c["start"]) for k in kept):
            kept.append(c)
    return kept


# ---------------------------------------------------------------- 6. raw clips

def cut_raw(src_info: Dict, a: float, b: float, out: str, height: int = 720) -> str:
    """Clip of [a, b] at the best quality ≤ height (720p for review, 1080p for polish), original framing/audio."""
    if os.path.exists(out):
        return out
    tmp = out[:-4] + ".part.mp4"
    if src_info["platform"] == "local":
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
                        "-i", src_info["url"], "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-c:a", "aac", "-b:a", "192k", tmp], check=True)
    else:
        from yt_dlp.utils import download_range_func  # type: ignore
        opts = {"format": f"best[height<={height}]/bestvideo[height<={height}]+bestaudio/best",
                "outtmpl": tmp[:-4] + ".%(ext)s", "quiet": True, "no_warnings": True, "noprogress": True,
                "concurrent_fragment_downloads": 4, "progress_hooks": [studio.download_hook],
                "download_ranges": download_range_func(None, [(a, b)]), "force_keyframes_at_cuts": True,
                "merge_output_format": "mp4"}
        with ytdlp().YoutubeDL(opts) as y:
            y.download([src_info["url"]])
    os.replace(tmp, out)
    return out


def grab_frames(path: str, n: int = 6, width: int = 512) -> List[bytes]:
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                               capture_output=True, text=True).stdout.strip() or 0)
    frames = []
    for k in range(n):
        t = dur * (k + 0.5) / n
        jpg = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                              "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-"],
                             capture_output=True).stdout
        if jpg:
            frames.append(jpg)
    return frames


def corner_boxes(box) -> List[List[float]]:
    """The profile's facecam box mirrored into all four corners (streamers move it per game)."""
    fx, fy, fw, fh = box
    xs, ys = {fx, round(1 - fx - fw, 4)}, {fy, round(1 - fy - fh, 4)}
    return [[x, y, fw, fh] for y in sorted(ys, reverse=True) for x in sorted(xs, reverse=True)]


def box_for_corner(box, corner: Optional[str]) -> Optional[List[float]]:
    """Profile facecam box moved to the corner Gemini saw the cam in (OpenCV 5 has no Haar face detector)."""
    if not box or corner not in ("top-left", "top-right", "bottom-left", "bottom-right"):
        return None
    fx, fy, fw, fh = box
    x = min(fx, 1 - fx - fw) if corner.endswith("left") else max(fx, 1 - fx - fw)
    y = min(fy, 1 - fy - fh) if corner.startswith("top") else max(fy, 1 - fy - fh)
    return [round(x, 4), round(y, 4), fw, fh]


VISUAL_PROMPT = """You are checking shortlisted livestream clips before they are edited into Shorts.
For each clip you get 6 frames in time order (labelled CLIP <id>) plus what the streamer says.
Judge ONLY what is visible. JSON only:
{{"clips": [{{"id": <id>, "visible": "what is on screen", "activity": "combat|driving|exploring|looting|menu|inventory|loading|just_chatting|other",
  "gameplay_action": 0-10, "visual_quality": 0-10, "facecam_visible": true/false,
  "facecam_corner": "bottom-left|bottom-right|top-left|top-right|full_screen|none",
  "cause_visible": true/false, "third_party_media": true/false,
  "verdict": "keep|reject", "reason": "one sentence"}}]}}
third_party_media = the streamer is watching/reacting to someone else's video, clip, song or beat on screen.
Reject clips that are mostly menus/inventory/loading/idle, or whose reaction has no visible or spoken cause.

{clips}"""


def visual_check(batch: List[Dict]) -> Dict[int, Dict]:
    images, desc = [], []
    for c in batch:
        desc.append(f"CLIP {c['id']}: title={c['title']!r}; transcript excerpt: {c['excerpt'][:600]!r}")
        images += c["_frames"]
    data = llm(VISUAL_PROMPT.format(clips="\n".join(desc)), images)
    return {int(v["id"]): v for v in data.get("clips", []) if "id" in v}


# ---------------------------------------------------------------- 8. rank

def score(c: Dict, require_facecam: bool, no_third_party: bool = False) -> Tuple[float, List[str]]:
    """Editorial rubric (0-100). Not a probability of going viral."""
    v = c.get("visual") or {}
    gameplay = v.get("gameplay_action", c.get("gameplay_guess", 5))
    vq = v.get("visual_quality", 6)
    base = 10 * (0.20 * c.get("hook_score", 0) + 0.25 * c.get("payoff_score", 0) + 0.20 * c.get("reaction_score", 0)
                 + 0.20 * gameplay + 0.15 * vq)
    notes, lv = [], c["local_visual"]
    if lv["static_ratio"] > 0.6:
        base -= 10; notes.append("mostly static footage (-10)")
    if lv["black_ratio"] > 0.2:
        base -= 10; notes.append("black/loading frames (-10)")
    if v.get("activity") in ("menu", "inventory", "loading"):
        base -= 15; notes.append(f"{v['activity']} screen (-15)")
    if v.get("cause_visible") is False:
        base -= 8; notes.append("reaction cause not visible (-8)")
    if require_facecam and v and not v.get("facecam_visible"):
        base -= 10; notes.append("facecam not visible (-10)")
    if no_third_party and v.get("third_party_media"):
        base = 0.0; notes.append("shows other creators' video/music — not allowed by this campaign")
    if not v:
        notes.append("no Gemini visual check (local signals only)")
    return round(max(0.0, base), 1), notes


# ---------------------------------------------------------------- gallery

def write_gallery(run_dir: str, run: Dict, cands: List[Dict]) -> str:
    cards = []
    for c in cands:
        rel = lambda p: os.path.relpath(p, run_dir).replace("\\", "/") if p else ""
        status = c["status"]
        cards.append(f"""<article class="{status}"><header><b>#{c['id']}</b> {html.escape(c['title'])}
<span class="score">{c['overall']}</span><span class="st">{status}</span></header>
<video controls preload="none" poster="{rel(c.get('thumb'))}" src="{rel(c.get('raw_clip'))}"></video>
<p class="ts">{c['start_hms']} → {c['end_hms']} ({c['duration']:.0f}s) · <a href="{html.escape(c['source_url'])}">source</a></p>
<p><i>Hook:</i> {html.escape(str(c.get('hook', '')))}</p><p>{html.escape(str(c.get('description', '')))}</p>
<p><i>Why:</i> {html.escape(str(c.get('why', '')))}</p>
<p class="sub"><b>Campaign preflight:</b> NEEDS MANUAL REVIEW. Discovery footage is for editorial review.</p>
<p class="sub"><b>Editing approach:</b> {("Keep the setup and payoff together. Use preceding footage for the disclosure and retain original speech." if status == "candidate" else "Hold for editorial review before editing.")}</p>
<p class="sub">hook {c.get('hook_score')} · reaction {c.get('reaction_score')} · payoff {c.get('payoff_score')} ·
gameplay {c['gameplay_score']} · visual {c['visual_score']} · facecam {(c.get('visual') or {}).get('facecam_corner', 'unchecked')} · confidence {c.get('confidence')}</p>
<p class="sub">{html.escape('; '.join(c['notes']) or 'no penalties')}</p>
<p class="sub">{html.escape((c.get('visual') or {}).get('visible', ''))}</p></article>""")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip review {html.escape(run['run_id'])}</title><style>
:root{{color-scheme:light dark;--bg:#111;--fg:#eee;--card:#1c1c22;--mut:#999;--acc:#a970ff}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}}
article{{background:var(--card);border-radius:10px;padding:12px}} article.rejected{{opacity:.45}}
video{{width:100%;border-radius:6px;background:#000}} header{{display:flex;gap:8px;align-items:center}}
.score{{margin-left:auto;background:var(--acc);color:#fff;border-radius:6px;padding:2px 8px;font-weight:700}}
.st,.sub,.ts{{color:var(--mut);font-size:13px}} p{{margin:6px 0}} a{{color:var(--acc)}}</style></head><body>
<h1>{html.escape(run['source']['title'] or '')}</h1>
<p class="sub">Run {html.escape(run['run_id'])} · {hms(run['range'][0])}–{hms(run['range'][1])} analysed ·
scores are an editorial ranking, not a prediction of views. Polish with: clip polish --run {html.escape(run['run_id'])} --candidates 1</p>
<main>{''.join(cards)}</main></body></html>"""
    path = os.path.join(run_dir, "review.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


# ---------------------------------------------------------------- main entry

def discover(src: str, top: int, campaign: Dict, rng: Optional[Tuple[float, float]] = None,
             min_score: float = 55.0, run_id: Optional[str] = None) -> Dict:
    studio.stage("Source verification", "Inspecting original source and channel")
    info = inspect_source(src)
    print(f"[discover] {info['platform']}: {info['title']!r} ({hms(info['duration'])})", flush=True)
    for ch in info["chapters"]:
        print(f"           chapter {hms(ch['start'])} {ch['title']}", flush=True)
    allowed = campaign.get("allowed_channels") or []
    if allowed and info["platform"] != "local" and info["channel"] not in allowed:
        raise RuntimeError(f"Campaign {campaign['name']} only allows {allowed}; this video is from {info['channel']!r}")

    rng = rng or (0.0, info["duration"])
    run_id = run_id or f"{datetime.now():%Y%m%d-%H%M}_{info['key']}"
    runs_root = os.path.realpath(os.path.join(ROOT, "work", "runs"))
    run_dir = os.path.join(runs_root, run_id)
    if os.path.basename(run_id) != run_id or os.path.dirname(os.path.realpath(run_dir)) != runs_root:
        raise ValueError("Use a run ID naming a folder directly under work/runs")
    run = read_json(os.path.join(run_dir, "run.json")) or {
        "run_id": run_id, "created": datetime.now().isoformat(timespec="seconds"), "source": info,
        "campaign": campaign["id"], "range": list(rng), "top": top, "source_dir": None}
    if run["source"]["key"] != info["key"] or run["campaign"] != campaign["id"]:
        raise ValueError("Existing run belongs to a different source or campaign")
    studio.attach(run_id)
    cache_dir = os.path.join(run_dir, "analysis")
    temp_dir = os.path.join(run_dir, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    os.environ.update(TEMP=temp_dir, TMP=temp_dir)
    tempfile.tempdir = temp_dir
    write_json(os.path.join(cache_dir, "source.json"), info)
    write_json(os.path.join(run_dir, "run.json"), run)

    print("[discover] checking Gemini access…", flush=True)
    llm('Reply with {"ok": true}')  # stops here, before hours of CPU work, if the key/quota is unusable

    source_dir = run.get("source_dir") or os.path.join(run_dir, "input")
    studio.stage("Downloading and caching", "Preparing analysis footage")
    lowres = get_lowres(info, source_dir)
    run["source_dir"] = source_dir if info["platform"] != "local" else None
    write_json(os.path.join(run_dir, "run.json"), run)

    box = campaign.get("facecam_box")
    studio.stage("Transcription", "Processing cached or new chunks")
    chunks = analyse_chunks(lowres, info["duration"], cache_dir, rng, box)
    tr = signal_tracks(chunks)
    studio.stage("Highlight analysis", "Ranking transcript and gameplay signals")
    cands = window_candidates(chunks, tr, cache_dir, rng, campaign.get("content_kind", "gaming livestream"))
    for c in cands:
        c["local_visual"] = local_visual(tr, c["start"], c["end"])
        c["pre"] = score(c, False)[0]
    cands = dedupe(cands, "pre")
    shortlist = cands[: max(top + 5, int(top * 1.5))]
    print(f"[discover] {len(cands)} distinct moments found; checking the top {len(shortlist)} visually", flush=True)

    segs = [x for ch in chunks for x in ch["segments"]]
    studio.stage("Candidate selection", "Extracting and checking candidate footage")
    raw_dir = os.path.join(run_dir, "raw")
    for i, c in enumerate(shortlist, 1):
        c["id"] = i
        a, b = max(0.0, c["start"] - 1.5), min(info["duration"], c["end"] + 1.0)
        c["clip_start"], c["clip_end"] = a, b
        c["raw_clip"] = os.path.join(raw_dir, f"c{i:02d}_{hms(c['start']).replace(':', 'h', 1).replace(':', 'm')}s_{slug(c['title'])}.mp4")
        print(f"[raw {i}/{len(shortlist)}] {hms(a)}–{hms(b)} {c['title']}", flush=True)
        cut_raw(info, a, b, c["raw_clip"])
        studio.progress("candidate previews", i, len(shortlist))
        c["_frames"] = grab_frames(c["raw_clip"])
        c["thumb"] = os.path.join(run_dir, "thumbs", f"c{i:02d}.jpg")
        os.makedirs(os.path.dirname(c["thumb"]), exist_ok=True)
        with open(c["thumb"], "wb") as f:
            f.write(c["_frames"][len(c["_frames"]) // 2])
        c["excerpt"] = " ".join(x["t"] for x in segs if a <= x["s"] < b)

    vis_path = os.path.join(run_dir, "visual.json")
    visual = {int(k): v for k, v in (read_json(vis_path) or {}).items()}
    try:
        todo = [c for c in shortlist if c["id"] not in visual]
        for k in range(0, len(todo), 4):
            visual.update(visual_check(todo[k:k + 4]))
            write_json(vis_path, visual)
            print(f"[visual] checked {min(k + 4, len(todo))}/{len(todo)}", flush=True)
    except QuotaExhausted:
        print("[visual] Gemini quota exhausted — ranking the rest with local signals only", flush=True)

    for c in shortlist:
        c.pop("_frames", None)
        c["visual"] = visual.get(c["id"])
        c["facecam_box"] = box_for_corner(box, (c["visual"] or {}).get("facecam_corner"))
        c["overall"], c["notes"] = score(c, bool(campaign.get("require_facecam")), bool(campaign.get("no_third_party_media")))
        c["gameplay_score"] = (c["visual"] or {}).get("gameplay_action", c.get("gameplay_guess"))
        c["visual_score"] = (c["visual"] or {}).get("visual_quality")
        rejected = c["overall"] < min_score or (c["visual"] or {}).get("verdict") == "reject"
        c["status"] = "rejected" if rejected else "candidate"
        c.update(source_url=info["url"], start_hms=hms(c["start"]), end_hms=hms(c["end"]),
                 duration=round(c["end"] - c["start"], 1))
    shortlist.sort(key=lambda c: (c["status"] != "candidate", -c["overall"]))
    kept = [c for c in shortlist if c["status"] == "candidate"][:top]
    for c in shortlist:
        if c["status"] == "candidate" and c not in kept:
            c["status"] = "rejected"; c["notes"].append(f"below top {top}")
        c["rank"] = (kept.index(c) + 1) if c in kept else None

    write_json(os.path.join(run_dir, "candidates.json"), shortlist)
    run["finished"] = datetime.now().isoformat(timespec="seconds")
    write_json(os.path.join(run_dir, "run.json"), run)
    gallery = write_gallery(run_dir, run, shortlist)
    return {"run_id": run_id, "run_dir": run_dir, "gallery": gallery, "candidates": shortlist}
