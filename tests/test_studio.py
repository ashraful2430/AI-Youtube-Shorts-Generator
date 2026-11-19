"""Studio self-checks. No project media is written or reprocessed."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio
from shorts_generator.local import studio_events as events

with tempfile.TemporaryDirectory() as temp:
    studio.ROOT = Path(temp)
    studio.RUNS = studio.ROOT / "work" / "runs"
    run = studio.RUNS / "example"
    (run / "polished").mkdir(parents=True)
    (run / "run.json").write_text('{"source":{"title":"Test source"}}', encoding="utf8")
    media = run / "polished" / "review.mp4"
    media.write_bytes(b"0123456789")
    (run / ".env").write_text("not served", encoding="utf8")
    assert studio.run_path("..") is None
    assert studio.file_path(run, [".env"]) is None
    assert studio.file_path(run, ["polished", "..", ".env"]) is None
    assert studio.snapshot("example")["run"]["versions"][0]["bytes"] == 10

    with studio.ThreadingHTTPServer((studio.HOST, 0), studio.Handler) as server:
        studio.PORT = server.server_port
        base = f"http://{studio.HOST}:{studio.PORT}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(Request(base + "/run/example/polished/review.mp4", headers={"Range": "bytes=2-5"})) as res:
                assert res.status == 206 and res.read() == b"2345"
            for req, status in [(Request(base + "/run/example/.env"), 404),
                                (Request(base + "/", headers={"Host": "foreign.example"}), 403)]:
                try:
                    urlopen(req)
                    raise AssertionError("Request unexpectedly allowed")
                except HTTPError as e:
                    assert e.code == status

            def test_render():
                with events.job(studio.ROOT, "test", "example"):
                    events.stage("Rendering", "Short measured FFmpeg verification")
                    events.render_ffmpeg(["ffmpeg", "-v", "error", "-re", "-f", "lavfi",
                        "-i", "color=s=80x80:r=20", "-t", "2.5", "-f", "null", "-"], temp, 2.5)
            worker = threading.Thread(target=test_render)
            worker.start()
            observed = False
            while worker.is_alive():
                with urlopen(base + "/api/studio/example") as res:
                    job = json.load(res)["run"]["job"]
                if job and job["progress"] and 0 < job["progress"]["current"] < 2.5:
                    observed = True
                time.sleep(0.15)
            worker.join()
            assert observed
            assert studio.jobs()[0]["state"] == "complete"
            assert studio.jobs()[0]["progress"]["current"] == 2.5
        finally:
            server.shutdown()
            thread.join()
print("studio checks passed")

# campaign stages follow the files that exist, and stay separate from campaign/platform status
with tempfile.TemporaryDirectory() as temp:
    studio.ROOT = Path(temp)
    studio.CAMPAIGNS = studio.ROOT / "campaigns"
    cfgd = studio.CAMPAIGNS / "demo" / "config"
    cfgd.mkdir(parents=True)
    (cfgd / "campaign.json").write_text(json.dumps({"id": "demo", "output_dir": "Approved Clips/Demo", "review_dir": "Review Clips/Demo"}), encoding="utf8")
    (studio.ROOT / "Review Clips" / "Demo").mkdir(parents=True)
    (studio.ROOT / "Review Clips" / "Demo" / "01.mp4").write_bytes(b"x")
    k = {"review_file": "Review Clips/Demo/01.mp4", "approved_file": "Approved Clips/Demo/01.mp4", "publish": {}, "submission": "NOT SUBMITTED"}
    assert studio.clip_stage(k, "demo") == "Review"  # approved copy missing on disk -> still Review
    assert studio.campaign_file("demo", "Review Clips/Demo/01.mp4")
    assert not studio.campaign_file("demo", "Review Clips/Other/01.mp4")
    assert studio.clip_stage({**k, "publish": {"TikTok": "PUBLISHED"}}, "demo") == "Published"
    assert studio.clip_stage({**k, "submission": "REJECTED"}, "demo") == "Campaign decision"
print("campaign stage checks passed")

# run folders: campaign workspace copy wins over the preserved legacy original; names stay plain
from shorts_generator.local import runs as run_roots
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    for base in (root / "work" / "runs" / "r1", root / "campaigns" / "kai" / "work" / "runs" / "r1", root / "work" / "runs" / "r2"):
        base.mkdir(parents=True)
        (base / "run.json").write_text("{}", encoding="utf8")
    assert run_roots.locate(root, "r1") == root / "campaigns" / "kai" / "work" / "runs" / "r1"
    assert run_roots.locate(root, "r2") == root / "work" / "runs" / "r2"
    assert run_roots.locate(root, "..") is None and run_roots.locate(root, "r1/../r2") is None
    assert run_roots.names(root) == ["r1", "r2"]
    assert run_roots.project_root(root / "campaigns" / "kai" / "work" / "runs" / "r1") == root.resolve()
    assert run_roots.project_root(root / "work" / "runs" / "r2") == root.resolve()
print("run location checks passed")

# replacement decisions: recorded on V2 clips only, whitelisted values, nothing else touched
with tempfile.TemporaryDirectory() as temp:
    studio.ROOT = Path(temp)
    studio.CAMPAIGNS = studio.ROOT / "campaigns"
    cfgd = studio.CAMPAIGNS / "demo" / "config"
    cfgd.mkdir(parents=True)
    (cfgd / "campaign.json").write_text(json.dumps({"id": "demo", "output_dir": "Approved Clips/Demo"}), encoding="utf8")
    (cfgd / "clips.json").write_text(json.dumps([{"id": "a"}, {"id": "a2", "v2_of": "a"}]), encoding="utf8")
    assert studio.replacement_decision({"campaign": "demo", "clip": "a", "decision": "prefer_v2"}) == "not a V2 clip"
    assert studio.replacement_decision({"campaign": "demo", "clip": "a2", "decision": "delete"}) == "invalid request"
    assert studio.replacement_decision({"campaign": "../x", "clip": "a2", "decision": "prefer_v2"}) == "invalid request"
    assert studio.replacement_decision({"campaign": "demo", "clip": "a2", "decision": "keep_original"}) is None
    saved = json.loads((cfgd / "clips.json").read_text(encoding="utf8"))
    assert saved[1]["replacement_decision"]["decision"] == "keep_original" and "replacement_decision" not in saved[0]
print("replacement decision checks passed")

# open-folder only accepts the two configured creator folders
with tempfile.TemporaryDirectory() as temp:
    studio.ROOT = Path(temp)
    studio.CAMPAIGNS = studio.ROOT / "campaigns"
    cfgd = studio.CAMPAIGNS / "demo" / "config"
    cfgd.mkdir(parents=True)
    (cfgd / "campaign.json").write_text(json.dumps({"id": "demo", "output_dir": "../outside", "review_dir": "Review Clips/Demo"}), encoding="utf8")
    assert studio.open_folder({"campaign": "demo", "which": "elsewhere"}) == "invalid request"
    assert studio.open_folder({"campaign": "demo", "which": "approved"}) == "folder missing"  # escapes ROOT
    assert studio.open_folder({"campaign": "demo", "which": "review"}) == "folder missing"    # not created
print("open-folder checks passed")
