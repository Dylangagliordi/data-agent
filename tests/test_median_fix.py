"""
Confirm the median calculation in _outlier_sensitivity_note is correct for
even-length lists.

The old code used vals[len(vals) // 2] which returns the upper-middle element
of a sorted list rather than the true median.  For [1, 10, 11, 33]:
  - Old: vals[2] = 11 → threshold = 33; 33 > 33 is False → no note fired
  - Correct: statistics.median([1,10,11,33]) = 10.5 → threshold = 31.5;
    33 > 31.5 is True → note correctly fires

This test uses that exact dataset to prove the fix.
"""

import statistics

from agents.sql_analyst import _outlier_sensitivity_note

if __name__ == "__main__":
    print("=" * 70)
    print("STEP 1: Even-length list [1, 10, 11, 33] — old code gave wrong median")
    print("=" * 70)

    # Verify that statistics.median gives the correct value
    vals = [1, 10, 11, 33]
    correct_median = statistics.median(vals)
    old_median = vals[len(vals) // 2]
    print(f"sorted vals: {vals}")
    print(f"statistics.median: {correct_median}  (correct)")
    print(f"old vals[n//2]:    {old_median}  (wrong for even-length list)")
    assert correct_median == 10.5, f"expected 10.5, got {correct_median}"
    assert old_median == 11, f"expected 11 for old code, got {old_median}"

    # With old code: max=33, old_median=11, 33 > 3*11=33 is False → no note
    assert not (33 > 3 * old_median), "confirm old code would NOT fire here"
    # With correct code: 33 > 3*10.5=31.5 is True → note fires
    assert 33 > 3 * correct_median, "confirm correct code DOES fire here"
    print("Confirmed: old code gives wrong result, correct code gives right result.")

    print("\n" + "=" * 70)
    print("STEP 2: _outlier_sensitivity_note fires for [1, 10, 11, 33]")
    print("=" * 70)

    result_data = [
        {"group": "A", "metric": 1},
        {"group": "B", "metric": 10},
        {"group": "C", "metric": 11},
        {"group": "D", "metric": 33},
    ]
    note = _outlier_sensitivity_note(result_data)
    print(f"note: {note!r}")
    assert note, (
        "expected _outlier_sensitivity_note to fire for [1,10,11,33] — "
        "33 is more than 3× the correct median of 10.5"
    )
    print("PASSED: note fires correctly with fixed median.")

    print("\n" + "=" * 70)
    print("STEP 3: Odd-length list still works correctly")
    print("=" * 70)

    # For an odd list statistics.median == vals[n//2], so both agree
    result_odd = [
        {"group": "A", "metric": 1},
        {"group": "B", "metric": 3},
        {"group": "C", "metric": 4},
        {"group": "D", "metric": 5},
        {"group": "E", "metric": 100},
    ]
    note_odd = _outlier_sensitivity_note(result_odd)
    print(f"odd-list note: {note_odd!r}")
    assert note_odd, "expected note to fire for [1,3,4,5,100] — 100 >> 3×median(4)=12"
    print("PASSED: odd-length list still works.")

    print("\n" + "=" * 70)
    print("STEP 4: No false positive when no outlier is present")
    print("=" * 70)

    result_flat = [
        {"group": "A", "metric": 10},
        {"group": "B", "metric": 11},
        {"group": "C", "metric": 12},
        {"group": "D", "metric": 13},
    ]
    note_flat = _outlier_sensitivity_note(result_flat)
    print(f"flat note: {note_flat!r}")
    assert not note_flat, f"expected no note for flat data [10,11,12,13], got: {note_flat!r}"
    print("PASSED: no false positive for evenly distributed data.")

    print("\n" + "=" * 70)
    print("ALL ASSERTIONS PASSED")
    print("=" * 70)
