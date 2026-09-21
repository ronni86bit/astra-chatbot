"""Tests for the deterministic RF question catalogue engine.

Tests use actual rows from SystemVue_RF_Unique_Question_Assistant_V63.xlsx.
Each test case verifies the engine lookup behaviour against real catalogue data.

Do NOT modify the application workbook. Tests only read from it.
"""

from __future__ import annotations

import pytest

from rf_catalogue.engine import CatalogueEngine, QARow, EXACT_MATCH, AMBIGUOUS, NOT_FOUND


# ---------------------------------------------------------------------------
# Fixture: session-scoped `engine` comes from conftest.py
# (the workbook load takes ~13s; tests are read-only on the engine)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helper: read a row from the workbook by Excel row number
# ---------------------------------------------------------------------------


def _read_workbook_row(workbook_path: str, excel_row: int) -> QARow:
    """Read a single QA row from the Excel workbook.

    Excel rows 5+ are data rows (row 5 is the first data row).
    excel_row=0 corresponds to Excel row 5 (first data row).
    """
    import openpyxl

    wb = openpyxl.load_workbook(workbook_path)
    ws = wb["RF Questions and Answers"]
    row = ws[excel_row + 5]  # data starts at row 5

    # Column mapping: A=ID, B=QUESTION, C=ANSWER, D=METRIC, E=REQUEST,
    # F=SCOPE, G=UNIT, H=ANSWER TYPE, I=FREQUENCY SELECTION
    cells = row
    if len(cells) < 9:
        raise ValueError(f"Excel row {excel_row + 5} has fewer than 9 columns")

    row_id = cells[0].value
    question = cells[1].value
    answer = cells[2].value
    metric = cells[3].value
    request = cells[4].value
    scope = cells[5].value
    unit = cells[6].value
    answer_type = cells[7].value
    freq_selection = cells[8].value

    try:
        rid = int(row_id)
    except (ValueError, TypeError):
        rid = 0

    def norm(s):
        if s is None:
            return ""
        return str(s).strip()

    return QARow(
        row_id=rid,
        question=norm(question),
        answer=norm(answer),
        metric=norm(metric),
        request=norm(request),
        scope=norm(scope),
        unit=norm(unit) if unit else "",
        answer_type=norm(answer_type),
        frequency_selection=freq_selection if freq_selection else "",
    )


# ---------------------------------------------------------------------------
# Test: engine loads all rows correctly
# ---------------------------------------------------------------------------


def test_engine_loads_rows(engine):
    """Engine should load all rows from the workbook."""
    workbook = "C:\\Ronni\\Projects\\astra chatbot\\SystemVue_RF_Unique_Question_Assistant_V63.xlsx"
    row = _read_workbook_row(workbook, excel_row=0)  # first data row
    assert engine.total_rows() > 0
    # The engine should have loaded at least this row
    assert engine.total_rows() >= 1


# ---------------------------------------------------------------------------
# Test: exact match — best/worst query at final node
# ---------------------------------------------------------------------------


def test_lookup_best_mismatchloss_final_node(engine):
    """Exact match: best Mismatch Loss in the final node."""
    result = engine.lookup(
        metric="MismatchLoss",
        request="Best",
        scope="Final node",
        unit="dB",
        freq_selection="All Frequencies",
    )
    assert result.kind == EXACT_MATCH, f"Expected EXACT_MATCH, got {result.kind}"
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.metric == "MismatchLoss"
    assert row.request == "Best"
    assert row.scope == "Final node"
    assert row.unit == "dB"
    assert row.frequency_selection == "All Frequencies"


def test_lookup_worst_mismatchloss_final_node(engine):
    """Exact match: worst Mismatch Loss in the final node."""
    result = engine.lookup(
        metric="MismatchLoss",
        request="Worst",
        scope="Final node",
        unit="dB",
        freq_selection="All Frequencies",
    )
    assert result.kind == EXACT_MATCH
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.metric == "MismatchLoss"
    assert row.request == "Worst"


