"""Spec 3 (Final): tests utils/manual_mode.py — Parts 1 (registry), 3/5
(reference-file versioning + progressive disclosure), and 4 (checklist).

No DB, no live LLM required for the scripted assertions (a fake LLM is used
for the Option [2] AI-clustering sub-flow). Real stdin is used for the
interactive resolve_manual_mode_candidate flow via monkeypatched
builtins.input, same discipline as this project's other in-process
interactive tests (see test_composite_field_split.py's redirect_stdin_yes).

_REFERENCE_MAPPINGS_DIR is monkeypatched to a temp directory for the whole
run so these tests never touch the real utils/reference_mappings/ folder.
"""

import shutil
import tempfile
from pathlib import Path

import utils.manual_mode as mm
from utils.manual_mode import (
    ManualModeOverride,
    _is_override_complete,
    apply_manual_mode,
    build_step_checklist,
    clear_manual_mode,
    get_manual_mode_override,
    load_latest_reference_mapping_file,
    override_from_reference_file,
    resolve_manual_mode_candidate,
    save_reference_mapping_file,
)
from utils.narrative import NarrativeStep

_TMP_DIR = tempfile.mkdtemp(prefix="manual_mode_test_")
mm._REFERENCE_MAPPINGS_DIR = Path(_TMP_DIR)


class _FakeCandidate:
    def __init__(self, candidate_id, columns, kind="categorical_consolidation"):
        self.candidate_id = candidate_id
        self.columns = columns
        self.kind = kind


def _feed_input(monkeypatch_lines):
    """Returns a callable that replaces builtins.input, yielding one queued
    line per call and raising EOFError if exhausted (never silently hangs)."""
    it = iter(monkeypatch_lines)

    def _fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError("test ran out of scripted input lines")

    return _fake_input


