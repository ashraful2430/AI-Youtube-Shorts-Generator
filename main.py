"""CLI entry point.

Usage:
    python main.py "https://www.youtube.com/watch?v=..." \
        --num-clips 3 --aspect-ratio 9:16
"""
import argparse
import json
import os
import sys

# Windows uses 'charmap' by default, which can't encode Unicode characters
# like →. Reconfigure stdout/stderr to UTF-8 so output works on all platforms.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from shorts_generator import generate_shorts


def _parse_hms(value: str) -> float:
    """'01:00:00' / '60:00' / '3600' → seconds."""
    total = 0.0
    for part in str(value).strip().split(":"):
        total = total * 60 + float(part or 0)
    return total


def _hms(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:04.1f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="AI YouTube Shorts Generator")
    parser.add_argument("url", help="YouTube URL, file:// URL, or local file path")
    parser.add_argument(
        "--mode",
        choices=["api", "local"],
        default="api",
        help="api (default, MuAPI) or local (remote URL, file://, or local path + faster-whisper + LLM provider + ffmpeg).",
    )
    parser.add_argument("--num-clips", type=int, default=3, help="How many shorts to render (default: 3)")
    parser.add_argument("--aspect-ratio", default="9:16", help="Output aspect ratio (default: 9:16)")
    parser.add_argument("--format", default="720", help="Source download resolution: 360 / 480 / 720 / 1080 (default: 720)")
    parser.add_argument("--language", default=None, help="Force Whisper language code, e.g. 'en' (default: auto-detect)")
    parser.add_argument("--output-json", default=None, help="Write the full result JSON to this path")
    parser.add_argument(
        "--layout",
        choices=["crop", "split"],
        default=None,
        help="local mode: crop (face-tracking crop) or split (facecam top + overlay + gameplay; needs FACECAM_BOX).",
    )
    parser.add_argument(
        "--source-offset",
        default="0",
        help="Where this file starts inside the original VOD, e.g. 01:00:00 — adds VOD timestamps to the JSON.",
    )
    args = parser.parse_args()
    if args.layout:
        os.environ["LOCAL_LAYOUT"] = args.layout
    offset = _parse_hms(args.source_offset)

    try:
        result = generate_shorts(
            youtube_url=args.url,
            num_clips=args.num_clips,
            aspect_ratio=args.aspect_ratio,
            download_format=args.format,
            language=args.language,
            mode=args.mode,
        )
    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1

    print("\n" + "=" * 72)
    print(f"Mode:          {result.get('mode', args.mode)}")
    print(f"Source video:  {result['source_video_url']}")
    print(f"Highlights:    {len(result['highlights'])} candidates → kept top {len(result['shorts'])}")
    print("=" * 72)
    for i, s in enumerate(result["shorts"], 1):
        s["file_start"], s["file_end"] = _hms(s["start_time"]), _hms(s["end_time"])
        s["vod_start"], s["vod_end"] = _hms(s["start_time"] + offset), _hms(s["end_time"] + offset)
        s["duration_s"] = round(s["end_time"] - s["start_time"], 1)
        print(f"\n#{i}  score={s.get('score')}  {s.get('start_time'):.1f}s → {s.get('end_time'):.1f}s")
        print(f"     file:   {s['file_start']} → {s['file_end']}  ({s['duration_s']}s)")
        if offset:
            print(f"     VOD:    {s['vod_start']} → {s['vod_end']}")
        print(f"     title:  {s.get('title')}")
        print(f"     hook:   {s.get('hook_sentence')}")
        if s.get("clip_url"):
            print(f"     clip:   {s['clip_url']}")
        else:
            print(f"     clip:   FAILED ({s.get('error')})")

    if args.output_json:
        result["source_offset"] = _hms(offset)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nFull JSON written to {args.output_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