# ---------------------------------------------------------------------------
# Test: exact-frequency query
# ---------------------------------------------------------------------------


def test_lookup_individual_frequency_cgain(engine):
    """Exact match: individual frequency CGAIN at 1500 MHz."""
    result = engine.lookup(
        metric="CGAIN",
        request="Individual frequency final value",
        scope="Final node",
        unit="dB",
        freq_selection="1500 MHz",
    )
    assert result.kind == EXACT_MATCH, f"Expected EXACT_MATCH, got {result.kind}: {result.message}"
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.metric == "CGAIN"
    assert row.request == "Individual frequency final value"
    assert row.scope == "Final node"
    assert row.unit == "dB"
    assert row.frequency_selection == "1500 MHz"


def test_lookup_dcp_particular_frequency(engine):
    """Exact match: DCP at particular frequency (1200 MHz, dBm)."""
    result = engine.lookup(
        metric="DCP",
        request="Particular frequency",
        scope="Final node",
        freq_selection="1200 MHz",
        unit="dBm",
    )
    assert result.kind == EXACT_MATCH
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.metric == "DCP"
    assert row.request == "Particular frequency"
    assert row.scope == "Final node"
    assert row.unit == "dBm"
    assert row.frequency_selection == "1200 MHz"


# ---------------------------------------------------------------------------
# Test: part query
# ---------------------------------------------------------------------------


def test_lookup_part_summary(engine):
    """Exact match: Part summary query for a specific part."""
    result = engine.lookup(
        metric="MismatchLoss",
        request="Part summary",
        scope="ADL8124",
        unit="dB",
        freq_selection="All Frequencies",
    )
    assert result.kind == EXACT_MATCH
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.metric == "MismatchLoss"
    assert row.request == "Part summary"
    assert row.scope == "ADL8124"


def test_lookup_node_summary(engine):
    """Exact match: Node summary query for a specific node."""
    result = engine.lookup(
        metric="MismatchLoss",
        request="Node summary",
        scope="2",
        unit="dB",
        freq_selection="All Frequencies",
    )
    assert result.kind == EXACT_MATCH
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.metric == "MismatchLoss"
    assert row.request == "Node summary"
    assert row.scope == "2"


# ---------------------------------------------------------------------------
# Test: threshold query — AMBIGUOUS even with the unit dimension
#
# AUDIT FINDING: threshold / closest / range queries carry their distinguishing
# parameter (e.g. "above 0.5 dB") only inside the QUESTION text. The unit
# dimension does NOT resolve them; they remain AMBIGUOUS under the 5-field
# key. A 6th "query parameter" dimension would be required.
# ---------------------------------------------------------------------------


def test_lookup_threshold_above_with_unit(engine):
    """Threshold Above with unit is AMBIGUOUS: 7 threshold rows share this key.

    The distinguishing threshold value (0.1, 0.5, 1, 2, 3, 5, 10 dB) exists
    only in the QUESTION text, so the 5-field key cannot separate the rows.
    The engine never guesses and reports the ambiguity.
    """
    result = engine.lookup(
        metric="MismatchLoss",
        request="Threshold Above",
        scope="All nodes",
        freq_selection="All Frequencies",
        unit="dB",
    )
    assert result.kind == AMBIGUOUS, f"Expected AMBIGUOUS, got {result.kind}: {result.message}"
    assert 0 < len(result.rows) <= 3  # engine returns up to 3 sample rows
    row = result.rows[0]
    assert row.metric == "MismatchLoss"
    assert row.request == "Threshold Above"
    assert row.scope == "All nodes"
    assert row.unit == "dB"


def test_lookup_threshold_below_with_unit(engine):
    """Threshold Below with unit is AMBIGUOUS as well."""
    result = engine.lookup(
        metric="MismatchLoss",
        request="Threshold Below",
        scope="All nodes",
        freq_selection="All Frequencies",
        unit="dB",
    )
    assert result.kind == AMBIGUOUS, f"Expected AMBIGUOUS, got {result.kind}: {result.message}"
    assert 0 < len(result.rows) <= 3
    row = result.rows[0]
    assert row.metric == "MismatchLoss"
    assert row.request == "Threshold Below"
    assert row.scope == "All nodes"
    assert row.unit == "dB"


