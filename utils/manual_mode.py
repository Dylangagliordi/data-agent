"""Spec 3 (Final) — "Manual Mode": pin a Transformation Options candidate's
decision to a supplied reference instead of asking live, with progressive
disclosure when the reference is incomplete or absent (Part 5).

Structural framing (Part 1): manual mode is NOT a new candidate kind and NOT
a new menu — it's a new SOURCE for the chosen_option_id that would normally
come from a live human answer via present_transformation_options. A
candidate resolved via manual mode still goes through every other part of
the normal flow: still logged to _transformation_decisions with full
context, still narratable by Spec 2 exactly like a live decision (see
utils/narrative.py's manual-mode-aware rendering), still visible in the
checklist (Part 4, build_step_checklist below).

Process-scoped registry (Part 1): apply_manual_mode/get_manual_mode_override
use a module-level dict, not a per-question state field. This is a
deliberate, narrow exception to the project's usual "no module-level mutable
global" discipline (see agents/router.py's own removed LAST_SQL_ANALYST_STATE
global, architecture review point #27) — manual mode is an operator/session
setup action taken BEFORE running questions in this process, analogous to a
runtime config flag, not per-request state that concurrent callers could
race on. This project's actual execution model is one question per CLI
process invocation, so the concurrency risk that made a global unsafe for
sql_analyst_trace does not apply here. apply_manual_mode REPLACES the active
set on every call (never merges) — a fresh registration always starts clean.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_REFERENCE_MAPPINGS_DIR = Path(__file__).resolve().parent / "reference_mappings"


# ---------------------------------------------------------------------------
# Part 1: ManualModeOverride + the process-scoped registry
# ---------------------------------------------------------------------------


@dataclass
class ManualModeOverride:
    """One candidate's manual-mode answer — complete or partial.

    candidate_id: matches a real TransformationCandidate.candidate_id.
    chosen_option_id: the answer to use instead of asking (e.g. "apply",
        "skip") — empty string means "not yet decided."
    reference_source: human-readable citation, e.g. "Reference deck: 'Data
        Science Job Market Analysis', slide 6, Industry & Job Title" — empty
        string means "not yet cited."
    supplied_data: the actual reference payload this override applies (e.g.
        a full {raw_value: group} mapping for categorical_consolidation) —
        may be empty, partially filled, or complete.
    """

    candidate_id: str
    chosen_option_id: str = ""
    reference_source: str = ""
    supplied_data: dict = field(default_factory=dict)


_ACTIVE_MANUAL_MODE_OVERRIDES: dict = {}


def apply_manual_mode(overrides: list) -> None:
    """Registers a set of overrides for the current pipeline run (process-
    scoped — see module docstring). Replaces whatever was previously active.
    An override may be complete (Part 1's simple case) or partial/empty
    (Part 5's progressive-disclosure case — a candidate_id present here at
    all marks it as "under manual mode," even with nothing filled in yet).
    """
    global _ACTIVE_MANUAL_MODE_OVERRIDES
    _ACTIVE_MANUAL_MODE_OVERRIDES = {o.candidate_id: o for o in overrides}


def get_manual_mode_override(candidate_id: str) -> "ManualModeOverride | None":
    return _ACTIVE_MANUAL_MODE_OVERRIDES.get(candidate_id)


def clear_manual_mode() -> None:
    """Deactivates manual mode entirely for this process (mainly for tests
    and for a caller that wants to guarantee a clean slate between runs)."""
    global _ACTIVE_MANUAL_MODE_OVERRIDES
    _ACTIVE_MANUAL_MODE_OVERRIDES = {}


def _is_override_complete(candidate, override: "ManualModeOverride", distinct_values: "list | None" = None) -> bool:
    """True only when nothing more needs to be asked. For
    categorical_consolidation with an "apply"-shaped choice, supplied_data
    must cover EVERY real distinct value (when distinct_values is given) —
    a mapping missing even one raw value is incomplete, never silently
    treated as good enough."""
    if override is None or not override.chosen_option_id:
        return False
    if candidate.kind == "categorical_consolidation" and override.chosen_option_id not in ("skip",):
        if not override.supplied_data:
            return False
        if distinct_values is not None:
            missing = set(distinct_values) - set(override.supplied_data.keys())
            if missing:
                return False
    return True


# ---------------------------------------------------------------------------
# Reference mapping file versioning (Part 3's static-file shape; Part 5's
# "saves the result as a new reference file" requirement)
# ---------------------------------------------------------------------------


def _reference_base_name(column: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", column.strip().lower()).strip("_")
    return f"{slug}_categories"


def _existing_reference_versions(base_name: str) -> list:
    _REFERENCE_MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    versions = []
    for p in _REFERENCE_MAPPINGS_DIR.glob(f"{base_name}_v*.json"):
        m = re.search(rf"^{re.escape(base_name)}_v(\d+)\.json$", p.name)
        if m:
            versions.append(int(m.group(1)))
    return sorted(versions)


def load_latest_reference_mapping_file(column: str) -> "dict | None":
    """Returns the highest-versioned saved reference mapping file's payload
    for this column, or None if none has ever been saved."""
    base_name = _reference_base_name(column)
    versions = _existing_reference_versions(base_name)
    if not versions:
        return None
    latest_version = versions[-1]
    path = _REFERENCE_MAPPINGS_DIR / f"{base_name}_v{latest_version}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def save_reference_mapping_file(
    column: str, mapping: dict, reference_source: str, provenance: "dict | None" = None,
) -> Path:
    """Writes a NEW versioned reference mapping file — never overwrites a
    prior version (Part 5's explicit requirement: "never overwriting a
    prior version"). provenance, when given, is {"supplied": [...raw values
    the operator explicitly gave], "inferred": [...raw values the pipeline
    proposed and the operator confirmed/edited]} — kept so every part of the
    mapping's origin stays honest and auditable (Part 5's "partial-
    information case" requirement).
    """
    base_name = _reference_base_name(column)
    versions = _existing_reference_versions(base_name)
    next_version = (versions[-1] + 1) if versions else 1

    payload = {
        "column": column,
        "version": next_version,
        "declared_category_count": len(set(mapping.values())),
        "reference_source": reference_source,
        "provenance": provenance or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mapping": mapping,
    }
    _REFERENCE_MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = _REFERENCE_MAPPINGS_DIR / f"{base_name}_v{next_version}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def override_from_reference_file(candidate, payload: dict) -> ManualModeOverride:
    """Builds a complete ManualModeOverride from a saved reference file's
    payload (as returned by load_latest_reference_mapping_file) — the "from
    this point forward, this candidate behaves exactly as if the file had
    existed all along" path (Part 5)."""
    return ManualModeOverride(
        candidate_id=candidate.candidate_id,
        chosen_option_id="apply",
        reference_source=f"{payload.get('reference_source', '')} (reference_mappings/{_reference_base_name(candidate.columns[0])}_v{payload.get('version')}.json)",
        supplied_data=dict(payload.get("mapping", {})),
    )


