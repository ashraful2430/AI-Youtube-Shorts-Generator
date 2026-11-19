"""Polish a stream highlight into a finished Short from an *edit plan*.

An edit plan is a small JSON file (source times in seconds):

    {
      "source": "D:/.../timmy_test.mp4",
      "window": [552, 612],                  # range transcribed word-by-word
      "verdict": {"score": 6, "has_payoff": true, "notes": "..."},
      "hook_text": "WHY IS ANIME SO COMPLICATED?",   # optional; must reflect the footage
      "segments": [                           # played in this order
        {"start": 579.0, "end": 583.3, "role": "hook"},
        {"start": 569.1, "end": 597.0, "role": "story", "tighten": true}
      ],
      "keywords": ["complicated", "50 kids"],  # words that get the accent colour
      "zooms": [{"at": 585.2, "amount": 1.18, "hold": 0.9, "impact": true}],
      "censor_captions": true,
      "max_gap": 0.55
    }

`make_auto_plan` asks Gemini to fill one in from word timestamps (it only
picks word *indices*, so it can't invent dialogue or times). The renderer:

  * cuts the segments, removing dead air inside "tighten" segments (pauses
    longer than max_gap between words) and alternating a slight zoom on each
    jump cut so it reads as an edit, not a glitch;
  * keeps the split layout (facecam top / official overlay / gameplay) and the
    full-camera scene handling from clipper.py;
  * burns word-by-word ASS captions (active word pops, keywords coloured);
  * adds punch zooms, a whoosh on each segment change, impacts on flagged
    zooms, all from sfx.py (synthesized, copyright-free) at low level;
  * loudness-normalises the original stream audio to -14 LUFS.
"""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

from . import sfx
from .clipper import _cam_scene_intervals, parse_facecam_box, split_geometry
from .words import word_transcript

FONT_FILE = os.getenv("CAPTION_FONT_FILE", r"C:\Windows\Fonts\seguibl.ttf")
FONT_NAME = os.getenv("CAPTION_FONT_NAME", "Segoe UI Black")

# ASS colours are &HBBGGRR&
WHITE, YELLOW, GREEN = "&H00FFFFFF&", "&H0000D4FF&", "&H006BFF3C&"
CYAN, PURPLE = "&H00FFE500&", "&H00FF7BB5&"   # #00E5FF, #B57BFF

_PROFANITY = {
    "fucking": "F***ING", "fuckin": "F***IN", "fucked": "F***ED", "fuck": "F**K",
    "motherfucker": "MOTHERF***ER", "shit": "SH*T", "bitch": "B***H",
}


# ----------------------------------------------------------------------------
# Plan → timeline
# ----------------------------------------------------------------------------

def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def build_pieces(plan: Dict, words: List[Dict]) -> List[Dict]:
    """Expand plan segments into contiguous source pieces, removing dead air."""
    pieces: List[Dict] = []
    for si, seg in enumerate(plan["segments"]):
        if "freeze" in seg:  # hold one source frame (pattern interrupt / callout), silent
            t = float(seg["freeze"])
            pieces.append({"a": t, "b": t, "seg": si, "role": seg.get("role", "freeze"), "first_of_seg": True,
                           "freeze": True, "speed": 1.0, "mute": True, "len": float(seg.get("dur", 1.0))})
            continue
        speed = float(seg.get("speed", 1.0))
        mute = bool(seg.get("mute", speed != 1.0))  # slowed replays lose their (chipmunk/slurred) audio
        max_gap = float(seg.get("max_gap", plan.get("max_gap", 0.55)))
        a, b = float(seg["start"]), float(seg["end"])
        cuts = [(a, b)]
        if seg.get("tighten"):
            inside = [w for w in words if w["start"] >= a - 0.05 and w["end"] <= b + 0.05]
            cuts, cur = [], a
            for w0, w1 in zip(inside, inside[1:]):
                if w1["start"] - w0["end"] > max_gap:
                    cut_a, cut_b = w0["end"] + 0.18, w1["start"] - 0.10
                    if cut_b - cut_a > 0.25:
                        cuts.append((cur, cut_a))
                        cur = cut_b
            cuts.append((cur, b))
        for j, (pa, pb) in enumerate(cuts):
            if pb - pa > 0.15:
                pieces.append({"a": pa, "b": pb, "seg": si, "role": seg.get("role", ""), "first_of_seg": j == 0,
                               "speed": speed, "mute": mute, "len": (pb - pa) / speed})
    t = 0.0
    for p in pieces:
        p["out"] = t
        t += p["len"]
    return pieces