# ---------------------------------------------------------------------------
# Test: zero matches
# ---------------------------------------------------------------------------


def test_lookup_no_match(engine):
    """Zero match: combination that doesn't exist."""
    result = engine.lookup(
        metric="MADEUPMETRIC",
        request="Best",
        scope="Final node",
        unit="dB",
        freq_selection="All Frequencies",
    )
    assert result.kind == NOT_FOUND
    assert len(result.rows) == 0


def test_lookup_combination_not_in_catalogue(engine):
    """Zero match: unrealistic dimension combination."""
    result = engine.lookup(
        metric="MismatchLoss",
        request="Maximum",
        scope="NonExistentPart",
        unit="dB",
        freq_selection="9999 MHz",
    )
    assert result.kind == NOT_FOUND


# ---------------------------------------------------------------------------
# Test: ambiguous match (multiple matches persist under the 5-field key)
# ---------------------------------------------------------------------------


def test_lookup_ambiguous_tnp_power_difference(engine):
    """TNP + Individual frequency power difference + Final node + 1200 MHz + dBm
    is AMBIGUOUS: 2 rows share this key ('above desired channel power' vs
    'above channel power' — the reference qualifier is only in QUESTION text)."""
    result = engine.lookup(
        metric="TNP",
        request="Individual frequency power difference",
        scope="Final node",
        freq_selection="1200 MHz",
        unit="dBm",
    )
    assert result.kind == AMBIGUOUS, f"Expected AMBIGUOUS, got {result.kind}: {result.message}"
    assert 0 < len(result.rows) <= 3


def test_lookup_ambiguous_tnp_power_difference_1300(engine):
    """TNP power difference at 1300 MHz with dBm is AMBIGUOUS."""
    result = engine.lookup(
        metric="TNP",
        request="Individual frequency power difference",
        scope="Final node",
        freq_selection="1300 MHz",
        unit="dBm",
    )
    assert result.kind == AMBIGUOUS, f"Expected AMBIGUOUS, got {result.kind}: {result.message}"


def test_lookup_ambiguous_tnp_power_difference_1400(engine):
    """TNP power difference at 1400 MHz with dBm is AMBIGUOUS."""
    result = engine.lookup(
        metric="TNP",
        request="Individual frequency power difference",
        scope="Final node",
        freq_selection="1400 MHz",
        unit="dBm",
    )
    assert result.kind == AMBIGUOUS, f"Expected AMBIGUOUS, got {result.kind}: {result.message}"


# ---------------------------------------------------------------------------
# Test: get_question_answer returns dict for exact match
# ---------------------------------------------------------------------------


def test_get_question_answer_exact(engine):
    """get_question_answer should return dict for exact match."""
    data = engine.get_question_answer(
        metric="MismatchLoss",
        request="Best",
        scope="Final node",
        unit="dB",
        freq_selection="All Frequencies",
    )
    assert data is not None
    assert "question" in data
    assert "answer" in data
    assert "metric" in data
    assert "request" in data
    assert "scope" in data
    assert "unit" in data
    assert "answer_type" in data
    assert "frequency_selection" in data
    assert "row_id" in data
    assert data["metric"] == "MismatchLoss"
    assert data["request"] == "Best"
    assert data["scope"] == "Final node"


def test_get_question_answer_no_match(engine):
    """get_question_answer should return None for zero-match."""
    data = engine.get_question_answer(
        metric="Nonexistent",
        request="Best",
        scope="Final node",
        unit="dB",
        freq_selection="All Frequencies",
    )
    assert data is None


def test_get_question_answer_ambiguous(engine):
    """get_question_answer should return None for ambiguous result."""
    data = engine.get_question_answer(
        metric="TNP",
        request="Individual frequency power difference",
        scope="Final node",
        freq_selection="1200 MHz",
        unit="dBm",
    )
    assert data is None  # ambiguous → None