# ---------------------------------------------------------------------------
# Part 5: progressive disclosure — resolve_manual_mode_candidate
# ---------------------------------------------------------------------------


def _parse_inline_mapping_json(raw: str) -> "dict | None":
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(k): str(v) for k, v in parsed.items()}


def resolve_manual_mode_candidate(
    candidate,
    partial_override: "ManualModeOverride | None" = None,
    distinct_values: "list | None" = None,
    llm=None,
) -> "ManualModeOverride | None":
    """Entry point for manual mode on one candidate (Part 5). Inspects what's
    already known (partial_override may have some fields filled, none, or
    all) and interactively fills EXACTLY the gaps — never re-asks for
    information already supplied. Returns a complete ManualModeOverride
    (persisted to a new reference file), or None when the operator chooses
    option [3]/[4] — explicitly falling through to Spec 1's normal behavior
    for this candidate, with NO override saved (the spec's own pseudocode
    return type doesn't have a way to express this "opt out" outcome, so
    None is the deliberate, documented signal for it — mirrored by every
    caller of this function).
    """
    override = partial_override or ManualModeOverride(candidate_id=candidate.candidate_id)
    distinct_values = list(distinct_values or [])

    if _is_override_complete(candidate, override, distinct_values):
        return override

    col_label = ", ".join(candidate.columns)
    already_have = bool(override.reference_source or override.supplied_data or override.chosen_option_id)

    print(f'Manual mode is on for "{col_label}" ({len(distinct_values)} distinct values found).')
    if already_have:
        missing_count = len(set(distinct_values) - set(override.supplied_data.keys())) if distinct_values else 0
        print(
            f"I already have a partial reference for this ({len(override.supplied_data)} of "
            f"{len(distinct_values)} values covered) — I only need to fill in the rest."
        )
    else:
        print("I don't have a reference mapping for this yet. How do you want to proceed?")
    print()
    print("  [1] I have a reference — describe it or point me to it now")
    print("  [2] Let me propose a grouping and you approve/edit it")
    print("  [3] Use the normal AI-driven grouping (exits manual mode for this candidate)")
    print("  [4] Keep raw categories, no consolidation")

    answer = None
    while answer not in ("1", "2", "3", "4"):
        answer = input("Choose an option (1/2/3/4): ").strip()

    if answer in ("3", "4"):
        print(
            "Falling through to the normal Transformation Options flow for this candidate "
            "— no reference file saved."
        )
        return None

    reference_source = override.reference_source
    if not reference_source and answer == "1":
        # Only Option [1] ("I have a reference") needs a citation up front —
        # Option [2]'s citation is generated afterward from what actually
        # happened (AI-proposed, operator-reviewed), never asked for here.
        reference_source = input(
            "Describe your reference (a citation), e.g. \"Reference deck: 'X', slide N\": "
        ).strip()

    supplied = dict(override.supplied_data)
    missing = [v for v in distinct_values if v not in supplied]
    supplied_this_session: set = set()
    inferred_this_session: set = set()

    if answer == "1":
        if missing:
            print(
                f"I still need group assignments for {len(missing)} of {len(distinct_values)} "
                "values (anything already supplied is kept as-is)."
            )
            raw = input(
                'Paste the mapping for the missing values as one line of JSON '
                '({"raw value": "group", ...}): '
            )
            parsed = _parse_inline_mapping_json(raw) or {}
            for k, v in parsed.items():
                if k in missing:
                    supplied[k] = v
                    supplied_this_session.add(k)
    elif answer == "2":
        if missing:
            from utils.categorical_consolidation import _generate_categorical_consolidation_mapping

            proposed = _generate_categorical_consolidation_mapping(missing, llm) if llm is not None else None
            proposed = proposed or {}
            if proposed:
                print("I propose this grouping for the values you haven't already covered:")
                for k, v in proposed.items():
                    print(f"    {k!r} -> {v!r}")
                edit = input(
                    "Type 'yes' to approve as-is, or paste corrections as one line of JSON "
                    "({\"raw value\": \"group\", ...}) to override specific entries, or "
                    "anything else to keep it as proposed: "
                ).strip()
                corrections = _parse_inline_mapping_json(edit) or {}
                for k in missing:
                    if k in corrections:
                        supplied[k] = corrections[k]
                        supplied_this_session.add(k)
                    elif k in proposed:
                        supplied[k] = proposed[k]
                        inferred_this_session.add(k)
            if not reference_source:
                reference_source = "AI-proposed grouping, reviewed and approved by the operator."

    still_missing = [v for v in distinct_values if v not in supplied]
    if still_missing:
        print(
            f"{len(still_missing)} value(s) still have no group assignment — leaving this "
            "candidate incomplete for now, no reference file saved."
        )
        return None

    resolved = ManualModeOverride(
        candidate_id=candidate.candidate_id,
        chosen_option_id="apply",
        reference_source=reference_source or "Manually supplied, no citation given.",
        supplied_data=supplied,
    )

    provenance = {
        "supplied": sorted(k for k in supplied if k not in inferred_this_session),
        "inferred": sorted(inferred_this_session),
    }
    saved_path = save_reference_mapping_file(
        candidate.columns[0], supplied, resolved.reference_source, provenance=provenance,
    )
    print(f"Saved this mapping to {saved_path} — future runs will use it silently.")
    return resolved