def _out(p: Dict, src_t: float) -> float:
    return p["out"] + (0.0 if p.get("freeze") else (src_t - p["a"]) / p["speed"])


def _map_time(pieces: List[Dict], src_t: float) -> List[float]:
    """All output times at which source time `src_t` is shown."""
    return [_out(p, src_t) for p in pieces if p["a"] - 1e-3 <= src_t <= p["b"] + 1e-3]


def apply_caption_fixes(words: List[Dict], fixes: Dict[str, str]) -> List[Dict]:
    """Plan "caption_fixes": {"<source time of word>": "replacement"} for verified mishearings.

    An empty replacement merges the word into the previous one (e.g. "into" + "twined"
    → fix the first to "intertwined" and the second to "").
    """
    if not fixes:
        return words
    out: List[Dict] = []
    for w in words:
        new = None
        for key, val in fixes.items():
            if abs(float(key) - w["start"]) < 0.06:
                new = val
        if new is None:
            out.append(w)
        elif new == "" and out:
            out[-1] = {**out[-1], "end": w["end"]}
        elif new:
            out.append({**w, "word": new})
    return out


def timeline_words(pieces: List[Dict], words: List[Dict]) -> List[Dict]:
    out = []
    for p in pieces:
        if p["mute"]:
            continue  # no captions over silent replays / freezes
        for w in words:
            mid = (w["start"] + w["end"]) / 2
            if p["a"] <= mid <= p["b"]:
                s = _out(p, max(w["start"], p["a"]))
                e = _out(p, min(w["end"], p["b"]))
                out.append({**w, "t0": s, "t1": max(e, s + 0.08), "piece": id(p)})
    return out


# ----------------------------------------------------------------------------
# Captions (ASS)
# ----------------------------------------------------------------------------

def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _caption_text(word: str, censor: bool) -> str:
    text = word.upper().replace("{", "(").replace("}", ")").replace("\\", "")
    if censor:
        for bad, good in _PROFANITY.items():
            text = re.sub(rf"\b{bad.upper()}\b", good, text)
    return text


def _keyword_set(plan: Dict) -> set:
    keys = set()
    for k in plan.get("keywords", []):
        for part in str(k).split():
            if _norm(part):
                keys.add(_norm(part))
    return keys