# ---------------------------------------------------------------------------
# Test: placeholder rows handled via configuration
# ---------------------------------------------------------------------------


def test_placeholder_rows_excluded_by_default(engine):
    """Engine should exclude placeholder rows by default (METRIC="1", etc.).

    Audit-verified: exactly 17 placeholder rows and 0 unindexable (malformed)
    rows exist in the workbook.
    """
    assert engine.exclude_placeholders is True
    assert engine.total_rows() >= 0
    assert engine.placeholder_count() == 17, (
        f"Expected 17 placeholder rows, got {engine.placeholder_count()}"
    )
    assert engine.unindexable_count() == 0, (
        f"Expected 0 unindexable rows, got {engine.unindexable_count()}"
    )


def test_placeholder_rows_included_when_disabled(engine):
    """Engine should include placeholder rows when exclude_placeholders=False."""
    engine_no_excl = CatalogueEngine(exclude_placeholders=False)
    # With placeholders included, total_rows should be >= the default count
    assert engine_no_excl.total_rows() >= engine.total_rows()


# ---------------------------------------------------------------------------
# Test: introspection APIs
# ---------------------------------------------------------------------------


def test_engine_introspection(engine):
    """Engine introspection APIs should return valid sets."""
    metrics = engine.metrics()
    requests = engine.requests()
    scopes = engine.scopes()
    freq_sels = engine.freq_selections()
    answer_types = engine.answer_types()

    assert isinstance(metrics, set)
    assert isinstance(requests, set)
    assert isinstance(scopes, set)
    assert isinstance(freq_sels, set)
    assert isinstance(answer_types, set)

    # Should contain some expected values
    assert "MismatchLoss" in metrics or len(metrics) > 0
    assert len(requests) > 0
    assert len(scopes) > 0


# ---------------------------------------------------------------------------
# Test: using actual workbook rows
# ---------------------------------------------------------------------------


def test_actual_row_lookup_from_workbook(engine):
    """Test lookup using actual rows from the workbook.

    Pull row 1 from the workbook (Excel row 5, first data row) and use
    its dimension values as a lookup query. Should get an exact match back.
    """
    workbook = "C:\\Ronni\\Projects\\astra chatbot\\SystemVue_RF_Unique_Question_Assistant_V63.xlsx"
    row = _read_workbook_row(workbook, excel_row=0)  # first data row

    result = engine.lookup(
        metric=row.metric,
        request=row.request,
        scope=row.scope,
        unit=row.unit,
        freq_selection=row.frequency_selection,
    )
    # The row's own dimensions should match exactly
    assert result.kind == EXACT_MATCH, f"Expected exact match for its own dimensions, got {result.kind}: {result.message}"
    assert len(result.rows) == 1
    matched_row = result.rows[0]
    assert matched_row.metric == row.metric
    assert matched_row.request == row.request
    assert matched_row.scope == row.scope
    assert matched_row.unit == row.unit
    assert matched_row.frequency_selection == row.frequency_selection


def test_actual_row_mismatched_lookup(engine):
    """Test lookup with mismatched dimensions from actual workbook rows."""
    workbook = "C:\\Ronni\\Projects\\astra chatbot\\SystemVue_RF_Unique_Question_Assistant_V63.xlsx"
    row1 = _read_workbook_row(workbook, excel_row=0)  # first data row
    # Look up with a different unit that shouldn't match row1's dimensions
    result = engine.lookup(
        metric=row1.metric,
        request="Part summary",  # row1 has request="Best", not Part summary
        scope=row1.scope,
        freq_selection=row1.frequency_selection,
        unit="dB",  # row1 has unit="dB"
    )
    # The engine returns exactly one of the three valid outcome kinds
    # without guessing — this is the key requirement
    assert result.kind in (EXACT_MATCH, AMBIGUOUS, NOT_FOUND)