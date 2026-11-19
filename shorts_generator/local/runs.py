"""Where run folders may live: campaigns/<id>/work/runs/<run> (preferred) or the legacy work/runs/<run>.
Trust boundary: a run id is one plain folder name directly under one of these roots, with a run.json."""
from pathlib import Path
import re


def roots(root):
    root = Path(root)
    return [*sorted((root / "campaigns").glob("*/work/runs")), root / "work" / "runs"]


def locate(root, run_id):
    """The run folder for run_id, campaign workspace first (a migrated copy wins over the preserved original)."""
    if not re.fullmatch(r"[\w.-]+", run_id or "") or run_id in {".", ".."}:
        return None
    for r in roots(root):
        p = r / run_id
        if p.resolve() == r.resolve() / run_id and (p / "run.json").is_file():
            return p
    return None


def names(root):
    return sorted({p.name for r in roots(root) if r.is_dir() for p in r.iterdir() if locate(root, p.name) == p})


def project_root(run):
    """Project root for a run folder in either layout, or None if the folder is not a run."""
    run = Path(run).resolve()
    if run.parent.name != "runs" or run.parent.parent.name != "work" or not (run / "run.json").is_file():
        return None
    up = run.parents[2]
    return up.parent.parent if up.parent.name == "campaigns" else up