def _chunks(tw: List[Dict], max_words: int = 3, max_chars: int = 16) -> List[List[Dict]]:
    chunks, cur = [], []
    for w in tw:
        brk = False
        if cur:
            gap = w["t0"] - cur[-1]["t1"]
            chars = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
            brk = (len(cur) >= max_words or chars > max_chars or gap > 0.35
                   or w["piece"] != cur[-1]["piece"] or cur[-1]["word"][-1:] in ".?!,")
        if brk:
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def write_ass(path: str, plan: Dict, tw: List[Dict], geo: Dict, duration: float,
              pieces: Optional[List[Dict]] = None) -> None:
    W, H = geo["W"], geo["H"]
    base_y = int(plan.get("caption_y", geo["game_y"] + 150))  # below the overlay band, above the face in cam scenes
    # "caption_y_overrides": [[source_start, source_end, y], ...] — move captions off an important visual.
    moves = []
    for s0, s1, y in plan.get("caption_y_overrides", []):
        for p in pieces or []:
            a, b = max(float(s0), p["a"]), min(float(s1), p["b"])
            if b > a:
                moves.append((_out(p, a), _out(p, b), int(y)))

    def cap_y_at(t: float) -> int:
        return next((y for a, b, y in moves if a <= t < b), base_y)

    hook_y = int(plan.get("hook_y", geo["game_y"] + 55))  # top edge of gameplay: never over the action
    censor = bool(plan.get("censor_captions", True))
    keys = _keyword_set(plan)
    bigs = {_norm(w) for w in plan.get("big_words", [])}
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{FONT_NAME},{int(plan.get('caption_size', 84))},{WHITE},{WHITE},&H00000000&,&H96000000&,0,0,0,0,100,100,1,0,1,7,4,5,40,40,0,1",
        f"Style: Hook,{FONT_NAME},64,{YELLOW},{YELLOW},&H00000000&,&HC0000000&,0,0,0,0,100,100,1,0,3,14,0,5,60,60,0,1",
        f"Style: Meme,Impact,118,{WHITE},{WHITE},&H00000000&,&H96000000&,0,0,0,0,100,100,2,0,1,9,5,5,40,40,0,1",
        f"Style: Ring,Arial,20,&HFF000000&,&HFF000000&,{YELLOW},&H00000000&,0,0,0,0,100,100,0,0,1,9,0,7,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    hook_text = str(plan.get("hook_text") or "").strip()
    if hook_text:
        txt = _caption_text(hook_text, censor)
        # reveal: drops in from above with an overshoot, then fades out
        lines.append(
            f"Dialogue: 2,{_ass_time(0.05)},{_ass_time(min(2.6, duration))},Hook,,0,0,0,,"
            f"{{\\move({W // 2},{hook_y - 90},{W // 2},{hook_y},0,180)\\fscx70\\fscy70\\t(0,160,\\fscx106\\fscy106)"
            f"\\t(160,260,\\fscx100\\fscy100)\\fad(0,180)}}{txt}"
        )

    def game_xy(fx: float, fy: float) -> Tuple[int, int]:
        """Source-frame fraction → output pixel inside the gameplay panel."""
        x = (fx * geo["src_w"] - geo["game_x"]) / geo["game_w"] * W
        return int(x), int(geo["game_y"] + fy * geo["game_out_h"])

    # Freeze-frame callouts: animated ring + arrow + label on the gameplay detail.
    for p in pieces or []:
        seg = plan["segments"][p["seg"]]
        co = seg.get("callout") if p.get("freeze") else None
        if not co:
            continue
        cx, cy = game_xy(float(co["x"]), float(co["y"]))
        r = int(float(co.get("r", 0.06)) * W)
        k = int(r * 0.5523)
        # Circle drawn in 0..2r coordinates and placed by its top-left corner: libass aligns drawings
        # by bounding box, so a centred (negative-coordinate) path lands one radius off.
        c = r
        ring = (f"m {c + r} {c} b {c + r} {c + k} {c + k} {c + r} {c} {c + r} b {c - k} {c + r} {c - r} {c + k} {c - r} {c} "
                f"b {c - r} {c - k} {c - k} {c - r} {c} {c - r} b {c + k} {c - r} {c + r} {c - k} {c + r} {c}")
        s, e = _ass_time(p["out"]), _ass_time(p["out"] + p["len"])
        lines.append(f"Dialogue: 3,{s},{e},Ring,,0,0,0,,{{\\an7\\pos({cx - r},{cy - r})\\bord22"
                     f"\\t(0,200,\\bord9)\\p1}}{ring}{{\\p0}}")
        # Arrow from the upper-right whose tip touches the ring; absolute coords, drawn from its bbox corner.
        tip = (cx + 0.71 * r + 14, cy - 0.71 * r - 14)
        local = [(0, 0), (46, -30), (46, -11), (170, -11), (170, 11), (46, 11), (46, 30)]  # pointing left
        ang = math.radians(-45)  # rotate so the shaft goes up-right and the tip points down-left
        pts = [(tip[0] + x * math.cos(ang) - y * math.sin(ang), tip[1] + x * math.sin(ang) + y * math.cos(ang))
               for x, y in local]
        mx, my = min(p[0] for p in pts), min(p[1] for p in pts)
        draw = "m " + " l ".join(f"{int(x - mx)} {int(y - my)}" for x, y in pts)
        lines.append(f"Dialogue: 3,{s},{e},Ring,,0,0,0,,{{\\an7\\pos({int(mx)},{int(my)})\\1c{YELLOW}\\1a&H00&"
                     f"\\bord4\\3c&H000000&\\fad(120,0)\\p1}}{draw}{{\\p0}}")
        if co.get("label"):
            lx = min(W - 220, int(max(p[0] for p in pts)) + 20)
            ly = max(geo["game_y"] + 80, int(my) - 40)
            lines.append(f"Dialogue: 4,{s},{e},Meme,,0,0,0,,{{\\pos({lx},{ly})\\fs84\\c{YELLOW}"
                         f"\\fscx60\\fscy60\\t(120,260,\\fscx100\\fscy100)}}{_caption_text(co['label'], censor)}")

    # Meme / reveal text: {"at": source_s, "dur": s, "text": "...", "y": px, "color": "yellow|cyan|purple"}
    for m in plan.get("texts", []):
        times = _map_time(pieces or [], float(m["at"])) if "at" in m else [float(m.get("out", 0))]
        if not times:
            continue
        t = times[int(m.get("occurrence", 0)) % len(times)]
        col = {"yellow": YELLOW, "cyan": CYAN, "purple": PURPLE}.get(m.get("color", "white"), WHITE)
        y = int(m.get("y", geo["game_y"] + 420))
        scale, overshoot = (92, 103) if m.get("subtle") else (40, 112)
        font_size = int(m.get("size", 118))
        lines.append(f"Dialogue: 4,{_ass_time(t)},{_ass_time(min(duration, t + float(m.get('dur', 1.4))))},Meme,,0,0,0,,"
                     f"{{\\pos({W // 2},{y})\\c{col}\\frz{m.get('tilt', -4)}\\fs{font_size}\\fscx{scale}\\fscy{scale}"
                     f"\\t(0,140,\\fscx{overshoot}\\fscy{overshoot})\\t(140,240,\\fscx100\\fscy100)\\fad(0,150)}}"
                     f"{_caption_text(m['text'], censor)}")

    chunks = _chunks(tw)
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0]["t0"] if ci + 1 < len(chunks) else duration
        chunk_end = min(nxt, ch[-1]["t1"] + 0.35)
        for wi, w in enumerate(ch):
            s = ch[0]["t0"] if wi == 0 else w["t0"]
            e = ch[wi + 1]["t0"] if wi + 1 < len(ch) else chunk_end
            if e - s < 0.04:
                continue
            parts = []
            for xi, x in enumerate(ch):
                txt = _caption_text(x["word"], censor)
                is_key = _norm(x["word"]) in keys
                is_big = _norm(x["word"]) in bigs
                if xi == wi and is_big:  # reaction peak: large purple word with a wobble
                    parts.append(f"{{\\c{PURPLE}\\fscx175\\fscy175\\frz-6\\t(0,110,\\fscx140\\fscy140\\frz3)"
                                 f"\\t(110,220,\\fscx150\\fscy150\\frz0)}}{txt}{{\\r}}")
                elif xi == wi:
                    big = 122 if is_key else 112
                    col = CYAN if is_key else YELLOW
                    parts.append(f"{{\\c{col}\\fscx{big}\\fscy{big}\\t(0,90,\\fscx100\\fscy100)}}{txt}{{\\r}}")
                else:
                    parts.append(f"{{\\c{CYAN}}}{txt}{{\\r}}" if is_key else txt)
            pop_in = "\\fscx88\\fscy88\\t(0,70,\\fscx100\\fscy100)" if wi == 0 else ""
            cap_y = cap_y_at(ch[0]["t0"])
            lines.append(
                f"Dialogue: 1,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{{\\pos({W // 2},{cap_y}){pop_in}}}" + " ".join(parts)
            )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ----------------------------------------------------------------------------
