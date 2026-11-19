"""Read-only Vice City Intel studio. Run with python studio.py --open."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import time
import threading
from urllib.parse import quote, unquote, urlsplit
import webbrowser

from shorts_generator.local import runs

ROOT = Path(os.environ.get("CLIP_ROOT", Path(__file__).resolve().parent.parent))
RUNS = ROOT / "work" / "runs"
UI = Path(__file__).resolve().parent / "studio.html"
HOST, PORT = "127.0.0.1", 8765
MEDIA = {"raw", "director", "polished", "thumbs", "temp", "hq", "input"}
MEDIA_EXT = {".mp4", ".webm", ".jpg", ".jpeg", ".png", ".wav", ".mp3"}
REPORTS = {"director", "compliance", "analysis"}
CAMPAIGNS = ROOT / "campaigns"  # campaign workspaces: campaigns/<id>/config/{campaign,clips}.json


def campaign_cfg(cid):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cid or ""):
        return None
    return read(CAMPAIGNS / cid / "config" / "campaign.json")


def campaign_file(cid, rel):
    """A media file belonging to one campaign: its workspace, its approved-clips folder or its legacy runs."""
    cfg = campaign_cfg(cid)
    if not cfg or not rel or ".." in rel.replace("\\", "/").split("/"):
        return None
    p = (ROOT / rel).resolve()
    roots = [CAMPAIGNS / cid, ROOT / cfg.get("output_dir", "-"), ROOT / cfg.get("review_dir", "-")] + [ROOT / r for r in cfg.get("legacy_runs", [])]
    ok = p.suffix.lower() in MEDIA_EXT and p.is_file() and any(p.is_relative_to(r.resolve()) for r in roots)
    return p if ok else None


def clip_stage(k, cid):
    """Pipeline column. Internal editorial stages (candidate/review/approved) come from the files that exist;
    platform and Content Rewards stages come from the registry, so they never mix."""
    sub = k.get("submission", "")
    if sub.startswith(("ACCEPTED", "REJECTED")):
        return "Campaign decision"
    if sub.startswith("SUBMITTED"):
        return "Submitted"
    if any(str(v).startswith("PUBLISHED") for v in (k.get("publish") or {}).values()):
        return "Published"
    if k.get("approved_file") and campaign_file(cid, k["approved_file"]):
        return "Approved (internal)"
    if k.get("review_file") and campaign_file(cid, k["review_file"]):
        return "Review"
    return "New candidate"


DECISIONS = {"keep_original", "prefer_v2", "reject_v2"}


def open_folder(body):
    """Open a campaign's Review or Approved folder in Explorer. Only the two folders named in that campaign's config."""
    cfg = campaign_cfg(body.get("campaign"))
    key = {"review": "review_dir", "approved": "output_dir"}.get(body.get("which"))
    if not cfg or not key or not cfg.get(key):
        return "invalid request"
    folder = (ROOT / cfg[key]).resolve()
    if not folder.is_relative_to(ROOT.resolve()) or not folder.is_dir():
        return "folder missing"
    os.startfile(folder)  # Windows-only local Studio
    return None


def replacement_decision(body):
    """Owner's choice between an original clip and its V2. Recorded only: no file is moved, replaced or deleted,
    and nothing is published or submitted. Locked (published/submitted) originals stay untouched either way."""
    cid, vid, decision = body.get("campaign"), body.get("clip"), body.get("decision")
    if not campaign_cfg(cid) or decision not in DECISIONS:
        return "invalid request"
    path = CAMPAIGNS / cid / "config" / "clips.json"
    clips = read(path, []) or []
    v2 = next((k for k in clips if k.get("id") == vid and k.get("v2_of")), None)
    if v2 is None:
        return "not a V2 clip"
    v2["replacement_decision"] = {"decision": decision, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": "owner via Studio",
                                  "note": "recorded only - promotion still needs campaign.py place + checks"}
    path.write_text(json.dumps(clips, indent=2, ensure_ascii=False), encoding="utf-8")
    return None


def campaigns():
    out = []
    for c in sorted(CAMPAIGNS.glob("*/config/campaign.json")):
        cfg = read(c, {}) or {}
        cid = cfg.get("id")
        if cid != c.parent.parent.name:
            continue
        clips = read(c.parent / "clips.json", []) or []
        for k in clips:
            qa = read(CAMPAIGNS / cid / "logs" / "qa" / f"{k.get('id')}.json", {}) or {}
            k["qa"] = qa.get("auto_result", "NOT RUN")
            # where the file actually is now (a manual move shows up on the next poll), preferring the creator folders
            here = next((k[f] for f in ("approved_file", "review_file", "preview", "export") if k.get(f) and campaign_file(cid, k[f])), None)
            k["url"] = f"/campaign/{cid}/" + quote(here) if here else None
            k["path"] = str(ROOT / here) if here else None
            k["stage"] = clip_stage(k, cid)
            k["ready"] = (k["stage"] == "Approved (internal)" and not k.get("blockers") and k.get("qa") == "PASS"
                          and (k.get("score") or 0) >= cfg.get("approval_threshold", 8))
        for k in clips:  # V2 <-> original pairing for side-by-side review
            if k.get("v2_of"):
                o = next((x for x in clips if x.get("id") == k["v2_of"]), {})
                k["original"] = {"n": o.get("n"), "title": o.get("title"), "score": o.get("score"), "url": o.get("url"), "stage": o.get("stage")}
                o.setdefault("alternatives", []).append({"id": k["id"], "title": k.get("title"), "score": k.get("score"), "url": k.get("url"),
                                                         "decision": (k.get("replacement_decision") or {}).get("decision")})
        for k in clips:  # version history: one card per logical clip, older renders/backups listed inside it
            hist = k.get("versions") or []
            k["version"] = next((h["version"] for h in hist if h.get("active")), 1)
            k["history"] = [{"version": h["version"], "score": h.get("score"), "note": h.get("note", ""), "active": h.get("active"),
                             "urls": [f"/campaign/{cid}/" + quote(b) for b in (h.get("backups") or []) + [h.get("export")] if b and campaign_file(cid, b)][:1]}
                            for h in hist]
            folder = ROOT / cfg.get("output_dir" if k.get("approved_file") else "review_dir", "-")
            k["kit"] = all((folder / f).is_file() for f in ("UPLOAD-INFO.txt", "YOUTUBE-KIT.txt", "TIKTOK-KIT.txt", "INSTAGRAM-KIT.txt"))
            k["missing"] = list(k.get("blockers") or []) + ([f"score {k.get('score')} < {cfg.get('approval_threshold', 8)}"]
                                                         if (k.get("score") or 0) < cfg.get("approval_threshold", 8) else []) +                            ([f"automated QA {k['qa']}"] if k["qa"] != "PASS" else [])
        clips = [k for k in clips if not k.get("v2_of")]  # alternatives live inside their parent card
        counts = {}
        for k in clips:
            counts[k.get("status", "DRAFT")] = counts.get(k.get("status", "DRAFT"), 0) + 1
        r, cap = cfg.get("render", {}), cfg.get("caption", {})
        out.append({"id": cid, "name": cfg.get("name"), "streamer": cfg.get("streamer"), "status": cfg.get("status"),
                    "platforms": cfg.get("platforms", []), "output_dir": str(ROOT / cfg.get("output_dir", "")),
                    "review_dir": str(ROOT / cfg.get("review_dir", "")), "threshold": cfg.get("approval_threshold", 8),
                    "verified": cfg.get("rules_verified", []),
                    "branding": {"label": r.get("disclosure_label"), "spoken": r.get("narration_text"),
                                 "overlay": Path(r["overlay"]["file"]).name if r.get("overlay") else None,
                                 "first_line": cap.get("first_line"), "hashtags": cap.get("required_hashtags", []),
                                 "phrases": cap.get("required_phrases", []), "no_urls": cap.get("forbid_urls", False)},
                    "warnings": cfg.get("rules_manual", []), "clips": clips, "counts": counts,
                    "runs": [Path(r).name for r in cfg.get("legacy_runs", [])]})
    return out


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def run_path(name):
    if not re.fullmatch(r"[\w.-]+", name) or name in {".", ".."}:
        return None
    return runs.locate(ROOT, name)


def file_path(run, parts):
    if not parts or any(x in {"", ".", ".."} or "\\" in x or ":" in x for x in parts):
        return None
    p = run.joinpath(*parts)
    if not p.resolve().is_relative_to(run.resolve()):
        return None
    if parts in (["review.html"], ["director", "meme-director.html"]) or (
        len(parts) == 2 and (
            (parts[0] in MEDIA and p.suffix.lower() in MEDIA_EXT and ".part" not in p.name)
            or (parts[0] in REPORTS and (p.suffix == ".md" or "qa" in p.stem or p.stem.endswith(("-review", "-audit"))))
        )
    ):
        return p if p.is_file() else None
    return None


def asset(run, path):
    try:
        parts = list(path.relative_to(run).parts)
        if not file_path(run, parts):
            return None
        return {"name": path.name, "url": "/run/" + "/".join(quote(x) for x in [run.name, *parts]),
                "path": str(path), "bytes": path.stat().st_size, "modified": path.stat().st_mtime}
    except (OSError, ValueError):
        return None


def assets(run, folders, suffixes):
    files = []
    for folder in folders:
        for path in (run / folder).glob("*"):
            if path.suffix.lower() in suffixes:
                item = asset(run, path)
                if item and item["bytes"]:
                    files.append(item)
    return sorted(files, key=lambda a: a["modified"], reverse=True)


def jobs():
    entries = []
    for p in sorted((ROOT / "work" / "studio-jobs").glob("*.json"), key=lambda p: p.name):
        a = read(p)
        if not isinstance(a, dict) or not isinstance(a.get("started"), (int, float)):
            continue
        if a.get("state") == "running" and time.time() - a.get("heartbeat", 0) > 16:
            a["state"] = "interrupted"
        entries.append(a)
    return sorted(entries, key=lambda a: a["started"], reverse=True)


def version_data(run, item):
    path = Path(item["path"])
    item["render"] = read(run / "plans" / (path.stem + ".studio.json"), {}) or {}
    audit = read(run / "compliance" / (path.stem + ".json"), {}) or {}
    lead = audit.get("disclosure_lead_in", {})
    item["disclosure"] = lead.get("intro", [])
    item["source_cuts"] = lead.get("highlight", [])
    item["metrics"] = audit.get("measured_export", {})
    item["checks"] = {k: v for k, v in audit.get("checks", {}).items() if isinstance(v, dict)}
    if "owner_delivery" in audit:
        item["delivery"] = audit["owner_delivery"].get("status")
    cid = re.match(r"c(\d+)", path.stem)
    poster = next((a for name in (path.stem + "-milestone.jpg", "solo-action-first-frame-zero.jpg" if "action-first-review" in path.stem else "")
                   if name and (a := asset(run, run / "director" / name))), None)
    if poster:
        item["poster"] = poster["url"]
    if cid:
        i = int(cid.group(1))
        options = sorted((run / "plans").glob(f"c{i:02}*.plan.json"))
        exact = run / "plans" / (path.stem + ".plan.json")
        plan_path = exact if exact.is_file() else (options[0] if len(options) == 1 else None)
        plan = read(plan_path, {}) if plan_path else {}
        item["plan"] = {k: plan[k] for k in ("source", "segments", "hook_text", "caption_size", "caption_y",
                                                "texts", "zooms", "sfx_events") if k in plan}
        item["plan_name"] = plan_path.name if plan_path else None
    return item


def edit_requests(run):
    out = []
    try:
        for line in (run / "edit-requests.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def add_edit_request(body):
    """Owner's note for one existing reel, appended to <run>/edit-requests.jsonl. Returns an error or None."""
    run = run_path(str(body.get("run", "")))
    url, note = str(body.get("file", "")), str(body.get("note", "")).strip()
    if not run or not url.startswith(f"/run/{quote(run.name)}/") or not 0 < len(note) <= 1000:
        return "invalid request"
    parts = [unquote(x) for x in url.split("/")[3:]]
    file = file_path(run, parts)
    if not file or parts[0] not in {"director", "polished"} or file.suffix.lower() not in {".mp4", ".webm"}:
        return "unknown reel"
    entry = {"at": time.time(), "file": file.name, "path": str(file), "note": note, "status": "new"}
    with (run / "edit-requests.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return None


def snapshot(selected):
    camp = campaigns()
    options = []
    for name in runs.names(ROOT):
        folder = run_path(name)
        p = folder
        if folder and not (read(folder / "studio-archive.json", {}) or {}).get("media_removed"):
            meta = read(folder / "run.json", {}) or {}
            owner = next((c["name"] for c in camp if p.name in c["runs"]), None)
            options.append({"id": p.name, "campaign": owner, "title": meta.get("source", {}).get("title") or p.name,
                            "finished": meta.get("finished"), "created": meta.get("created", "")})
    options.sort(key=lambda x: (x["created"], x["id"]), reverse=True)
    recent = jobs()
    running = [j for j in recent if j.get("state") == "running"]
    selectable = {option["id"] for option in options}
    target = selected if selected in selectable else next(
        (j["run"] for j in running if j.get("run") in selectable), None)
    target = target or (options[0]["id"] if options else None)
    run = run_path(target) if target else None
    result = {"runs": options, "active": running, "run": None, "campaigns": camp}
    if not run:
        return result
    meta = read(run / "run.json", {}) or {}
    source = meta.get("source", {})
    cands = read(run / "candidates.json", []) or []
    highlights = read(run / "director" / "shortlist.json", []) or []
    unavailable = {j["output"] for j in recent if j.get("output") and j["state"] in {"failed", "interrupted", "running"}}
    versions = [version_data(run, x) for x in assets(run, ["polished"], {".mp4", ".webm"}) if x["path"] not in unavailable]
    previews = assets(run, ["director"], {".mp4", ".webm"})
    frames = assets(run, ["director", "thumbs", "temp"], {".jpg", ".png"})
    original = assets(run, ["hq", "input", "raw"], {".mp4", ".webm"})
    current = next((j for j in recent if j.get("run") == run.name), None)
    counts = {"chunks": len(list((run / "analysis").glob("chunk_*.json"))),
              "windows": len(list((run / "analysis").glob("window_*.json"))), "candidates": len(cands)}
    result["run"] = {"id": run.name, "folder": str(run), "title": source.get("title", run.name),
                     "channel": source.get("channel"), "source_url": source.get("url") if str(source.get("url")).startswith(("https://www.twitch.tv/", "https://twitch.tv/")) else None,
                     "source_duration": source.get("duration"), "finished": meta.get("finished"),
                     "gallery": asset(run, run / "review.html"), "counts": counts, "job": current,
                     "meme_director": asset(run, run / "director" / "meme-director.html"),
                     "versions": versions, "previews": previews, "frames": frames, "original": original,
                     "shortlist": highlights, "edit_requests": edit_requests(run)[-100:],
                     "candidates": [{k: c.get(k) for k in ("id", "title", "status", "overall", "start_hms", "end_hms")} for c in cands]}
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_file(self, path):
        size = path.stat().st_size
        start, end = 0, size - 1
        value = self.headers.get("Range")
        if value:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
            if not m or not any(m.groups()) or not size:
                self.send_error(416)
                return
            a, b = m.groups()
            if a:
                start, end = int(a), min(end, int(b)) if b else end
            else:
                start = max(0, size - int(b))
            if start > end:
                self.send_error(416)
                return
        self.send_response(206 if value else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if path.suffix == ".html":
            scripts = " 'unsafe-inline'" if path == UI else " 'none'"
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src" + scripts + "; connect-src 'self'; img-src 'self'; media-src 'self'; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
        if value:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(remaining, 128 * 1024))
                if not chunk:
                    return
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self):
        if self.headers.get("Host") != f"{HOST}:{PORT}" or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(403)
            return
        url = urlsplit(self.path)
        path = unquote(url.path)
        try:
            if path == "/":
                self.send_file(UI)
            elif path == "/api/studio" or path.startswith("/api/studio/"):
                data = json.dumps(snapshot(path.removeprefix("/api/studio/") if path != "/api/studio" else None)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            elif path.startswith("/campaign/"):
                _, _, cid, rel = (path.split("/", 3) + [""])[:4]
                file = campaign_file(cid, rel)
                if file:
                    self.send_file(file)
                else:
                    self.send_error(404)
            elif path.startswith("/run/"):
                _, _, name, *parts = path.split("/")
                run = run_path(name)
                file = file_path(run, parts) if run else None
                if file:
                    self.send_file(file)
                else:
                    self.send_error(404)
            else:
                self.send_error(404)
        except (ConnectionError, OSError):
            return

    do_HEAD = do_GET

    def do_POST(self):
        # Only the Studio page itself may write: same host, same-origin fetch, small JSON body.
        if (self.headers.get("Host") != f"{HOST}:{PORT}" or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")
                or self.headers.get("Origin") not in (None, f"http://{HOST}:{PORT}")
                or urlsplit(self.path).path not in ("/api/edit-request", "/api/replacement-decision", "/api/open-folder")
                or not self.headers.get("Content-Type", "").startswith("application/json")):
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 4096:
            self.send_error(413)
            return
        try:
            body = json.loads(self.rfile.read(length))
            route = urlsplit(self.path).path
            error = (replacement_decision(body) if route == "/api/replacement-decision"
                     else open_folder(body) if route == "/api/open-folder" else add_edit_request(body))
        except (ValueError, OSError):
            error = "invalid request"
        self.send_response(400 if error else 201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": error} if error else {"ok": True}).encode())


def start_background():
    """Serve during a CLI job when no standalone studio is already listening."""
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    url = f"http://{HOST}:{PORT}/"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        from urllib.request import urlopen
        with urlopen(url + "api/studio", timeout=2) as response:
            if "runs" not in json.load(response):
                raise RuntimeError("Studio port is in use by another application")
        if args.open:
            webbrowser.open(url)
        return
    print(url, flush=True)
    if args.open:
        webbrowser.open(url)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