try:
    print("=" * 70)
    print("TEST 1: apply_manual_mode / get_manual_mode_override registry")
    print("=" * 70)

    clear_manual_mode()
    assert get_manual_mode_override("cand_a") is None
    o1 = ManualModeOverride(candidate_id="cand_a", chosen_option_id="apply", reference_source="ref A", supplied_data={"x": "y"})
    apply_manual_mode([o1])
    assert get_manual_mode_override("cand_a") is o1
    assert get_manual_mode_override("cand_b") is None, "a candidate with no override present must resolve to None"

    # apply_manual_mode REPLACES, never merges.
    o2 = ManualModeOverride(candidate_id="cand_b", chosen_option_id="skip", reference_source="", supplied_data={})
    apply_manual_mode([o2])
    assert get_manual_mode_override("cand_a") is None, "a fresh apply_manual_mode call must replace the prior set"
    assert get_manual_mode_override("cand_b") is o2
    clear_manual_mode()
    assert get_manual_mode_override("cand_b") is None
    print("PASS: registry registers, replaces, and clears correctly; an unregistered candidate resolves to None.\n")

    print("=" * 70)
    print("TEST 2: _is_override_complete")
    print("=" * 70)

    cand_cc = _FakeCandidate("cc1", ["Industry"], kind="categorical_consolidation")
    cand_other = _FakeCandidate("rd1", ["Salary Estimate"], kind="range_decomposition")

    empty = ManualModeOverride(candidate_id="cc1")
    assert not _is_override_complete(cand_cc, empty)
    assert not _is_override_complete(cand_cc, None)

    partial = ManualModeOverride(candidate_id="cc1", chosen_option_id="apply", supplied_data={"a": "G1"})
    assert not _is_override_complete(cand_cc, partial, distinct_values=["a", "b", "c"]), (
        "supplied_data covering only some real distinct values must be incomplete"
    )

    full = ManualModeOverride(candidate_id="cc1", chosen_option_id="apply", supplied_data={"a": "G1", "b": "G1", "c": "G2"})
    assert _is_override_complete(cand_cc, full, distinct_values=["a", "b", "c"])

    skip_override = ManualModeOverride(candidate_id="cc1", chosen_option_id="skip")
    assert _is_override_complete(cand_cc, skip_override, distinct_values=["a", "b", "c"]), (
        "a 'skip' choice needs no supplied_data at all to be complete"
    )

    other_complete = ManualModeOverride(candidate_id="rd1", chosen_option_id="apply")
    assert _is_override_complete(cand_other, other_complete), (
        "a non-categorical_consolidation candidate only needs chosen_option_id set"
    )
    print("PASS: completeness correctly requires full raw-value coverage for categorical_consolidation, "
          "and just a chosen_option_id for other kinds; 'skip' never needs supplied_data.\n")

    print("=" * 70)
    print("TEST 3: reference mapping file versioning — never overwrites")
    print("=" * 70)

    mapping_v1 = {"a": "G1", "b": "G1", "c": "G2"}
    path1 = save_reference_mapping_file("Industry", mapping_v1, "Test reference v1")
    assert path1.name == "industry_categories_v1.json", path1.name
    payload1 = load_latest_reference_mapping_file("Industry")
    assert payload1["version"] == 1
    assert payload1["mapping"] == mapping_v1
    assert payload1["declared_category_count"] == len(set(mapping_v1.values())) == 2

    mapping_v2 = {"a": "G1", "b": "G2", "c": "G2", "d": "G3"}
    path2 = save_reference_mapping_file("Industry", mapping_v2, "Test reference v2")
    assert path2.name == "industry_categories_v2.json", path2.name
    assert path1.exists(), "v1 must never be overwritten by a v2 save"
    payload2 = load_latest_reference_mapping_file("Industry")
    assert payload2["version"] == 2
    assert payload2["mapping"] == mapping_v2
    print(f"PASS: v1 preserved at {path1}, v2 saved separately at {path2}, latest lookup returns v2.\n")

    override_from_file = override_from_reference_file(cand_cc, payload2)
    assert override_from_file.chosen_option_id == "apply"
    assert override_from_file.supplied_data == mapping_v2
    assert _is_override_complete(cand_cc, override_from_file, distinct_values=list(mapping_v2.keys()))
    print("PASS: override_from_reference_file produces a complete override from a saved file.\n")

    print("=" * 70)
    print("TEST 4: resolve_manual_mode_candidate — no info, option [1], full paste")
    print("=" * 70)

    cand4 = _FakeCandidate("cc_t4", ["JobField"], kind="categorical_consolidation")
    distinct4 = ["Data Scientist", "Data Analyst", "ML Engineer"]
    lines4 = [
        "1",
        "Reference deck: 'Job Market', slide 6",
        '{"Data Scientist": "Technical", "Data Analyst": "Technical", "ML Engineer": "Technical"}',
    ]
    import builtins
    original_input = builtins.input
    builtins.input = _feed_input(lines4)
    try:
        result4 = resolve_manual_mode_candidate(cand4, distinct_values=distinct4)
    finally:
        builtins.input = original_input

    assert result4 is not None
    assert result4.chosen_option_id == "apply"
    assert result4.supplied_data == {v: "Technical" for v in distinct4}
    assert "Job Market" in result4.reference_source
    saved4 = load_latest_reference_mapping_file("JobField")
    assert saved4["mapping"] == result4.supplied_data
    print("PASS: a from-scratch Option [1] session fills the whole mapping and saves a new reference file.\n")

    print("=" * 70)
    print("TEST 5: resolve_manual_mode_candidate — partial override, only asks")
    print("about the missing fields, never re-prompts for given ones")
    print("=" * 70)

    cand5 = _FakeCandidate("cc_t5", ["JobField2"], kind="categorical_consolidation")
    distinct5 = ["Data Scientist", "Data Analyst", "ML Engineer", "Recruiter"]
    partial5 = ManualModeOverride(
        candidate_id="cc_t5",
        reference_source="Already-cited reference",
        supplied_data={"Data Scientist": "Technical", "Data Analyst": "Technical", "ML Engineer": "Technical"},
    )
    # Only ONE line queued: choosing option 1, then the JSON for the single
    # missing value. If the function tried to re-ask for reference_source or
    # for any of the 3 already-supplied values, this would raise EOFError
    # from running out of scripted lines — proving it never re-prompts.
    lines5 = ["1", '{"Recruiter": "Non-technical"}']
    builtins.input = _feed_input(lines5)
    try:
        result5 = resolve_manual_mode_candidate(cand5, partial_override=partial5, distinct_values=distinct5)
    finally:
        builtins.input = original_input

    assert result5 is not None
    assert result5.reference_source == "Already-cited reference", "must reuse the already-given reference_source verbatim"
    assert result5.supplied_data["Recruiter"] == "Non-technical"
    assert result5.supplied_data["Data Scientist"] == "Technical", "already-supplied entries must be kept, not re-asked"
    assert len(result5.supplied_data) == 4
    print("PASS: partial override only asked about the one missing value, reused the given reference_source verbatim.\n")

    print("=" * 70)
    print("TEST 6: resolve_manual_mode_candidate — option [3]/[4] fall through,")
    print("no reference file written")
    print("=" * 70)

    cand6 = _FakeCandidate("cc_t6", ["JobField3"], kind="categorical_consolidation")
    distinct6 = ["A", "B", "C"] * 6  # 18 rows -> use unique set below
    distinct6 = [f"val_{i}" for i in range(16)]

    builtins.input = _feed_input(["3"])
    try:
        result6a = resolve_manual_mode_candidate(cand6, distinct_values=distinct6)
    finally:
        builtins.input = original_input
    assert result6a is None, "option [3] must return None"
    assert load_latest_reference_mapping_file("JobField3") is None, "option [3] must not save a reference file"

    builtins.input = _feed_input(["4"])
    try:
        result6b = resolve_manual_mode_candidate(cand6, distinct_values=distinct6)
    finally:
        builtins.input = original_input
    assert result6b is None, "option [4] must return None"
    assert load_latest_reference_mapping_file("JobField3") is None, "option [4] must not save a reference file"
    print("PASS: options [3] and [4] both return None and never write a reference file.\n")

    print("=" * 70)
    print("TEST 7: resolve_manual_mode_candidate — option [2], AI proposes,")
    print("operator approves as-is")
    print("=" * 70)

    class _FakeClusterLLM:
        def with_structured_output(self, schema_cls):
            return self

        def invoke(self, messages):
            from models.schema import CategoricalAssignment, CategoricalConsolidationProposal
            return CategoricalConsolidationProposal(
                assignments=[
                    CategoricalAssignment(raw_value="Data Scientist", group="Technical"),
                    CategoricalAssignment(raw_value="Recruiter", group="Non-technical"),
                ]
            )

    cand7 = _FakeCandidate("cc_t7", ["JobField4"], kind="categorical_consolidation")
    distinct7 = ["Data Scientist", "Recruiter"]
    lines7 = ["2", "yes"]
    builtins.input = _feed_input(lines7)
    try:
        result7 = resolve_manual_mode_candidate(cand7, distinct_values=distinct7, llm=_FakeClusterLLM())
    finally:
        builtins.input = original_input

    assert result7 is not None
    assert result7.supplied_data == {"Data Scientist": "Technical", "Recruiter": "Non-technical"}
    assert "AI-proposed" in result7.reference_source
    saved7 = load_latest_reference_mapping_file("JobField4")
    assert saved7["provenance"]["inferred"] == ["Data Scientist", "Recruiter"] or set(saved7["provenance"]["inferred"]) == {"Data Scientist", "Recruiter"}
    print("PASS: Option [2] proposes via the AI-clustering call, approves as-is, and saves provenance "
          f"marking both values as inferred: {saved7['provenance']}\n")

    print("=" * 70)
    print("TEST 8: resolve_manual_mode_candidate — already-complete override")
    print("returns immediately, no prompt at all")
    print("=" * 70)

    cand8 = _FakeCandidate("cc_t8", ["JobField5"], kind="categorical_consolidation")
    distinct8 = ["A", "B"]
    complete8 = ManualModeOverride(candidate_id="cc_t8", chosen_option_id="apply", supplied_data={"A": "G", "B": "G"})
    builtins.input = _feed_input([])  # any input() call would raise EOFError
    try:
        result8 = resolve_manual_mode_candidate(cand8, partial_override=complete8, distinct_values=distinct8)
    finally:
        builtins.input = original_input
    assert result8 is complete8
    print("PASS: an already-complete override is returned immediately with zero prompts.\n")

    print("=" * 70)
    print("TEST 9: build_step_checklist derives from real NarrativeStep.stats,")
    print("a manual-mode step visibly shows its reference_source")
    print("=" * 70)

    steps9 = [
        NarrativeStep(1, "cleaning", "Load t", "...", stats={"table": "t", "row_count": 10, "column_count": 3}),
        NarrativeStep(2, "cleaning", "Fix x — t", "...", stats={"table": "t", "resolved": True}),
        NarrativeStep(3, "cleaning", "Fix y — t", "...", stats={"table": "t", "resolved": False}),
        NarrativeStep(4, "transformation", "Range Decomposition for Salary?", "...", stats={"fresh": True, "reload_reask": False}),
        NarrativeStep(5, "transformation", "Company Age for Founded?", "...", stats={"fresh": False, "reload_reask": False}),
        NarrativeStep(6, "transformation", "Categorical Consolidation for Industry?", "...", stats={
            "manual_mode": True, "reference_source": "Reference deck, slide 6", "fresh": True,
        }),
        NarrativeStep(7, "transformation", "Other available transformations (not asked about here)", "...", stats={"low_emphasis": True}),
        NarrativeStep(8, "analysis", "The final result", "...", stats={"columns": ["a"], "rows": [[1]]}),
    ]
    checklist9 = build_step_checklist(steps9)
    assert len(checklist9) == len(steps9), "checklist must never disagree with its source step list in count"
    assert [c["step_number"] for c in checklist9] == [s.step_number for s in steps9]
    assert [c["part"] for c in checklist9] == [s.part for s in steps9]
    assert [c["title"] for c in checklist9] == [s.title for s in steps9]

    by_num = {c["step_number"]: c for c in checklist9}
    assert by_num[2]["outcome"] == "Resolved"
    assert by_num[3]["outcome"] == "Unresolved"
    assert by_num[4]["outcome"] == "Decided fresh this run"
    assert by_num[5]["outcome"] == "Reused from prior decision"
    assert by_num[6]["outcome"] == "Pinned to reference"
    assert by_num[6]["reference_source"] == "Reference deck, slide 6", "manual-mode entry must show its real reference_source"
    assert by_num[7]["reference_source"] == "" and by_num[7]["outcome"].startswith("Not asked")
    # Non-manual-mode entries must never carry a reference_source.
    for n in (1, 2, 3, 4, 5, 8):
        assert by_num[n]["reference_source"] == "", f"step {n} must not carry a reference_source"
    print("PASS: checklist entries derive purely from step.stats, count/order/part/title match the "
          "source list exactly, and only the manual-mode step shows a reference_source.\n")

    print("=" * 70)
    print("ALL MANUAL-MODE (SPEC 3) ASSERTIONS PASSED")
    print("=" * 70)
finally:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
    clear_manual_mode()