# Zoom expressions (evaluated per frame by zoompan; `it` = output seconds)
# ----------------------------------------------------------------------------

def _bump(a: float, amp: float, hold: float, attack: float = 0.12, release: float = 0.35) -> str:
    b, c, d = a + attack, a + attack + hold, a + attack + hold + release
    return (f"{amp:.3f}*if(lt(it,{a:.3f}),0,if(lt(it,{b:.3f}),(it-{a:.3f})/{attack},"
            f"if(lt(it,{c:.3f}),1,if(lt(it,{d:.3f}),1-(it-{c:.3f})/{release},0))))")


def _zoom_exprs(plan: Dict, pieces: List[Dict], hidden: List[Tuple[float, float]], duration: float) -> Tuple[str, str, str, str]:
    cam_terms, game_terms = ["1"], ["1"]
    if plan.get("game_push", True):
        game_terms.append(f"0.035*it/{max(duration, 1):.2f}")
    # Jump-cut punch: alternate a slight zoom on consecutive pieces of a tightened segment.
    alt = 0
    for p in pieces:
        alt = 0 if p["first_of_seg"] else 1 - alt
        if alt:
            term = f"0.06*between(it,{p['out']:.3f},{p['out'] + p['len']:.3f})"
            cam_terms.append(term)
            game_terms.append(term)
    for z in plan.get("zooms", []):
        amp = (float(z.get("amount", 1.15)) - 1.0) * float(plan.get("zoom_scale", 1.0))
        hold = float(z.get("hold", 0.8))
        times = _map_time(pieces, float(z["at"]))
        if "occurrence" in z:
            i = int(z["occurrence"])
            times = times[i:i + 1]
        for t in times:
            term = _bump(t, amp, hold)
            cam_terms.append(term)
            # In full-camera scenes the lower panel shows the face: zoom that too.
            if z.get("target") == "game" or any(a <= t <= b for a, b in hidden):
                game_terms.append(term)
    # Screen shake on real impacts: decaying sine offset; zoom in a little so the crop has room to move.
    shake = []
    for s in plan.get("shakes", []):
        times = _map_time(pieces, float(s["at"]))
        for t in times[int(s.get("occurrence", 0)) % len(times):][:1] if times else []:
            d, amp = float(s.get("dur", 0.35)), float(s.get("amp", 0.012))
            env = f"between(it,{t:.3f},{t + d:.3f})*(1-(it-{t:.3f})/{d})"
            shake.append((env, amp))
            cam_terms.append(f"0.05*{env}")
            game_terms.append(f"0.05*{env}")
    sx = "+".join(f"iw*{a}*sin(it*83)*{e}" for e, a in shake) or "0"
    sy = "+".join(f"ih*{a}*cos(it*71)*{e}" for e, a in shake) or "0"
    return "+".join(cam_terms), "+".join(game_terms), sx, sy


