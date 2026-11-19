"""Self-checks for the clipping logic. Run: .venv\\Scripts\\python.exe tests\\test_clip_logic.py"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shorts_generator.local.discover import box_for_corner, corner_boxes, dedupe, hms, parse_hms, score  # noqa: E402
from shorts_generator.local import sfx  # noqa: E402
from shorts_generator.local.enhance import _map_time, apply_caption_fixes, build_pieces  # noqa: E402

from clip import OPENING_CHECKS, opening_review_flags  # noqa: E402

assert opening_review_flags({}) == ["opening_review"]
opening = dict.fromkeys(OPENING_CHECKS, True)
opening.update(entertainment_score=8, evidence="Source 01:35:42.14: visible game result and genuine reaction.")
assert opening_review_flags({"opening_review": opening}) == []
for check in OPENING_CHECKS:
    assert opening_review_flags({"opening_review": {**opening, check: False}}) == [check]
assert opening_review_flags({"opening_review": {**opening, "entertainment_score": True, "evidence": " "}}) == [
    "entertainment_score", "evidence"
]

# dedupe keeps the higher-scored of two overlapping moments, keeps distinct ones
c = [{"start": 0, "end": 30, "pre": 50}, {"start": 5, "end": 35, "pre": 80}, {"start": 100, "end": 120, "pre": 10}]
assert [x["pre"] for x in dedupe(c, "pre")] == [80, 10]

# rubric: menu screens and missing facecam are penalised; perfect clip scores 100
good = {"hook_score": 10, "reaction_score": 10, "payoff_score": 10,
        "visual": {"gameplay_action": 10, "visual_quality": 10, "activity": "combat", "facecam_visible": True},
        "local_visual": {"static_ratio": 0.1, "black_ratio": 0.0}, "facecam_ratio": 1.0}
assert score(good, True)[0] == 100
menu = {**good, "visual": {**good["visual"], "activity": "inventory", "facecam_visible": False}, "facecam_ratio": 0.0}
s, notes = score(menu, True)
assert s == 75 and len(notes) == 2, (s, notes)

react = {**good, "visual": {**good["visual"], "third_party_media": True}}
assert score(react, True, no_third_party=True)[0] == 0 and score(react, True)[0] == 100

assert hms(3725) == "1:02:05" and parse_hms("1:02:05") == 3725

# the facecam box is tried in all four corners
boxes = corner_boxes([0.7676, 0.7104, 0.2230, 0.2208])
assert len(boxes) == 4 and [0.0094, 0.7104, 0.223, 0.2208] in boxes, boxes
assert box_for_corner([0.7676, 0.7104, 0.2230, 0.2208], "bottom-left") == [0.0094, 0.7104, 0.223, 0.2208]
assert box_for_corner([0.7676, 0.7104, 0.2230, 0.2208], "full_screen") is None

# tighten: a 3 s pause between words is cut, short gaps are kept
words = [{"start": 0.0, "end": 0.5, "word": "a"}, {"start": 0.7, "end": 1.0, "word": "b"},
         {"start": 4.0, "end": 4.5, "word": "c"}]
pieces = build_pieces({"segments": [{"start": 0, "end": 5, "tighten": True}]}, words)
assert len(pieces) == 2 and abs(pieces[1]["out"] - 1.18) < 1e-6, pieces

# slowed replay (muted, double length) and freeze frames shift the timeline correctly
plan = {"segments": [{"start": 0, "end": 2, "role": "hook"}, {"start": 3, "end": 4, "speed": 0.5, "role": "replay"},
                     {"freeze": 3.5, "dur": 1.0, "role": "replay"}, {"start": 5, "end": 6}]}
p = build_pieces(plan, words)
assert [round(x["len"], 2) for x in p] == [2.0, 2.0, 1.0, 1.0] and [x["out"] for x in p] == [0, 2, 4, 5], p
assert p[1]["mute"] and p[2]["mute"] and not p[0]["mute"]
assert [round(t, 2) for t in _map_time(p, 3.5)] == [3.0, 4.0]
for name in sfx.SOUNDS:
    s = sfx.SOUNDS[name][0]()
    assert 0.2 < abs(s).max() <= 1.0 and len(s) > 1000, name

# caption fixes: replace a word, merge a split word into the previous one
w = [{"start": 1.0, "end": 1.2, "word": "into"}, {"start": 1.3, "end": 1.6, "word": "twined"}]
fixed = apply_caption_fixes(w, {"1.0": "intertwined", "1.3": ""})
assert [x["word"] for x in fixed] == ["intertwined"] and fixed[0]["end"] == 1.6

# clean refuses run ids that point outside work/runs
with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, "work", "runs"))
    env = {**os.environ, "CLIP_ROOT": root}
    clip = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "clip.py")
    r = subprocess.run([sys.executable, clip, "clean", "--run", "..\\..", "--rejected-only"], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "No run" in r.stderr, r.stderr
    assert os.path.isdir(os.path.join(root, "work", "runs"))

from compliance import reaction_montage_source_map

flashback = [{"source": [20, 22], "output": [0, 2]},
             {"source": [8, 11], "output": [2, 5]},
             {"source": [22, 24], "output": [5, 7]}]
assert reaction_montage_source_map(flashback)[0] == "PASS"
assert reaction_montage_source_map(flashback + [{"source": [21, 23], "output": [7, 9]}])[0] == "FAIL"
assert reaction_montage_source_map(flashback + [{"source": [25, 27], "output": [8, 10]}])[0] == "FAIL"
assert reaction_montage_source_map(None)[0] == "NEEDS MANUAL REVIEW"

print("all checks passed")
