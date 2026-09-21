"""Regression tests for lookup-key integrity of the canonical 5-field key.

Canonical lookup key:
    (metric, request, scope, frequency_selection, unit)

AUDIT VERDICT (verified against the actual workbook, see audit_keys.py and
audit/duplicate_keys_5field.txt): the 5-field key is NOT fully unique.
80 duplicate keys remain, covering 417 rows. All are parameterised queries
(Threshold Above/Below, Closest value, Range, TNP power difference) whose
distinguishing parameter (threshold / closest / range bound, or the TNP
reference qualifier) appears ONLY inside the QUESTION text — the workbook
has no column for it. A 6th "query parameter" dimension is required to
resolve them.

These tests FAIL if the duplicate count changes in any direction, so any
data or key change forces a re-audit.
"""

from __future__ import annotations

import pytest

from rf_catalogue.engine import EXACT_MATCH, AMBIGUOUS, NOT_FOUND


# ---------------------------------------------------------------------------
# Fixture: session-scoped `engine` comes from conftest.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pinned integrity numbers (audit-verified). Any deviation fails the suite.
# ---------------------------------------------------------------------------


def test_total_non_placeholder_records_pinned(engine):
    """Total non-placeholder records must remain 4768 (audit-verified)."""
    assert engine.total_rows() == 4768, (
        "Non-placeholder record count changed; re-run audit_keys.py and "
        "update this pin only after a fresh audit."
    )


def test_unique_5field_keys_pinned(engine):
    """Unique 5-field keys must remain 4431 (audit-verified)."""
    assert engine.unique_lookup_keys() == 4431, (
        "Unique 5-field key count changed; re-run audit_keys.py."
    )


def test_duplicate_5field_keys_pinned(engine):
    """REGRESSION GUARD: duplicate 5-field keys must remain exactly 80.

    If this number changes (up OR down), the lookup-key state has changed
    and the suite fails until audit_keys.py is re-run against the workbook.
    """
    dup = engine.duplicate_lookup_keys()
    assert dup == 80, (
        f"Duplicate 5-field keys changed: expected 80, got {dup}. "
        f"The production lookup key state changed — re-run the full "
        f"lookup-key integrity audit (audit_keys.py) before proceeding."
    )


@pytest.mark.xfail(
    reason="5-field key is NOT unique: 80 known duplicate keys remain "
           "(417 rows). A 6th 'query parameter' dimension is required. "
           "This test passes only when the key becomes truly unique.",
    strict=False,
)
def test_5field_key_is_unique(engine):
    """GOAL: the canonical 5-field key is unique (currently unmet).

    XFAIL INVESTIGATION (2026-09-13):
    - Why xfail: the audited 5-field key has 80 duplicate keys / 417 rows;
      `duplicate_lookup_keys() == 0` cannot pass against the current workbook.
    - Intentional known limitation: YES. The base 5-field production key is
      deliberately unchanged; the workbook stores the differentiating
      parameter only inside QUESTION text, so the base key cannot be made
      unique without a new catalogue column.
    - Remain xfail or fix? REMAIN. The canonical QueryIntent representation
      (rf_catalogue.query_intent + engine.lookup_by_intent) now resolves all
      417 rows exactly (see tests/test_query_intent.py — 0 duplicates, 0
      parse failures), but that is a validated lookup VIEW, not a change to
      the 5-field key itself. This test should be removed only if a future
      task adds a real query-parameter column to the workbook, making the
      raw key unique.
    """
    assert engine.duplicate_lookup_keys() == 0


def test_no_true_content_clones_in_duplicate_keys(engine):
    """No two rows sharing a 5-field key may share identical QUESTION text.

    This proves the 80 duplicates are parameter-family rows (distinct
    threshold/closest/range parameters), not accidental duplicate records.
    """
    clones = []
    for key, rows in engine._index.items():
        if len(rows) > 1:
            questions = [r.question for r in rows]
            if len(set(questions)) != len(questions):
                clones.append(key)
    assert not clones, f"True content clones found for keys: {clones}"


# ---------------------------------------------------------------------------
# Known duplicate classes MUST return AMBIGUOUS (never guess)
# ---------------------------------------------------------------------------