# ----------------------------------------------------------------------------
# Render
# ----------------------------------------------------------------------------

from . import studio_events as studio


@studio.track_render
def render_plan(plan: Dict, out_path: str, overlay: Optional[str] = None) -> Dict:
    studio.stage("Captions", "Preparing word-level transcription")
    source = plan["source"]
    w0, w1 = plan["window"]
    words = word_transcript(source, float(w0), float(w1))
    pieces = build_pieces(plan, words)
    if not pieces:
        raise RuntimeError("edit plan produced no pieces")
    intro = [piece for piece in pieces if piece["role"] == "disclosure"]
    if intro:
        from compliance import disclosure_lead_in

        if pieces[:len(intro)] != intro or any(piece.get("freeze") for piece in intro):
            raise ValueError("The disclosure must open on moving source footage")
        main = [piece for piece in pieces[len(intro):] if not piece.get("freeze")]
        if not main:
            raise ValueError("The disclosure needs a main highlight")
        # The exported transition is still reviewed separately in preflight.
        status, reason = disclosure_lead_in({"disclosure_lead_in": {
            "intro": [[p["a"], p["b"]] for p in intro],
            "highlight": [[p["a"], p["b"]] for p in main],
            "rendered_transition_checked": True,
        }}, True)
        if status != "PASS":
            raise ValueError(reason)
        if any(i["a"] <= p["a"] < i["b"] for i in intro for p in pieces if p.get("freeze")):
            raise ValueError("A highlight freeze frame reuses the disclosure footage")
    duration = sum(p["len"] for p in pieces)
    tw = timeline_words(pieces, apply_caption_fixes(words, plan.get("caption_fixes") or {}))

    box = parse_facecam_box()
    geo = split_geometry(source, box)
    W, H, BAND = geo["W"], geo["H"], geo["BAND"]
    fps = min(geo["fps"], 30.0)  # 60 fps sources render at 30: half the CPU, same look on phones
    overlay = overlay or os.getenv("OVERLAY_PATH", "").strip()
    if not overlay or not os.path.isfile(overlay):
        raise RuntimeError("OVERLAY_PATH must point to the official campaign overlay image")

    hidden: List[Tuple[float, float]] = []
    for p in pieces:
        if p.get("freeze"):
            continue
        for a, b in _cam_scene_intervals(source, box, p["a"], p["b"]):
            hidden.append((_out(p, p["a"] + a), _out(p, p["a"] + b)))
    zcam, zgame, sx, sy = _zoom_exprs(plan, pieces, hidden, duration)

    # Sound design: whoosh into each new segment, impact on flagged zooms, pop under the hook title.
    events: List[Tuple[float, str]] = []
    if plan.get("automatic_sfx", True):
        if plan.get("hook_text"):
            events.append((0.05, "pop"))
        # Whoosh only where the edit visibly jumps: a role change or a >5 s skip in the source.
        for prev, p in zip(pieces, pieces[1:]):
            if p["first_of_seg"] and (p["role"] != prev["role"] or abs(p["a"] - prev["b"]) > 5.0):
                events.append((p["out"], "whoosh"))
        for z in plan.get("zooms", []):
            times = _map_time(pieces, float(z["at"]))
            if z.get("impact") and times:
                events.append((times[0], "impact"))   # once, even if the moment is replayed in the hook
        for p in pieces:
            if p.get("freeze"):
                events.append((p["out"], "scratch"))  # freeze-frame = record-scratch stop
    # Plan-placed stingers on real reactions: {"at": source_s, "sound": "buzz|ding|pop|impact", "occurrence": 0}
    for ev in plan.get("sfx_events", []):
        times = _map_time(pieces, float(ev["at"]))
        if times:
            events.append((times[int(ev.get("occurrence", 0)) % len(times)], ev["sound"]))
    if plan.get("sfx") is False:
        events = []

    src_a = max(0.0, min(p["a"] for p in pieces) - 0.5)
    src_b = max(p["b"] for p in pieces) + 0.5
    n = len(pieces)
    full_h = int(round(W * geo["src_h"] / geo["src_w"] / 2)) * 2
    cam_h2, game_h2 = geo["cam_out_h"], geo["game_out_h"]

    tmp = tempfile.mkdtemp(prefix="edit_", dir=os.getenv("TEMP") or None)
    try:
        studio.stage("Motion graphics", "Preparing frame-timed overlays and captions")
        write_ass(os.path.join(tmp, "captions.ass"), plan, tw, geo, duration, pieces)
        studio.stage("Sound effects", "Preparing authored cues and audio mix")
        sfx_path = sfx.render_track(events, duration + 0.5, os.path.join(tmp, "sfx.wav"))
        shutil.copy(FONT_FILE, os.path.join(tmp, os.path.basename(FONT_FILE)))

        f = [f"[0:v]split={n}" + "".join(f"[vs{i}]" for i in range(n)),
             f"[0:a]asplit={n}" + "".join(f"[as{i}]" for i in range(n))]
        afmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        for i, p in enumerate(pieces):
            a, b, L = p["a"] - src_a, p["b"] - src_a, p["len"]
            if p.get("freeze"):
                f.append(f"[vs{i}]trim=start={a:.3f},trim=end_frame=1,setpts=PTS-STARTPTS,"
                         f"tpad=stop_mode=clone:stop_duration={L:.3f},trim=duration={L:.3f}[v{i}]")
            else:
                f.append(f"[vs{i}]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{p['speed']}[v{i}]")
            if p["mute"]:
                f.append(f"[as{i}]anullsink")
                f.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={L:.3f},{afmt}[a{i}]")
            else:
                f.append(f"[as{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,{afmt},"
                         f"afade=t=in:d=0.015,afade=t=out:st={max(0, L - 0.03):.3f}:d=0.03[a{i}]")
        f.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
        zp = f"x='iw/2-(iw/zoom/2)+{sx}':y='ih/2-(ih/zoom/2)+{sy}':d=1:fps={fps:.3f}"
        f += [
            f"[vc]fps={fps:.3f},split=3[c][g][fl]",
            f"[c]crop={geo['cam_w']}:{geo['cam_h']}:{geo['cam_x']}:{geo['cam_y']},"
            f"scale={2 * W}:{2 * cam_h2}:flags=lanczos,zoompan=z='{zcam}':{zp}:s={W}x{cam_h2}[cam]",
            f"[g]crop={geo['game_w']}:{geo['src_h']}:{geo['game_x']}:0,"
            f"scale={2 * W}:{2 * game_h2}:flags=lanczos,zoompan=z='{zgame}':{zp}:s={W}x{game_h2}[game]",
            f"[fl]scale={W}:{full_h}:flags=lanczos,crop={W}:{min(full_h, cam_h2)}[full]",
            f"color=c=black:s={W}x{H}:r={fps:.3f}:d={duration:.3f}[bg]",
            "[bg][cam]overlay=0:0[t0]",
        ]
        if hidden:
            enable = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in hidden)
            f.append(f"[t0][full]overlay=0:({cam_h2}-h)/2:enable='{enable}'[t1]")
        else:
            f += ["[full]nullsink", "[t0]null[t1]"]
        f += [
            f"[t1][game]overlay=0:{geo['game_y']}[t2]",
            f"[1:v]scale={W - 80}:-2:flags=lanczos[ov]",
            f"[t2][ov]overlay=(W-w)/2:{cam_h2}+({BAND}-h)/2:shortest=1[t3]",
            "[t3]ass=captions.ass:fontsdir=.,format=yuv420p[vout]",
            # 80 Hz high-pass trims game rumble (e.g. helicopter rotors) without touching the voice.
            "[ac]highpass=f=80,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo[voice]",
            # Ducking: effects drop ~10 dB whenever Timmy speaks, so they never cover his voice.
            "[voice]asplit=2[vmain][vkey]",
            f"[2:a]{afmt}[fxin]",
            "[fxin][vkey]sidechaincompress=threshold=0.02:ratio=6:attack=5:release=350[fx]",
            "[vmain][fx]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.85:level=false[aout]",
        ]
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-stats",
            "-ss", f"{src_a:.3f}", "-t", f"{src_b - src_a:.3f}", "-i", source,
            "-loop", "1", "-i", overlay,
            "-i", sfx_path,
            "-filter_complex", ";".join(f),
            "-map", "[vout]", "-map", "[aout]", "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            os.path.abspath(out_path),
        ]
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        studio.stage("Rendering", "Encoding the review MP4", os.path.abspath(out_path))
        studio.render_ffmpeg(cmd, tmp, duration)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    result = {
        "output": os.path.abspath(out_path),
        "duration_s": round(duration, 2),
        "pieces": [{"source_start": round(p["a"], 2), "source_end": round(p["b"], 2),
                    "output_start": round(p["out"], 2), "role": p["role"]} for p in pieces],
        "full_camera_intervals": [(round(a, 2), round(b, 2)) for a, b in hidden],
        "sfx": [(round(t, 2), name) for t, name in events],
        "caption_words": len(tw),
    }
    tracks = {
        "hook": [{"start": .05, "length": min(2.55, max(0, duration - .05)), "name": str(plan["hook_text"])}]
                if plan.get("hook_text") else [],
        "captions": [{"start": w["t0"], "length": w["t1"] - w["t0"], "name": w["word"]} for w in tw],
        "graphics": [{"start": times[int(m.get("occurrence", 0)) % len(times)],
                      "length": float(m.get("dur", 1.4)), "name": m["text"]}
                     for m in plan.get("texts", []) for times in
                     [_map_time(pieces, float(m["at"])) if "at" in m else [float(m.get("out", 0))]] if times],
        "sfx": [{"start": t, "length": .12, "name": name, "tip": "Cue onset"} for t, name in events],
    }
    display_pieces = [{**logged, "output_end": round(p["out"] + p["len"], 3)}
                      for logged, p in zip(result["pieces"], pieces)]
    studio.stage("Compliance and quality checks", "Recording render evidence. Campaign approval remains manual.")
    studio.save_render(os.environ.get("LOCAL_OUTPUT_DIR", ""), out_path,
                       {"duration_s": result["duration_s"], "pieces": display_pieces, "tracks": tracks})
    return result


