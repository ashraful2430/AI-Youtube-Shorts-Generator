"""One command for the whole clipping workflow.

  clip discover <url-or-mp4> --campaign skiimasktimmy [--top 20] [--range 2:00:00-3:18:40]
  clip polish   --run <run-id> --candidates 1,3 [--zoom-scale 1.5] [--no-sfx] [--replan]
  clip runs                                            (list runs with size and status)

Use approve --help and clean --help for the publishing workflow.
"""
import argparse
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.getenv("CLIP_ROOT", r"D:\GTA-VI-Clipping")
RUNS = os.path.join(ROOT, "work", "runs")
from publishing import register
from shorts_generator.local import studio_events as studio
from shorts_generator.local import runs as run_roots


def load_campaign(name: str) -> dict:
    path = os.path.join(HERE, "campaigns", f"{name.lower()}.json")
    if not os.path.exists(path):
        sys.exit(f"Unknown campaign {name!r}: create {path} (copy campaigns/skiimasktimmy.json)")
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    c["overlay"] = os.path.join(ROOT, c["overlay"])
    c["id"] = name.lower()
    return c


def run_dir(run_id: str) -> str:
    # Trust boundary: a run id must name exactly one folder directly under work/runs or campaigns/<id>/work/runs.
    d = run_roots.locate(ROOT, run_id)
    if d is None:
        sys.exit(f"No run {run_id!r} in {RUNS} or campaigns/*/work/runs (see: clip runs)")
    return str(d)


def read(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ids(text) -> list:
    text = ",".join(text) if isinstance(text, list) else text  # PowerShell turns 1,3 into two args
    return [int(x) for x in text.replace(" ", "").split(",") if x]


def size_of(paths) -> int:
    total = 0
    for p in paths:
        if os.path.isfile(p):
            total += os.path.getsize(p)
        elif os.path.isdir(p):
            total += sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)
    return total


# ---------------------------------------------------------------- commands

def cmd_discover(a):
    from shorts_generator.local.discover import QuotaExhausted, discover, parse_hms
    camp = load_campaign(a.campaign)
    rng = tuple(parse_hms(x) for x in a.range.split("-")) if a.range else None
    try:
        res = discover(a.source, a.top, camp, rng, a.min_score, a.run_id)
    except QuotaExhausted as e:
        sys.exit(f"\nGemini free-tier quota is used up ({e}).\nProgress is saved; run the same command again "
                 f"after the quota resets (add --run-id <id> to continue the same run).")
    kept = [c for c in res["candidates"] if c["status"] == "candidate"]
    print(f"\nRun: {res['run_id']}\nReview gallery: {res['gallery']}\n")
    for c in kept:
        print(f"#{c['id']:>2} rank {c['rank']:>2}  score {c['overall']:>5}  {c['start_hms']}–{c['end_hms']}  {c['title']}")
    print(f"\n{len(kept)} candidates kept, {len(res['candidates']) - len(kept)} rejected after checks.")


OPENING_CHECKS = (
    "moving_frame_zero", "clear_action_or_reaction", "clear_first_frame_context",
    "narration_fits_action", "unique_source_intervals", "streamer_speech_clear",
    "disclosure_timing_and_readability",
)


def opening_review_flags(review):
    """Return missing or failed director checks for the first two seconds."""
    opening = review.get("opening_review")
    if not isinstance(opening, dict):
        return ["opening_review"]
    flags = [key for key in OPENING_CHECKS if opening.get(key) is not True]
    score = opening.get("entertainment_score")
    if type(score) not in (int, float) or not 1 <= score <= 10:
        flags.append("entertainment_score")
    evidence = opening.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        flags.append("evidence")
    return flags


def editorial_ready(folder, candidate):
    review = read(os.path.join(folder, "editorial.json"), {}).get(str(candidate["id"]), {})
    if candidate["status"] != "candidate" or review.get("decision") != "select":
        return False
    if review.get("source_window") != [candidate["clip_start"], candidate["clip_end"]]:
        return False
    return all(isinstance(review.get(key), str) and review[key].strip()
               for key in ("hook", "context", "escalation", "visible_payoff", "reaction", "ending", "dialogue", "contact_sheet"))


