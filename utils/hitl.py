"""Unified Human-In-The-Loop interaction layer (Spec 13, Part 2).

Before this, four separate human-in-the-loop mechanisms existed with no
shared shape and no unified record: cleaning-code approval
(utils.data_cleaning._request_approval), Transformation Options' numbered
menu (utils.transformation_options.present_transformation_options), Manual
Mode's 4-option menu (utils.manual_mode.resolve_manual_mode_candidate), and
Scratch Mode's approval (agents.sql_analyst.run_scratch_mode, already
incidentally reusing _request_approval, not by design). Cleaning approvals
weren't logged anywhere beyond stdout; Transformation decisions were logged
in a completely different shape/location (_transformation_decisions).

This module is the one shared primitive every HITL gate in this project now
calls into, and the one place every real human decision gets logged, to
logs/hitl_log.jsonl — the first unified transcript across cleaning,
transforming, and visualizing.

Deliberately a PURE REFACTOR of the two single-shot decision shapes that
genuinely already had the same structure everywhere they appeared:
- "approve or decline a piece of generated code" (single read, no re-prompt —
  a typo IS a decline; this exact discipline is preserved unchanged)
- "choose one of N options" (loop until a valid option id is given)

Manual Mode's multi-step wizard (citation entry, JSON-paste, an approve/edit
sub-flow) is a genuinely different interaction shape — only its initial
1-4 menu choice (structurally identical to the "choose one of N options"
case) is routed through this layer; the nested follow-up prompts are left
as their own bespoke input() calls rather than force-fit into a primitive
that doesn't actually match their shape. Extending real free-text/multi-step
support here is legitimate future work, not silently pretended to already
be done.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HITL_LOG_PATH = _PROJECT_ROOT / "logs" / "hitl_log.jsonl"


def log_hitl_decision(decision_type: str, title: str, context: str, response: str) -> None:
    """Appends one line to logs/hitl_log.jsonl. Never raises — a logging
    failure must never be allowed to block or corrupt the real decision it's
    recording (same "never let bookkeeping break the real operation"
    discipline this project already applies elsewhere, e.g. _record_ingestion
    never blocking a real fetch result)."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision_type": decision_type,
        "title": title,
        "context": context,
        "response": response,
    }
    try:
        _HITL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_HITL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def request_decision(
    decision_type: str,
    title: str,
    context: str,
    prompt: str,
    valid_responses: "set | None" = None,
    retry_until_valid: bool = False,
    banner: "str | None" = None,
) -> str:
    """The one shared interaction primitive. Prints a consistent format — a
    boxed header naming the kind of decision, the real context, then the
    prompt — reads real input(), and unconditionally logs the outcome.

    banner: the exact text printed as the header line (defaults to
    decision_type.upper() if not given). Kept deliberately separate from
    decision_type (the machine-readable log category) so a caller migrating
    an existing gate onto this shared primitive can preserve its EXACT
    pre-existing printed banner for behavioral compatibility — a real
    regression this design fixes: test_transformation_options_live_wiring.py
    parses stdout for the literal substring "TRANSFORMATION OPTION:", which
    a mechanically-derived decision_type.upper() banner would have broken.

    retry_until_valid=False (the default): reads exactly once and returns
    whatever was typed, valid or not — the discipline
    utils.data_cleaning._request_approval already uses (a typo is just
    another way of not saying "yes", never a re-prompt).

    retry_until_valid=True: loops until the answer is in valid_responses —
    the discipline present_transformation_options'/resolve_manual_mode_
    candidate's menus already use. valid_responses is required in this mode.

    Returns the raw response string in both modes — coercing to a bool or
    validating against a real option list is the caller's job, so this stays
    a general-purpose primitive usable for both shapes.
    """
    display_banner = banner if banner is not None else decision_type.upper()
    print("=" * 70)
    print(f"{display_banner}: {title}")
    print("=" * 70)
    if context:
        print(context)
    print()

    if retry_until_valid:
        if not valid_responses:
            raise ValueError("retry_until_valid=True requires a non-empty valid_responses set")
        response = None
        while response not in valid_responses:
            response = input(prompt).strip()
            if response not in valid_responses:
                print(f"{response!r} is not a valid response — try again.")
    else:
        response = input(prompt).strip()

    log_hitl_decision(decision_type, title, context, response)
    return response


def request_code_approval(code: str, file_path, decision_type: str = "code_approval") -> bool:
    """Drop-in behavioral replacement for the print/input logic inside
    utils.data_cleaning._request_approval — same exact printed code block
    and prompt text, same single-read-no-reprompt discipline, same "yes"
    (case-insensitive, stripped) is the only approving answer. Adds the one
    real new thing: logging to the unified transcript.
    """
    print("\n" + "=" * 70)
    print(f"GENERATED CLEANING CODE for: {file_path}")
    print("=" * 70)
    print(code)
    print("=" * 70)
    answer = input(f"Run this code against {file_path}? Type 'yes' to approve, anything else to decline: ")
    approved = answer.strip().lower() == "yes"
    log_hitl_decision(decision_type, f"Approve generated code for {file_path}", code, answer.strip())
    return approved


def request_option_choice(
    options: list, title: str, context: str = "", decision_type: str = "option_choice", banner: "str | None" = None
) -> str:
    """Shared "choose one of N options" prompt. options: list of {"id",
    "label", "description"}. Loops until a valid option id is chosen (same
    discipline present_transformation_options already uses). Logs the
    decision. Returns the chosen option id. See request_decision's own
    docstring for why banner is kept separate from decision_type."""
    valid_ids = {opt["id"] for opt in options}
    lines = [f"  [{opt['id']}] {opt['label']} — {opt.get('description', '')}" for opt in options]
    full_context = (context + "\n\n" if context else "") + "\n".join(lines)

    return request_decision(
        decision_type=decision_type,
        title=title,
        context=full_context,
        prompt=f"Choose an option ({'/'.join(sorted(valid_ids))}): ",
        valid_responses=valid_ids,
        retry_until_valid=True,
        banner=banner,
    )
