"""Approval, publication records, and manifest-backed cleanup."""
from contextlib import closing
from compliance import evaluate
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date
from urllib.parse import urlsplit

PLATFORMS = ("youtube", "instagram", "tiktok")
UPLOAD = Path.home() / "Videos/Captures/Upload videos/Clips folder"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inside(root, path):
    path = Path(path).absolute()
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError(f"Unsafe file path: {path}")
    return path


def run_folder(root, path):
    from shorts_generator.local.runs import roots
    path = Path(path).absolute()
    for base in roots(root):
        if path.parent == base.absolute():
            return inside(base.absolute(), path)
    raise ValueError(f"Unsafe file path: {path}")


def register(parser, root, run_dir, read):
    root = Path(root).resolve()

    def execute(a):
        work = root / "work"
        work.mkdir(exist_ok=True)
        with closing(sqlite3.connect(work / "publishing.sqlite3")) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS clips (run TEXT, candidate INTEGER, record TEXT, PRIMARY KEY(run,candidate))")
            db.execute("CREATE TABLE IF NOT EXISTS previews (token TEXT PRIMARY KEY, files TEXT)")
            db.execute("BEGIN IMMEDIATE")

            def save(record):
                db.execute("INSERT OR REPLACE INTO clips VALUES (?,?,?)", (a.run, a.candidate, json.dumps(record)))

            def records():
                return {(run, cid): json.loads(payload) for run,cid,payload in db.execute("SELECT * FROM clips")}

            def complete(record):
                return (record["uploads"].keys() >= set(record["platforms"])
                        and (record["campaign_submitted"] or not record["submission_required"]))

            def pending(record):
                return not complete(record)

            def cleanup_files():
                eligible, retained = [], []

                def consider(path, base, expected=None):
                    path = inside(base, path)
                    if path.is_file():
                        sha = digest(path)
                        if expected is not None and sha != expected:
                            retained.append(f"{path} (content changed)")
                        else:
                            eligible.append([str(path), path.stat().st_size, sha])

                for (run, cid), record in records().items():
                    path = UPLOAD / record["name"]
                    if pending(record):
                        retained.append(f"{path} (upload or campaign submission pending)")
                    else:
                        consider(path, UPLOAD, record["sha256"])

                if a.run and a.rejected_only:
                    folder = run_folder(root, run_dir(a.run))
                    run = read(folder / "run.json")
                    for c in read(folder / "candidates.json", []):
                        if run.get("finished") and c["status"] == "rejected":
                            for key, ext in (("raw_clip", ".mp4"), ("thumb", ".jpg")):
                                path = c.get(key)
                                base = folder / ("raw" if key == "raw_clip" else "thumbs")
                                if path and Path(path).suffix.lower() == ext:
                                    consider(path, base)
                    for f in a.file:
                        if Path(f).name != f or not f.lower().endswith(".mp4"):
                            raise ValueError("Use an exact polished MP4 filename")
                        path = inside(folder / "polished", folder / "polished" / f)
                        if any(r.get("approved_version", r["name"].split("__", 1)[-1]) == f for r in records().values()):
                            raise ValueError(f"Approved draft is protected: {f}")
                        consider(path, folder)
                return sorted({tuple(f) for f in eligible}), retained

            if a.cmd in ("clean", "ready"):
                if a.cmd == "ready":
                    for (run, cid), r in records().items():
                        remaining = sorted(set(r["platforms"]) - r["uploads"].keys())
                        if pending(r) and (UPLOAD / r["name"]).is_file():
                            print(f"{run} #{cid}: {UPLOAD / r['name']} | remaining: {', '.join(remaining) or 'none'}"
                                  f" | campaign pending: {r['submission_required'] and not r['campaign_submitted']}")
                    return
                if (a.rejected_only or a.file) and (not a.run or not a.rejected_only):
                    raise ValueError("Use --run and --rejected-only with explicitly rejected --file drafts")
                files, retained = cleanup_files()
                if a.confirm:
                    row = db.execute("SELECT files FROM previews WHERE token=?", (a.confirm,)).fetchone()
                    if not row or [list(f) for f in files] != json.loads(row[0]):
                        raise ValueError("Cleanup scope changed. Run a fresh preview.")
                    for name, size, sha in files:
                        path = inside(UPLOAD if Path(name).is_relative_to(UPLOAD) else root, name)
                        if path.stat().st_size != size or digest(path) != sha:
                            raise ValueError(f"File changed: {path}")
                    for name, size, _ in files:
                        path = inside(UPLOAD if Path(name).is_relative_to(UPLOAD) else root, name)
                        path.unlink()
                        if path.exists():
                            raise OSError(f"Could not verify removal: {path}")
                        print(f"Removed {name} ({size:,} bytes)", flush=True)
                    db.execute("DELETE FROM previews WHERE token=?", (a.confirm,))
                    print(f"Recovered {sum(f[1] for f in files):,} bytes")
                    return
                for name, size, _ in files:
                    print(f"Would remove {name} ({size:,} bytes)")
                for item in retained:
                    print(f"Keep {item}")
                print("Keep source footage, active candidates, project files, assets, caches and untracked files.")
                print(f"Estimated savings: {sum(f[1] for f in files):,} bytes")
                if files:
                    import secrets
                    token = secrets.token_hex(8)
                    db.execute("INSERT INTO previews VALUES (?,?)", (token, json.dumps(files)))
                    print(f"To apply this preview, repeat the same options with --confirm {token}")
                return

            folder = run_folder(root, run_dir(a.run))
            run = read(folder / "run.json")
            candidates = read(folder / "candidates.json", [])
            if a.cmd == "review":
                print(f"Gallery: {folder / 'review.html'}")
                for path in sorted((folder / "polished").glob("*.mp4")):
                    m = re.match(r"c(\d+)_", path.name)
                    c = next((c for c in candidates if m and c["id"] == int(m[1])), {})
                    props = probe(path)
                    status = "incomplete" if "incomplete" in path.name.lower() else "review required"
                    print(f"#{c.get('id', '?')} {c.get('title', path.stem)} | "
                          f"{props.get('width','?')}x{props.get('height','?')} {props.get('duration','?')}s | {status} | {path}")
                return

            if a.cmd == "preflight":
                if (Path(a.file).name != a.file or not a.file.startswith(f"c{a.candidate:02d}_")
                        or not a.file.lower().endswith(".mp4")):
                    raise ValueError("Use the exact MP4 filename for this candidate")
                src = inside(folder, folder / "polished" / a.file)
                status, reason = evaluate(folder, run["campaign"], src, a.platform, digest(src))
                print(f"{status}: {reason}")
                if status != "PASS":
                    raise SystemExit(1)
                return

            current = records().get((a.run, a.candidate))
            if a.cmd == "approve":
                if not any(c["id"] == a.candidate and c["status"] == "candidate" for c in candidates):
                    raise ValueError("Candidate must be retained for review")
                if (Path(a.file).name != a.file or not a.file.startswith(f"c{a.candidate:02d}_")
                        or not a.file.lower().endswith(".mp4") or "incomplete" in a.file.lower()):
                    raise ValueError("Use the exact finished MP4 filename for this candidate")
                src = inside(folder, folder / "polished" / a.file)
                if not src.is_file():
                    raise ValueError(f"Draft is missing: {src}")
                sha = digest(src)
                status, reason = evaluate(folder, run["campaign"], src, a.platform, sha)
                if status != "PASS":
                    raise ValueError(f"Compliance {status}: {reason}")
                name = a.upload_name or f"{a.run}__{src.name}"
                if a.upload_name and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*\.mp4", name, re.IGNORECASE):
                    raise ValueError("Use a simple MP4 filename for --upload-name")
                if current and a.upload_name and current["name"] != name:
                    raise ValueError("This approval already uses a different upload filename")
                if current and (current.get("approved_version", current["name"].split("__", 1)[-1]) != src.name
                                or current["sha256"] != sha):
                    raise ValueError("A different version is already approved for this candidate")
                if current and not pending(current):
                    print("This approval has already completed its publishing workflow.")
                    return
                props = probe(src)
                if not props or (props["width"], props["height"], props.get("display_aspect_ratio")) != (1080, 1920, "9:16"):
                    raise ValueError("Approval requires a 1080×1920, 9:16 MP4")
                subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(src),
                                "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "NUL"], check=True)
                UPLOAD.mkdir(parents=True, exist_ok=True)
                dst = inside(UPLOAD, UPLOAD / (current["name"] if current else name))
                if not dst.exists():
                    with tempfile.NamedTemporaryFile(dir=UPLOAD.parent, prefix=".clip-stage-", delete=False) as stream:
                        staged = Path(stream.name)
                        with src.open("rb") as source:
                            shutil.copyfileobj(source, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if staged.stat().st_size != src.stat().st_size or digest(staged) != sha:
                        raise ValueError(f"Copy verification failed: {staged}")
                    staged.rename(dst)  # Windows rename fails if the destination already exists.
                if digest(dst) != sha:
                    raise ValueError(f"Upload name conflict: {dst}")
                if not current:
                    save(dict(name=name, approved_version=src.name, sha256=sha, campaign=run["campaign"], source=run["source"],
                              platforms=sorted(set(a.platform)), uploads={}, campaign_submitted=False,
                              submission_required=not a.no_campaign_submission, approved=date.today().isoformat()))
                print(f"Approved copy: {dst}")
                return

            if not current:
                raise ValueError("Approve an exact version before recording uploads")
            if a.cmd == "uploaded":
                url = urlsplit(a.url)
                if a.platform not in current["platforms"] or url.scheme != "https" or not url.hostname:
                    raise ValueError("Use an intended platform and a published HTTPS URL")
                current["uploads"][a.platform] = {"url": a.url, "date": a.date.isoformat()}
            else:
                current["campaign_submitted"] = True
            save(current)
            print(f"Updated {current['name']}")

    def dispatch(a):
        try:
            execute(a)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as exc:
            raise SystemExit(str(exc)) from None

    for name, help_text in (
        ("preflight", "check an exact draft against current campaign rules"),
        ("review", "list the gallery and measured properties of polished drafts"),
        ("approve", "copy one exact approved version to the Clips folder"),
        ("uploaded", "record a manually published video"),
        ("submitted", "record completed campaign submission"),
        ("ready", "list approved copies with publishing work remaining"),
        ("clean", "preview cleanup or apply an exact reviewed preview"),
    ):
        p = parser.add_parser(name, help=help_text)
        p.set_defaults(fn=dispatch)
        if name not in ("clean", "ready"):
            p.add_argument("--run", required=True)
        if name in ("preflight", "approve", "uploaded", "submitted"):
            p.add_argument("--candidate", required=True, type=int)
        if name in ("preflight", "approve"):
            p.add_argument("--file", required=True)
            p.add_argument("--platform", choices=PLATFORMS, action="append", required=True)
        if name == "approve":
            p.add_argument("--upload-name", help="exact filename for the approved copy")
            p.add_argument("--no-campaign-submission", action="store_true")
        if name == "uploaded":
            p.add_argument("--platform", choices=PLATFORMS, required=True)
            p.add_argument("--url", required=True)
            p.add_argument("--date", type=date.fromisoformat, default=date.today())
        if name == "clean":
            p.add_argument("--run")
            p.add_argument("--rejected-only", action="store_true")
            p.add_argument("--file", action="append", default=[], help="exact draft explicitly rejected for cleanup")
            p.add_argument("--confirm", help="token from an unchanged cleanup preview")


def probe(path):
    raw = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                   "-show_entries", "stream=width,height,display_aspect_ratio,duration",
                                   "-of", "json", str(path)])
    return next(iter(json.loads(raw)["streams"]), {})