def cmd_polish(a):
    d = run_dir(a.run)
    run = read(os.path.join(d, "run.json"))
    camp = load_campaign(run["campaign"])
    temp_dir = os.path.join(d, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    os.environ.update(TEMP=temp_dir, TMP=temp_dir, LOCAL_OUTPUT_DIR=d, OVERLAY_PATH=camp["overlay"],
                      FACECAM_BOX=",".join(map(str, camp["facecam_box"])))
    from shorts_generator.local.discover import cut_raw, slug
    from shorts_generator.local.enhance import load_plan, make_auto_plan, render_plan, save_plan
    cands = {c["id"]: c for c in read(os.path.join(d, "candidates.json"), [])}
    selected = ids(a.candidates)
    studio.stage("Candidate selection", "Checking the recorded director review")
    for cid in selected:
        c = cands.get(cid) or sys.exit(f"No candidate {cid} in run {a.run}")
        if not editorial_ready(d, c):
            sys.exit(f"Candidate {cid} needs a director review in {os.path.join(d, 'editorial.json')}.")
        review = read(os.path.join(d, "editorial.json"), {}).get(str(cid), {})
        flags = opening_review_flags(review)
        if flags:
            sys.exit(f"Candidate {cid} needs opening review in editorial.json: {', '.join(flags)}")
    for cid in selected:
        c = cands[cid]
        # the corner where discovery actually found Timmy's face, not just the profile default
        os.environ["FACECAM_BOX"] = ",".join(map(str, c.get("facecam_box") or camp["facecam_box"]))
        studio.stage("Downloading and caching", f"Candidate #{cid} source footage")
        print(f"[polish] #{cid}: fetching the 1080p source segment…", flush=True)
        hq = cut_raw(run["source"], c["clip_start"], c["clip_end"], os.path.join(d, "hq", f"c{cid:02d}.mp4"), 1080)
        studio.stage("Timeline assembly", f"Candidate #{cid} edit plan")
        plan_path = os.path.join(d, "plans", f"c{cid:02d}.plan.json")
        if os.path.exists(plan_path) and not a.replan:
            plan = load_plan(plan_path)
        else:
            dur = c["clip_end"] - c["clip_start"]
            plan = make_auto_plan(hq, 1.0, dur - 0.5, pad=1.5)
        if a.zoom_scale is not None:
            plan["zoom_scale"] = a.zoom_scale
        if a.no_sfx:
            plan["sfx"] = False
        if a.sfx:
            plan["sfx"] = True
        save_plan(plan, plan_path)
        v = plan.get("verdict") or {}
        print(f"[polish] #{cid} {c['title']} — editor verdict {v.get('score')}/10: {v.get('notes', '')}")
        out = os.path.join(d, "polished", f"c{cid:02d}_{slug(c['title'])}.mp4")
        info = render_plan(plan, out)
        studio.stage("Compliance and quality checks", "Review export available. Campaign approval remains manual.")
        c.setdefault("polished", out)
        print(f"[polish] #{cid} → {info['output']} ({info['duration_s']}s)")
    with open(os.path.join(d, "candidates.json"), "w", encoding="utf-8") as f:
        json.dump(list(cands.values()), f, indent=1, ensure_ascii=False)


def cmd_runs(a):
    names = run_roots.names(ROOT)
    if not names:
        print("no runs yet")
        return
    for r in names:
        d = str(run_roots.locate(ROOT, r))
        run = read(os.path.join(d, "run.json"))
        if not run:
            continue
        cands = read(os.path.join(d, "candidates.json"), [])
        kept = sum(c["status"] == "candidate" for c in cands)
        print(f"{r}  {size_of([d]) / 1e6:8.1f} MB  "
              f"{'done' if run.get('finished') else 'unfinished'}  {kept} candidates  {run['source']['title']}")


def main():
    ap = argparse.ArgumentParser(prog="clip", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover", help="analyse a whole video and cut ranked raw candidates")
    p.add_argument("source")
    p.add_argument("--campaign", required=True)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--range", help="only this part, e.g. 2:00:00-3:18:40 (cached analysis is reused)")
    p.add_argument("--min-score", type=float, default=55.0)
    p.add_argument("--run-id", help="continue an existing run")
    p.set_defaults(fn=cmd_discover)
    p = sub.add_parser("polish", help="edit candidates into finished vertical Shorts")
    p.add_argument("--run", required=True)
    p.add_argument("--candidates", required=True, nargs="+", help="e.g. 1,3,5 or 1 3 5")
    p.add_argument("--zoom-scale", type=float, help="1.0 normal, 1.5 = stronger zooms")
    p.add_argument("--no-sfx", action="store_true")
    p.add_argument("--sfx", action="store_true", help="turn sound effects back on")
    p.add_argument("--replan", action="store_true", help="ask Gemini for a fresh edit plan")
    p.set_defaults(fn=cmd_polish)
    register(sub, ROOT, run_dir, read)
    sub.add_parser("runs", help="list runs").set_defaults(fn=cmd_runs)
    a = ap.parse_args()
    if a.cmd in ("discover", "polish"):
        from studio import start_background

        start_background()
        with studio.job(ROOT, a.cmd, getattr(a, "run", None) or getattr(a, "run_id", None)):
            a.fn(a)
    else:
        a.fn(a)


if __name__ == "__main__":
    main()
