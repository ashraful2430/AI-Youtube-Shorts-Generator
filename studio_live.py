"""Show Claude's step-by-step work in the Studio while a session runs.

    python studio_live.py --run <run-id> [--hours 12]

Each line appended to <run>/live-log.txt as "Stage | detail" becomes a Studio event
(use the Studio stage names, e.g. "Rendering | Mr. Mangrove v1"). Stops when
<run>/STOP-LIVE-SESSION exists or after --hours.
"""
import argparse
import os
from pathlib import Path
import time

from shorts_generator.local import studio_events as ev

ROOT = Path(os.environ.get("CLIP_ROOT", Path(__file__).resolve().parent.parent))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True)
    ap.add_argument("--hours", type=float, default=12)
    a = ap.parse_args()
    run = ROOT / "work" / "runs" / a.run
    if not (run / "run.json").is_file():
        raise SystemExit(f"no run {a.run}")
    log, stop = run / "live-log.txt", run / "STOP-LIVE-SESSION"
    log.touch()
    seen = len(log.read_text(encoding="utf-8").splitlines())  # only new lines become events
    deadline = time.time() + a.hours * 3600
    with ev.job(ROOT, "Claude live session", a.run):
        ev.stage("Source verification", "Claude session started; reels and edit requests are in the Reels tab")
        while time.time() < deadline and not stop.exists():
            lines = log.read_text(encoding="utf-8").splitlines()
            for line in lines[seen:]:
                stage, _, detail = line.partition("|")
                if stage.strip():
                    ev.stage(stage.strip(), detail.strip() or None)
            seen = len(lines)
            time.sleep(2)


if __name__ == "__main__":
    main()
