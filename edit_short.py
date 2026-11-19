"""Polish one highlight into a finished Short (captions, hook, pacing, zooms, SFX).

Usage:
    # 1) let Gemini draft an edit plan for a source range, then render it
    python edit_short.py VIDEO --start 559.6 --end 606.2

    # 2) re-render after hand-editing the plan JSON (no new AI call)
    python edit_short.py --plan D:/GTA-VI-Clipping/output/edits/timmy_test_559.plan.json

    # 3) work from the pipeline's results JSON (short #2)
    python edit_short.py --from-results D:/GTA-VI-Clipping/output/timmy_test_results.json --index 2

Needs FACECAM_BOX and OVERLAY_PATH (make_shorts.ps1 / edit_short.ps1 set them).
"""
import argparse
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from shorts_generator.config import LOCAL_OUTPUT_DIR
from shorts_generator.local.enhance import load_plan, make_auto_plan, render_plan, save_plan


def main() -> int:
    ap = argparse.ArgumentParser(description="Polish a highlight into a finished Short")
    ap.add_argument("video", nargs="?", help="source video (the long stream file)")
    ap.add_argument("--start", type=float, help="highlight start in the source (seconds)")
    ap.add_argument("--end", type=float, help="highlight end in the source (seconds)")
    ap.add_argument("--from-results", help="results JSON written by main.py")
    ap.add_argument("--index", type=int, default=1, help="which short in --from-results (1-based)")
    ap.add_argument("--plan", help="edit plan JSON to render (created if missing)")
    ap.add_argument("--replan", action="store_true", help="ask Gemini for a fresh plan even if one exists")
    ap.add_argument("--plan-only", action="store_true", help="write the plan and stop (review before rendering)")
    ap.add_argument("--out", help="output mp4 (default: <output>/edits/<plan name>.mp4)")
    args = ap.parse_args()

    if args.from_results:
        with open(args.from_results, encoding="utf-8") as f:
            res = json.load(f)
        short = res["shorts"][args.index - 1]
        args.video = args.video or res["source_video_url"]
        args.start = args.start if args.start is not None else float(short["start_time"])
        args.end = args.end if args.end is not None else float(short["end_time"])

    edits_dir = os.path.join(LOCAL_OUTPUT_DIR, "edits")
    if not args.plan:
        if not (args.video and args.start is not None and args.end is not None):
            ap.error("give VIDEO --start --end, --from-results, or --plan")
        stem = os.path.splitext(os.path.basename(args.video))[0]
        args.plan = os.path.join(edits_dir, f"{stem}_{int(args.start)}.plan.json")

    try:
        if os.path.exists(args.plan) and not args.replan:
            plan = load_plan(args.plan)
            print(f"[edit] using plan {args.plan}")
        else:
            plan = make_auto_plan(args.video, args.start, args.end)
            save_plan(plan, args.plan)
            print(f"[edit] wrote plan {args.plan}")
        v = plan.get("verdict") or {}
        print(f"[edit] verdict: score={v.get('score')} payoff={v.get('has_payoff')} — {v.get('notes', '')}")
        if args.plan_only:
            return 0
        out = args.out or os.path.splitext(args.plan)[0].replace(".plan", "") + ".mp4"
        info = render_plan(plan, out)
    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1

    print(f"\n[edit] done: {info['output']}  ({info['duration_s']}s)")
    for p in info["pieces"]:
        print(f"   {p['output_start']:6.2f}s ← source {p['source_start']:.2f}-{p['source_end']:.2f}  {p['role']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