# ---------------------------------------------------------------------------
# Part 4: checklist view (shared infrastructure)
# ---------------------------------------------------------------------------


def build_step_checklist(steps: list) -> list:
    """Derives a compact, scannable checklist from the same real
    NarrativeStep data Spec 2 assembles — never a second source of truth
    (every field here is read straight off a step's own .stats, nothing
    re-derived or re-computed). One dict per step:
    {"step_number", "part", "title", "outcome", "reference_source"}.
    `reference_source` is non-empty ONLY for a manual-mode step, so the
    checklist itself shows "this matched a known reference" at a glance —
    Part 4's own explicit requirement.
    """
    checklist = []
    for step in steps:
        stats = step.stats or {}
        if stats.get("low_emphasis"):
            outcome = "Not asked about (not relevant to this question)"
        elif stats.get("manual_mode"):
            outcome = "Pinned to reference"
        elif "fresh" in stats:
            if stats.get("reload_reask"):
                outcome = "Re-asked after reload"
            elif stats.get("fresh"):
                outcome = "Decided fresh this run"
            else:
                outcome = "Reused from prior decision"
        elif "resolved" in stats:
            outcome = "Resolved" if stats.get("resolved") else "Unresolved"
        else:
            outcome = ""

        checklist.append({
            "step_number": step.step_number,
            "part": step.part,
            "title": step.title,
            "outcome": outcome,
            "reference_source": stats.get("reference_source", "") if stats.get("manual_mode") else "",
        })
    return checklist