def test_threshold_above_all_nodes_is_ambiguous(engine):
    """MismatchLoss / Threshold Above / All nodes / All Frequencies / dB
    matches 7 rows (thresholds 0.1, 0.5, 1, 2, 3, 5, 10 dB) -> AMBIGUOUS.
    The unit dimension does NOT resolve threshold queries."""
    result = engine.lookup("MismatchLoss", "Threshold Above", "All nodes",
                           "All Frequencies", "dB")
    assert result.kind == AMBIGUOUS, f"Expected AMBIGUOUS, got {result.kind}"
    assert result.rows  # engine returns up to 3 sample rows


def test_threshold_below_all_nodes_is_ambiguous(engine):
    """MismatchLoss / Threshold Below / All nodes is also AMBIGUOUS."""
    result = engine.lookup("MismatchLoss", "Threshold Below", "All nodes",
                           "All Frequencies", "dB")
    assert result.kind == AMBIGUOUS, f"Expected AMBIGUOUS, got {result.kind}"


def test_ccomp_threshold_above_db20_is_ambiguous(engine):
    """CCOMP / Threshold Above / All nodes / All Frequencies / dB20 has 5
    parameter rows (0.1, 0.5, 1, 2, 3 dB20) -> AMBIGUOUS."""
    result = engine.lookup("CCOMP", "Threshold Above", "All nodes",
                           "All Frequencies", "dB20")
    assert result.kind == AMBIGUOUS, f"Expected AMBIGUOUS, got {result.kind}"


@pytest.mark.parametrize("freq", [
    "1200 MHz", "1300 MHz", "1400 MHz", "1500 MHz",
    "2400 MHz", "3000 MHz",
])
def test_tnp_power_difference_is_ambiguous(engine, freq):
    """TNP / Individual frequency power difference / Final node / <freq> / dBm
    matches 2 rows ('above desired channel power' vs 'above channel power')
    -> AMBIGUOUS. The reference qualifier lives only in QUESTION text."""
    result = engine.lookup("TNP", "Individual frequency power difference",
                           "Final node", freq, "dBm")
    assert result.kind == AMBIGUOUS, (
        f"Expected AMBIGUOUS for {freq}, got {result.kind}: {result.message}"
    )


# ---------------------------------------------------------------------------
# Non-duplicate keys MUST return EXACT_MATCH (unit resolves these)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [
    ("MismatchLoss", "Best", "Final node", "All Frequencies", "dB"),
    ("MismatchLoss", "Worst", "Final node", "All Frequencies", "dB"),
    ("MismatchLoss", "Part summary", "ADL8124", "All Frequencies", "dB"),
    ("MismatchLoss", "Node summary", "2", "All Frequencies", "dB"),
    ("CGAIN", "Best", "Final node", "All Frequencies", "dB"),
    ("GAIN", "Best", "Final node", "All Frequencies", "dB"),
    ("CGAIN", "Individual frequency final value", "Final node", "1500 MHz", "dB"),
    ("DCP", "Particular frequency", "Final node", "1200 MHz", "dBm"),
    ("CCOMP", "Best", "Final node", "All Frequencies", "dB20"),
    ("TNP", "Best", "Final node", "All Frequencies", "dBm"),
])
def test_representative_non_duplicate_keys_exact(engine, key):
    """Representative keys outside the 80 duplicates resolve EXACTLY."""
    result = engine.lookup(*key)
    assert result.kind == EXACT_MATCH, (
        f"Expected EXACT_MATCH for {key}, got {result.kind}: {result.message}"
    )


def test_unknown_key_not_found(engine):
    """A key outside the catalogue returns NOT_FOUND."""
    result = engine.lookup("MADEUPMETRIC", "Best", "Final node",
                           "All Frequencies", "dB")
    assert result.kind == NOT_FOUND


def test_duplicate_keys_use_all_metrics_documented(engine):
    """The 80 duplicate keys span exactly the audited metric set."""
    metrics = {key[0] for key, rows in engine._index.items() if len(rows) > 1}
    expected = {
        "CCOMP", "CGAIN", "CNDR", "CNF", "CNP", "CP", "DCP", "GAIN",
        "IP1DB", "IPSAT", "MismatchLoss", "NDCP", "OP1DB", "OPSAT", "TNP",
    }
    assert metrics == expected, (
        f"Duplicate-key metric set changed: extra={metrics - expected}, "
        f"missing={expected - metrics}"
    )
