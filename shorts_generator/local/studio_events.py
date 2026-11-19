"""Low-overhead, best-effort progress for the local studio."""
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

_active = None
_lock = threading.Lock()


def _update(**values):
    if _active is None:
        return
    path, data = _active
    with _lock:
        data.update(values, heartbeat=time.time())
        try:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            pass  # Studio telemetry must never interrupt the editing job.


@contextmanager
def job(root, operation, run=None):
    global _active
    if _active is not None:
        yield
        return
    folder = Path(root) / "work" / "studio-jobs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield
        return
    _active = (folder / f"{uuid.uuid4().hex}.json", {
        "operation": operation, "run": run, "started": time.time(), "state": "running",
        "events": [], "stage": None, "progress": None,
    })
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(4):
            _update()
    thread = threading.Thread(target=heartbeat, daemon=True)
    _update()
    thread.start()
    try:
        yield
    except BaseException as exc:
        _update(state="failed", error=type(exc).__name__, ended=time.time())
        raise
    else:
        _update(state="complete", ended=time.time())
    finally:
        stop.set()
        thread.join()
        _active = None


def attach(run):
    _update(run=run)


def stage(name, detail=None, output=None):
    if _active is None:
        return
    events = _active[1]["events"]
    _update(stage=name, progress=None, output=output, events=[*events, {"stage": name, "detail": detail, "at": time.time()}])


def progress(unit, current, total=None):
    _update(progress={"unit": unit, "current": current, "total": total})


def download_hook(info):
    total = info.get("total_bytes")
    downloaded = info.get("downloaded_bytes")
    if downloaded is not None and _active is not None:
        now = time.monotonic()
        if now - _active[1].get("download_observed", 0) >= 1 or info.get("status") == "finished":
            _update(download_observed=now, progress={"unit": "bytes", "current": downloaded, "total": total})


def render_ffmpeg(command, cwd, duration):
    if _active is None:
        subprocess.run(command, cwd=cwd, check=True)
        return
    published = 0.0
    with subprocess.Popen([command[0], "-progress", "pipe:1", "-nostats", *[a for a in command[1:] if a != "-stats"]],
                          cwd=cwd, stdout=subprocess.PIPE, text=True) as process:
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.lstrip("-").isdigit():
                now = time.monotonic()
                if now - published >= 1:
                    progress("seconds encoded", max(0, int(value) / 1e6), duration)
                    published = now
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, command)
    progress("seconds encoded", duration, duration)

def save_render(root, output, timeline):
    """Save timestamped render evidence and one small milestone frame."""
    run = Path(root).resolve()
    path = Path(output).resolve()
    if not (run / "run.json").is_file() or path.parent != run / "polished":
        return
    try:
        (run / "plans").mkdir(exist_ok=True)
        (run / "plans" / (path.stem + ".studio.json")).write_text(
            json.dumps(timeline, ensure_ascii=False), encoding="utf8")
        frames = run / "director"
        frames.mkdir(exist_ok=True)
        thumb = frames / (path.stem + "-milestone.jpg")
        if not thumb.exists():
            subprocess.run(["ffmpeg", "-n", "-v", "error", "-ss", str(min(1, timeline["duration_s"] / 2)),
                            "-i", str(path), "-vf", "scale=216:-2", "-frames:v", "1", str(thumb)],
                           check=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass

def track_render(function):
    @wraps(function)
    def wrapped(plan, out_path, overlay=None):
        if _active is not None:
            return function(plan, out_path, overlay)
        run = Path(os.environ.get("LOCAL_OUTPUT_DIR", "")).resolve()
        if run.parent.name != "runs" or run.parent.parent.name != "work" or not (run / "run.json").is_file():
            return function(plan, out_path, overlay)
        with job(run.parents[2], "render", run.name):
            stage("Timeline assembly", "Using the supplied edit plan")
            return function(plan, out_path, overlay)
    return wrapped
