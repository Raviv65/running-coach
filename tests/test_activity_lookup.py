"""
Tests for activity id resolution across a Runalyze merge.

A FIT upload creates a `suunto-<date>-<minutes>` row. A later Runalyze sync folds
that row into the Runalyze activity and the numeric id becomes canonical, so any
page already open on the synthetic id used to 404. _find_activity resolves both.

Needs the app dependencies (flask, google-cloud-storage) installed; skips cleanly
without them. No GCS credentials or network required — the lookup is pure dict work.

Run with: python -m pytest tests/test_activity_lookup.py -v
      or: python tests/test_activity_lookup.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from app import _find_activity
    DEPS_OK = True
except ImportError as e:  # flask / google-cloud not installed
    DEPS_OK = False
    _IMPORT_ERR = e


def _db(*rows):
    return {"activities": {"2026-10-14": list(rows)}}


# The real shape after the merge drops the uploaded suunto-* row.
MERGED = _db({"id": "215996409", "duration_min": 50.0,
              "prev_ids": ["suunto-2026-10-14-50"]})


def test_canonical_id_resolves():
    assert _find_activity(MERGED, "215996409")["id"] == "215996409"


def test_pre_merge_synthetic_id_still_resolves():
    # The regression: this 404'd, and the UI blamed the user's connection.
    assert _find_activity(MERGED, "suunto-2026-10-14-50")["id"] == "215996409"


def test_unknown_id_returns_none():
    assert _find_activity(MERGED, "no-such-activity") is None


def test_current_id_wins_over_another_rows_alias():
    db = _db({"id": "A", "prev_ids": ["B"]}, {"id": "B"})
    assert _find_activity(db, "B")["id"] == "B"


def test_alias_matching_coerces_to_str():
    db = _db({"id": "N", "prev_ids": [123]})
    assert _find_activity(db, "123")["id"] == "N"


def test_rows_without_prev_ids_are_safe():
    assert _find_activity(_db({"id": "Z"}), "Z")["id"] == "Z"


def test_empty_and_missing_structures_are_safe():
    assert _find_activity({}, "x") is None
    assert _find_activity({"activities": {}}, "x") is None
    assert _find_activity({"activities": {"d": []}}, "x") is None


def test_lookup_spans_multiple_days():
    db = {"activities": {
        "2026-10-11": [{"id": "215284369"}],
        "2026-10-14": [{"id": "215996409", "prev_ids": ["suunto-2026-10-14-50"]}],
    }}
    assert _find_activity(db, "215284369")["id"] == "215284369"
    assert _find_activity(db, "suunto-2026-10-14-50")["id"] == "215996409"


if __name__ == "__main__":
    if not DEPS_OK:
        print(f"SKIP  app dependencies not installed ({_IMPORT_ERR})")
        raise SystemExit(0)
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {e.__class__.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
