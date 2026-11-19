"""Fail-closed campaign preflight for upload-folder delivery."""
import json
from math import isfinite
from datetime import date
from pathlib import Path

CHECKS = (
    "campaign", "rules", "source_and_rights", "branding", "paid_disclosure",
    "disclosure_timing", "platform_disclosures", "facecam_and_voice", "captions",
    "truthful_edit", "technical", "sound_licensing", "allowed_footage", "playback",
)
MANUAL = "NEEDS MANUAL REVIEW"
RULES = Path(__file__).parent / "campaigns"


def disclosure_lead_in(audit, required):
    if required is False and audit.get("paid_disclosure_required") is False:
        return "PASS", ""
    lead = audit.get("disclosure_lead_in", {})
    intro, highlight = lead.get("intro"), lead.get("highlight")
    for intervals in (intro, highlight):
        if not isinstance(intervals, list) or not intervals:
            return MANUAL, "Record the disclosure and highlight source intervals."
        for pair in intervals:
            if (not isinstance(pair, list) or len(pair) != 2
                    or any(type(t) not in (float, int) or not isfinite(t) for t in pair)
                    or not 0 <= pair[0] < pair[1]):
                return MANUAL, "Invalid disclosure or highlight source interval."
    if any(max(a, c) < min(b, d) for a, b in intro for c, d in highlight):
        return "FAIL", "Disclosure lead-in reuses highlight footage."
    if any(b > c for (_, b), (c, _) in zip(intro, intro[1:])):
        return "FAIL", "Disclosure lead-in repeats or rewinds source footage."
    if lead.get("rendered_transition_checked") is not True:
        return MANUAL, "Inspect the rendered transition for duplicate frames."
    return "PASS", ""


def reaction_montage_source_map(entries):
    """Validate screen order and nonrepeated Twitch ranges for a flashback edit."""
    if not isinstance(entries, list) or not entries:
        return MANUAL, "Record a source map for the reaction montage."
    sources = []
    end = 0.0
    for cut in entries:
        if not isinstance(cut, dict):
            return "FAIL", "Invalid montage cut."
        for key in ("source", "output"):
            pair = cut.get(key)
            if (not isinstance(pair, list) or len(pair) != 2
                    or any(type(t) not in (int, float) or not isfinite(t) for t in pair)
                    or not 0 <= pair[0] < pair[1]):
                return "FAIL", "Invalid montage range."
        if abs(cut["output"][0] - end) > 0.002:
            return "FAIL", "Montage output ranges have a gap or overlap."
        end = cut["output"][1]
        sources.append(cut["source"])
    sources.sort()
    if any(a[1] > b[0] for a, b in zip(sources, sources[1:])):
        return "FAIL", "Reaction montage repeats source footage or dialogue."
    return "PASS", ""


def evaluate(folder, campaign, source, platforms, sha):
    if Path(campaign).name != campaign:
        return MANUAL, "Invalid campaign identifier."
    rules_path = RULES / f"{campaign}.rules.json"
    audit_path = folder / "compliance" / f"{source.stem}.json"
    if not rules_path.is_file() or not audit_path.is_file():
        return MANUAL, "Campaign rules or exact-file checklist missing."
    rules = json.loads(rules_path.read_text(encoding="utf-8-sig"))
    audit = json.loads(audit_path.read_text(encoding="utf-8-sig"))
    if (audit.get("sha256", "").lower() != sha.lower() or audit.get("campaign") != campaign
            or audit.get("rules_version") != rules.get("version")
            or not set(platforms) <= set(audit.get("platforms", []))):
        return MANUAL, "Checklist does not cover this exact export, rules version, or platforms."
    required = rules.get("paid_disclosure_required")
    status, reason = disclosure_lead_in(audit, required)
    if status == "FAIL":
        return status, reason

    if audit.get("edit_style") == "reaction_montage":
        map_status, map_reason = reaction_montage_source_map(audit.get("edited_source_map"))
        if map_status == "FAIL":
            return map_status, map_reason
        if map_status != "PASS" and status == "PASS":
            status, reason = map_status, map_reason

    checks = audit.get("checks", {})
    failed, unresolved = [], []
    for check in CHECKS:
        result = checks.get(check, {})
        if result.get("status") == "FAIL":
            failed.append(check)
        elif result.get("status") != "PASS" or not result.get("evidence"):
            unresolved.append(check)
    if failed:
        return "FAIL", ", ".join(failed)
    if (rules.get("status") != "PASS" or rules.get("verified_on") != date.today().isoformat()
            or not rules.get("sources")):
        return MANUAL, "Confirm complete campaign rules and current official terms today."
    if status != "PASS":
        unresolved.append(reason)
    if unresolved:
        return MANUAL, ", ".join(unresolved)
    return "PASS", str(audit_path)
