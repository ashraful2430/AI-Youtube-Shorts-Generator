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