# ----------------------------------------------------------------------------
# Automatic plan (Gemini picks word indices only)
# ----------------------------------------------------------------------------

AUTO_PLAN_PROMPT = """You are editing a vertical Short from a gaming livestream clip.
Below are the streamer's spoken words with index and time (seconds). Pick an edit that a
viewer would watch to the end: a HOOK (strongest genuine line/reaction, 1-5 s) shown first,
then the STORY in chronological order (setup → payoff), cutting rambling, repetition and
off-topic chatter. Target 20-40 s total, never pad. Only use the words given; do not invent.

Respond with JSON only:
{{"verdict": {{"score": 1-10, "has_payoff": true/false, "notes": "one honest sentence about whether this is a good Short"}},
  "hook_text": "max 6 words on-screen title that accurately describes what happens (no claims beyond the words)",
  "hook": [first_word_index, last_word_index] or null,
  "story": [[first_word_index, last_word_index], ...],
  "keywords": [word_index, ...],          # 3-8 words to colour-highlight
  "zooms": [{{"word": word_index, "impact": true/false}}]   # 2-5 reaction moments
}}

Words:
{words}"""


def make_auto_plan(source: str, start: float, end: float, pad: float = 8.0) -> Dict:
    from .llm import call_local_llm
    from ..highlights import _parse_json_loose

    w0, w1 = max(0.0, start - pad), end + pad
    words = word_transcript(source, w0, w1)
    if not words:
        raise RuntimeError("no speech found in the window")
    listing = "\n".join(f"{i}: {w['start'] - w0:.2f} {w['word']}" for i, w in enumerate(words))
    raw = call_local_llm(AUTO_PLAN_PROMPT.format(words=listing))
    data = _parse_json_loose(raw)

    def idx(i) -> int:
        return max(0, min(len(words) - 1, int(i)))

    def rng(pair) -> Tuple[float, float]:
        i, j = sorted((idx(pair[0]), idx(pair[1])))
        return round(words[i]["start"] - 0.12, 2), round(words[j]["end"] + 0.3, 2)

    segments = []
    if data.get("hook"):
        a, b = rng(data["hook"])
        segments.append({"start": a, "end": b, "role": "hook"})
    story: List[List[float]] = []
    for pair in sorted((rng(p) for p in data.get("story") or []), key=lambda r: r[0]):
        if story and pair[0] <= story[-1][1] + 0.6:   # overlapping/adjacent → one segment
            story[-1][1] = max(story[-1][1], pair[1])
        else:
            story.append(list(pair))
    for a, b in story:
        segments.append({"start": a, "end": b, "role": "story", "tighten": True})
    return {
        "source": source,
        "window": [round(w0, 2), round(w1, 2)],
        "verdict": data.get("verdict", {}),
        "hook_text": str(data.get("hook_text") or "")[:60],
        "segments": segments,
        "keywords": [words[idx(i)]["word"] for i in data.get("keywords", [])],
        "zooms": [{"at": round(words[idx(z["word"])]["start"] + 0.05, 2), "amount": 1.16,
                   "hold": 0.8, "impact": bool(z.get("impact"))} for z in data.get("zooms", [])],
        "censor_captions": True,
        "max_gap": 0.55,
    }


def save_plan(plan: Dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)


def load_plan(path: str) -> Dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
