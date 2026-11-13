"""Local clipping: ffmpeg subclip + OpenCV face-aware vertical crop.

Two stages per highlight:
  1. Cut the source video to [start, end] with ffmpeg (re-encoded, audio kept).
  2. Reframe the cut to the target aspect ratio. For 9:16 we slide a vertical
     window horizontally across the frame to keep faces centred (Haar
     cascade — same approach as the original repo, no external models).
"""
import os
import subprocess
from typing import Dict, List, Optional, Tuple

from ..config import LOCAL_OUTPUT_DIR


def _ratio(aspect_ratio: str) -> float:
    """Parse '9:16' → 9/16, '1:1' → 1.0."""
    try:
        w, h = aspect_ratio.split(":")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return 9.0 / 16.0


def _cut_subclip(source_path: str, start: float, end: float, out_path: str) -> str:
    """ffmpeg -ss start -to end → re-encoded mp4 with audio."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", source_path,
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def _reframe_vertical(in_path: str, out_path: str, aspect_ratio: str) -> str:
    """Crop the cut clip to the target aspect ratio, tracking faces if possible."""
    try:
        import cv2  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "opencv-python is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    target_ratio = _ratio(aspect_ratio)
    cap = cv2.VideoCapture(in_path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open {in_path}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    # Compute the largest crop that fits inside the frame at the target ratio.
    if target_ratio < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target_ratio)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target_ratio)
    crop_w = max(2, crop_w - (crop_w % 2))
    crop_h = max(2, crop_h - (crop_h % 2))

    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    silent_path = out_path + ".silent.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(silent_path, fourcc, fps, (crop_w, crop_h))

    last_center: Optional[Tuple[int, int]] = None
    smoothing = 0.15  # how aggressively to chase a new face position
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
        if len(faces) > 0:
            # Pick the largest face — usually the speaker.
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            cx = x + w // 2
            cy = y + h // 2
            if last_center is None:
                last_center = (cx, cy)
            else:
                lx, ly = last_center
                last_center = (
                    int(lx + (cx - lx) * smoothing),
                    int(ly + (cy - ly) * smoothing),
                )
        if last_center is None:
            last_center = (src_w // 2, src_h // 2)

        cx, cy = last_center
        x0 = max(0, min(src_w - crop_w, cx - crop_w // 2))
        y0 = max(0, min(src_h - crop_h, cy - crop_h // 2))
        cropped = frame[y0:y0 + crop_h, x0:x0 + crop_w]
        writer.write(cropped)

    cap.release()
    writer.release()

    # Mux audio from the cut clip back onto the silent reframed video.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", silent_path,
        "-i", in_path,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-map", "0:v:0", "-map", "1:a:0?",
        "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    os.remove(silent_path)
    return out_path


def _probe_video(path: str) -> Tuple[int, int, float]:
    import cv2  # type: ignore

    cap = cv2.VideoCapture(path)
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    finally:
        cap.release()
    if not w or not h:
        raise RuntimeError(f"could not read video size of {path}")
    return w, h, fps


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0)) * 2)


_SCENE_REF_CACHE: Dict[str, object] = {}


def _cam_scene_intervals(source_path: str, box: Tuple[float, float, float, float], start: float, end: float) -> List[Tuple[float, float]]:
    """Return (t0, t1) intervals, relative to `start`, where the corner facecam is NOT on screen.

    Streamers switch scenes (e.g. full-screen "just chatting" camera). We take
    the strip just below the facecam box (usually a static overlay bar), learn
    its typical look from ~60 frames across the whole video, and flag sampled
    moments in the clip where that strip no longer matches.
    """
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    fx, fy, fw, fh = box
    cap = cv2.VideoCapture(source_path)
    try:
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30.0)
        x0, x1 = int(fx * src_w), int((fx + fw) * src_w)
        y0 = int((fy + fh) * src_h)
        y1 = min(src_h, y0 + max(8, int(0.05 * src_h)))
        if y1 - y0 < 4:
            return []

        def strip_at(t: float):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            return cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY) if ok else None

        def score(a, b) -> float:
            return float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)[0][0])

        ref = _SCENE_REF_CACHE.get(source_path)
        if ref is None:
            samples = [s for s in (strip_at(total * (i + 0.5) / 60) for i in range(60)) if s is not None]
            if len(samples) < 10:
                return []
            sims = np.array([[score(a, b) for b in samples] for a in samples])
            best = int(sims.mean(axis=1).argmax())  # most "typical" look of the strip
            if (sims[best] > 0.6).mean() < 0.3:
                print("[clip/local] scene detection disabled: no stable bar under the facecam", flush=True)
                ref = False
            else:
                ref = samples[best]
            _SCENE_REF_CACHE[source_path] = ref
        if ref is False:
            return []

        step = 0.5
        hidden: List[Tuple[float, float]] = []
        t = start
        while t < end:
            s = strip_at(t)
            if s is not None and score(s, ref) < 0.6:
                a, b = t - start, min(end, t + step) - start
                if hidden and abs(hidden[-1][1] - a) < 1e-6:
                    hidden[-1] = (hidden[-1][0], b)
                else:
                    hidden.append((a, b))
            t += step
        return hidden
    finally:
        cap.release()


def _render_split(source_path: str, start: float, end: float, out_path: str) -> str:
    """1080x1920 split layout for streams with a corner facecam.

    Top: facecam (FACECAM_BOX, fractions of the frame: x,y,w,h).
    Middle: OVERLAY_PATH image (e.g. the campaign's Twitch banner).
    Bottom: centre crop of the gameplay, shifted so it excludes the facecam.
    Original audio is kept as-is (re-encoded to AAC), nothing is mixed in.
    """
    box = os.getenv("FACECAM_BOX", "").strip()
    if not box:
        raise RuntimeError(
            "--layout split needs FACECAM_BOX=x,y,w,h (fractions of the frame, e.g. 0.7676,0.7104,0.2230,0.2208)"
        )
    fx, fy, fw, fh = (float(v) for v in box.split(","))
    overlay = os.getenv("OVERLAY_PATH", "").strip()
    if overlay and not os.path.isfile(overlay):
        raise RuntimeError(f"OVERLAY_PATH does not exist: {overlay}")

    src_w, src_h, fps = _probe_video(source_path)
    W, H, BAND = 1080, 1920, 200
    cam_x, cam_y = int(fx * src_w), int(fy * src_h)
    cam_w, cam_h = _even(fw * src_w), _even(fh * src_h)
    cam_out_h = _even(W * cam_h / cam_w)
    game_y = cam_out_h + BAND
    game_out_h = H - game_y
    game_w = min(src_w, _even(src_h * W / game_out_h))
    # Centre the gameplay crop, but slide left if it would include the facecam.
    game_x = max(0, min((src_w - game_w) // 2, cam_x - game_w))
    dur = end - start

    # Where the corner facecam disappears (full-screen camera scene), the top
    # panel would show whatever is in that corner (e.g. chat). Swap in the
    # whole stream frame there instead; the lower crop already frames the face.
    hidden = _cam_scene_intervals(source_path, (fx, fy, fw, fh), start, end)
    if hidden:
        print(f"[clip/local] full-camera scene at {', '.join(f'{a:.1f}-{b:.1f}s' for a, b in hidden)} — using full frame on top", flush=True)
    full_h = _even(W * src_h / src_w)

    filters = [
        f"color=c=black:s={W}x{H}:r={fps:.3f}:d={dur:.3f}[bg]",
        f"[0:v]split=3[c][g][f]",
        f"[c]crop={cam_w}:{cam_h}:{cam_x}:{cam_y},scale={W}:{cam_out_h}:flags=lanczos[cam]",
        f"[g]crop={game_w}:{src_h}:{game_x}:0,scale={W}:{game_out_h}:flags=lanczos[game]",
        f"[f]scale={W}:{full_h}:flags=lanczos,crop={W}:{min(full_h, cam_out_h)}[full]",
        f"[bg][cam]overlay=0:0[t0]",
    ]
    if hidden:
        enable = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in hidden)
        filters.append(f"[t0][full]overlay=0:({cam_out_h}-h)/2:enable='{enable}'[t1]")
    else:
        filters.append("[full]nullsink")
        filters.append("[t0]null[t1]")
    filters.append(f"[t1][game]overlay=0:{game_y}" + ("[t2]" if overlay else ",format=yuv420p[v]"))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", source_path]
    if overlay:
        cmd += ["-loop", "1", "-i", overlay]
        filters += [
            f"[1:v]scale={W - 80}:-2:flags=lanczos[ov]",
            f"[t2][ov]overlay=(W-w)/2:{cam_out_h}+({BAND}-h)/2:shortest=1,format=yuv420p[v]",
        ]
    else:
        print("[clip/local] WARNING: OVERLAY_PATH not set — clip has no campaign overlay", flush=True)
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "0:a:0?",
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def crop_clip_local(
    source_path: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str,
    out_path: str,
) -> str:
    """Cut + reframe one highlight, returning the local mp4 path."""
    if os.getenv("LOCAL_LAYOUT", "crop").strip().lower() == "split":
        return _render_split(source_path, start_time, end_time, out_path)
    cut_path = out_path + ".cut.mp4"
    try:
        _cut_subclip(source_path, start_time, end_time, cut_path)
        _reframe_vertical(cut_path, out_path, aspect_ratio)
    finally:
        if os.path.exists(cut_path):
            os.remove(cut_path)
    return out_path


def crop_highlights_local(
    source_path: str,
    highlights: List[Dict],
    aspect_ratio: str = "9:16",
    out_dir: Optional[str] = None,
) -> List[Dict]:
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    stem = os.path.splitext(os.path.basename(source_path))[0]
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"{stem}_short_{i:02d}.mp4")
        print(f"[clip/local] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            crop_clip_local(
                source_path,
                float(h["start_time"]),
                float(h["end_time"]),
                aspect_ratio,
                out_path,
            )
            results.append({**h, "clip_url": out_path})
        except Exception as e:
            print(f"[clip/local] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results
